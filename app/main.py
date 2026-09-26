"""FastAPI backend for the didder GUI.

Dithering itself is never reimplemented here: every render shells out to the
`didder` binary with an argv list built by app.spec. Pillow is used only to read
source dimensions and to produce the downscaled preview source.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ValidationError

from .palettes import PALETTE_PRESETS
from .spec import (
    COMPRESSION_TYPES,
    EDM_NAMES,
    ODM_NAMES,
    ColorError,
    Params,
    bayer_combos,
    build_argv,
    canonical_color,
    detect_cpu_quota,
    display_command,
    grayscale_of,
    parse_color,
)

log = logging.getLogger("didder-ui")

# --------------------------------------------------------------------------- #
# Configuration (env-driven so the same code runs in Docker and bare uvicorn)
# --------------------------------------------------------------------------- #

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

# Resolved from PATH by default; never a hardcoded container path.
DIDDER_BIN = os.environ.get("DIDDER_BIN", "didder")

WORK_DIR = Path(os.environ.get("DIDDER_WORK_DIR", Path.cwd() / "work")).resolve()

MAX_UPLOAD_BYTES = int(os.environ.get("DIDDER_MAX_UPLOAD_MB", "32")) * 1024 * 1024
# Whole-request cap, enforced while the body streams in -- before the multipart
# parser spools it to a temp file.
MAX_REQUEST_BYTES = int(os.environ.get("DIDDER_MAX_REQUEST_MB", "256")) * 1024 * 1024
MAX_FILES_PER_SESSION = int(os.environ.get("DIDDER_MAX_FILES", "48"))
PREVIEW_LONG_EDGE = int(os.environ.get("DIDDER_PREVIEW_LONG_EDGE", "800"))
SESSION_TTL_SECONDS = int(os.environ.get("DIDDER_SESSION_TTL", str(6 * 3600)))
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("DIDDER_CLEANUP_INTERVAL", "300"))
RENDER_TIMEOUT_SECONDS = float(os.environ.get("DIDDER_RENDER_TIMEOUT", "120"))

# Guard against decompression-bomb uploads before Pillow allocates.
Image.MAX_IMAGE_PIXELS = int(os.environ.get("DIDDER_MAX_PIXELS", str(80_000_000)))

ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# didder capabilities discovered at startup.
DIDDER_INFO: dict[str, Any] = {"version": "unknown", "mmcq": False, "path": None}


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


@dataclass
class SourceImage:
    name: str  # sanitized original filename
    path: Path  # original upload
    width: int
    height: int
    bytes: int


@dataclass
class Session:
    id: str
    dir: Path
    images: list[SourceImage] = field(default_factory=list)
    last_used: float = field(default_factory=time.monotonic)
    generation: int = 0
    current_proc: asyncio.subprocess.Process | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_used = time.monotonic()
        with contextlib.suppress(OSError):
            os.utime(self.dir, None)

    @property
    def originals_dir(self) -> Path:
        return self.dir / "originals"

    @property
    def preview_dir(self) -> Path:
        return self.dir / "preview"

    @property
    def export_dir(self) -> Path:
        return self.dir / "export"


SESSIONS: dict[str, Session] = {}

MANIFEST = "manifest.json"


def save_manifest(sess: Session) -> None:
    """Persist upload order so a session survives an app/container restart."""
    data = {"images": [i.name for i in sess.images]}
    tmp = sess.dir / f".{MANIFEST}.tmp"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(sess.dir / MANIFEST)


def rehydrate_sessions() -> int:
    """Rebuild in-memory sessions from ./work after a restart."""
    root = WORK_DIR / "sessions"
    if not root.is_dir():
        return 0
    cutoff = time.time() - SESSION_TTL_SECONDS
    restored = 0
    for sdir in root.iterdir():
        manifest = sdir / MANIFEST
        if not (SESSION_ID_RE.match(sdir.name) and manifest.is_file()):
            continue
        try:
            if manifest.stat().st_mtime < cutoff:
                continue  # stale; the cleanup loop will remove it
            names = json.loads(manifest.read_text(encoding="utf-8")).get("images", [])
            sess = Session(id=sdir.name, dir=sdir)
            for name in names:
                path = sess.originals_dir / Path(str(name)).name
                if not path.is_file():
                    continue
                with Image.open(path) as im:
                    width, height = im.size
                sess.images.append(
                    SourceImage(name=path.name, path=path, width=width,
                                height=height, bytes=path.stat().st_size)
                )
            for sub in ("originals", "preview", "export"):
                (sdir / sub).mkdir(exist_ok=True)
            SESSIONS[sess.id] = sess
            restored += 1
        except (OSError, ValueError, UnidentifiedImageError):
            log.warning("could not restore session %s", sdir.name, exc_info=True)
    return restored


def _new_session() -> Session:
    sid = uuid.uuid4().hex
    sdir = WORK_DIR / "sessions" / sid
    for sub in ("originals", "preview", "export"):
        (sdir / sub).mkdir(parents=True, exist_ok=True)
    sess = Session(id=sid, dir=sdir)
    SESSIONS[sid] = sess
    return sess


def get_session(sid: str) -> Session:
    if not SESSION_ID_RE.match(sid or ""):
        raise HTTPException(status_code=400, detail="Malformed session id.")
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(
            status_code=404,
            detail="Session expired or unknown -- re-upload your image.",
        )
    sess.touch()
    return sess


def ensure_within(path: Path, root: Path) -> Path:
    """Resolve `path` and refuse anything that escapes `root`."""
    resolved = Path(os.path.realpath(path))
    root_resolved = Path(os.path.realpath(root))
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise HTTPException(status_code=400, detail="Path outside the work directory.")
    return resolved


# --------------------------------------------------------------------------- #
# didder invocation
# --------------------------------------------------------------------------- #


class DidderResult(BaseModel):
    ok: bool
    returncode: int
    # didder prints its own error messages to *stdout* (and only Go runtime
    # panics to stderr), so both streams are captured and reported verbatim.
    output: str
    duration_ms: int


async def run_didder(
    argv: list[str], *, session: Session | None = None, register: bool = False
) -> DidderResult:
    """Run didder with an argv list. Never goes through a shell."""
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"didder binary not found at {argv[0]!r}. Set DIDDER_BIN.",
        ) from exc

    if register and session is not None:
        session.current_proc = proc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=RENDER_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return DidderResult(
            ok=False,
            returncode=-1,
            output=f"didder timed out after {RENDER_TIMEOUT_SECONDS:g}s.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if register and session is not None and session.current_proc is proc:
            session.current_proc = None

    return DidderResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode if proc.returncode is not None else -1,
        output="\n".join(
            part
            for part in (
                stdout.decode("utf-8", "replace").strip(),
                stderr.decode("utf-8", "replace").strip(),
            )
            if part
        ),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def kill_inflight(session: Session) -> None:
    """Kill a still-running render so a newer request cannot be overtaken."""
    proc = session.current_proc
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


# --------------------------------------------------------------------------- #
# Preview source generation (the only place Pillow touches pixels)
# --------------------------------------------------------------------------- #


def preview_source(sess: Session, image: SourceImage, long_edge: int) -> tuple[Path, float]:
    """Return a downscaled PNG copy of `image` plus its scale factor.

    Cached per (image, long_edge). PNG is used so the dither input is lossless.
    """
    longest = max(image.width, image.height)
    scale = min(1.0, long_edge / longest) if longest else 1.0

    dest = sess.preview_dir / f"src_{image.name}.{long_edge}.png"
    if dest.exists():
        return dest, scale

    with Image.open(image.path) as im:
        im = im.convert("RGBA" if _has_alpha(im) else "RGB")
        if scale < 1.0:
            target = (
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale)),
            )
            im = im.resize(target, Image.LANCZOS)
        im.save(dest, format="PNG")
    return dest, scale


def _has_alpha(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or (
        im.mode == "P" and "transparency" in im.info
    )


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class RenderRequest(BaseModel):
    session: str
    params: dict[str, Any]
    image_index: int = 0


# Cross-field checks in Params raise messages prefixed with the concern they
# belong to; map those prefixes back to a field so the UI can show them inline.
_ERROR_PREFIX_FIELDS: tuple[tuple[str, str], ...] = (
    ("bayer:", "bayer"),
    ("palette:", "palette"),
    ("recolor:", "recolor"),
    ("mmcq:", "mmcq"),
    ("odm matrix:", "odm_matrix"),
    ("edm matrix:", "edm_matrix"),
    ("random:", "random"),
    ("gif:", "format"),
)


def parse_params(raw: dict[str, Any]) -> Params:
    """Validate params, converting pydantic errors into inline field errors."""
    try:
        return Params.model_validate(raw)
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            msg = str(err["msg"]).removeprefix("Value error, ")
            field = ".".join(str(p) for p in err["loc"])
            if not field:
                for prefix, name in _ERROR_PREFIX_FIELDS:
                    if msg.startswith(prefix):
                        field = name
                        break
            errors.append({"field": field or "params", "message": msg})
        raise HTTPException(
            status_code=422, detail={"message": "Invalid settings.", "errors": errors}
        ) from exc


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

async def didder_version(binary: str) -> str:
    """First line of `didder --version` (which writes to stdout)."""
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"'{binary} --version' exited {proc.returncode}: "
            f"{err.decode('utf-8', 'replace').strip()}"
        )
    lines = out.decode("utf-8", "replace").strip().splitlines()
    return lines[0] if lines else "didder (unknown version)"


def resolve_didder() -> str:
    """Locate didder on PATH (or DIDDER_BIN), failing loudly if absent."""
    candidate = DIDDER_BIN if os.sep in DIDDER_BIN else shutil.which(DIDDER_BIN)
    if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise RuntimeError(
            "\n"
            "==================================================================\n"
            f" FATAL: the 'didder' binary was not found (looked for {DIDDER_BIN!r}).\n"
            "\n"
            "  In Docker it is built into the image at /usr/local/bin/didder.\n"
            "  For local development, install it and put it on PATH:\n"
            "      go install github.com/makeworld-the-better-one/didder@v1.3.0\n"
            "  or point at a specific binary:  DIDDER_BIN=/path/to/didder\n"
            "==================================================================="
        )
    return candidate


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(
        level=os.environ.get("DIDDER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    binary = resolve_didder()
    version = await didder_version(binary)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    (WORK_DIR / "sessions").mkdir(parents=True, exist_ok=True)
    if os.environ.get("TMPDIR"):
        Path(os.environ["TMPDIR"]).mkdir(parents=True, exist_ok=True)

    DIDDER_INFO["path"] = binary
    DIDDER_INFO["version"] = version
    DIDDER_INFO["mmcq"] = await _probe_mmcq(binary)
    DIDDER_INFO["threads_default"] = detect_cpu_quota()
    restored = rehydrate_sessions()
    if restored:
        log.info("restored %d session(s) from %s", restored, WORK_DIR)

    log.info(
        "didder ready: %s at %s | mmcq=%s | threads=%s | work=%s",
        version,
        binary,
        DIDDER_INFO["mmcq"],
        DIDDER_INFO["threads_default"],
        WORK_DIR,
    )

    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        for sess in SESSIONS.values():
            kill_inflight(sess)


class _BodyTooLarge(Exception):
    pass


class BodySizeLimit:
    """Reject oversized request bodies with 413 while they stream in.

    Checks Content-Length up front and also counts bytes as they arrive, so
    chunked uploads are capped too. Without this, Starlette would spool the
    whole multipart body to disk (or a RAM-backed /tmp) before the upload
    handler could apply its own per-file limit.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = False
            if too_big:
                return await self._reject(send)

        received = 0
        started = False
        exceeded = False
        replaced = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started, replaced
            if exceeded:
                # The multipart parser catches our exception and answers with
                # a generic 400; swap that for an accurate 413.
                if message["type"] == "http.response.start" and not started:
                    started = replaced = True
                    await self._reject(send)
                    return
                if replaced:
                    return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(send)

    async def _reject(self, send) -> None:
        body = json.dumps(
            {"detail": f"Request exceeds the {self.max_bytes // (1024 * 1024)} MB limit."}
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


app = FastAPI(title="didder GUI", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(BodySizeLimit, max_bytes=MAX_REQUEST_BYTES)


async def _probe_mmcq(binary: str) -> bool:
    """Detect whether this didder build understands `-p mmcq:N`.

    mmcq landed after v1.3.0, so a released binary will reject it. Probing with a
    deliberately bad value is enough: the error text differs depending on whether
    the flag is understood at all.
    """
    probe_dir = WORK_DIR / ".probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    src = probe_dir / "probe.png"
    if not src.exists():
        Image.new("RGB", (8, 8), (128, 128, 128)).save(src, format="PNG")
    out = probe_dir / "probe_out.png"
    result = await run_didder(
        [binary, "-p", "mmcq:4", "-i", str(src), "-o", str(out), "bayer", "2x2"]
    )
    with contextlib.suppress(OSError):
        out.unlink()
    return result.ok


async def cleanup_loop() -> None:
    """Drop stale sessions from memory and disk on a timer."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            await asyncio.get_running_loop().run_in_executor(None, cleanup_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # never let the janitor kill itself
            log.exception("session cleanup failed")


def cleanup_once() -> None:
    now = time.monotonic()
    for sid, sess in list(SESSIONS.items()):
        if now - sess.last_used > SESSION_TTL_SECONDS:
            SESSIONS.pop(sid, None)
            shutil.rmtree(sess.dir, ignore_errors=True)
            log.info("cleaned up session %s", sid)

    # Also sweep directories left behind by a previous process.
    sessions_root = WORK_DIR / "sessions"
    wall_cutoff = time.time() - SESSION_TTL_SECONDS
    if sessions_root.is_dir():
        for child in sessions_root.iterdir():
            if not child.is_dir() or child.name in SESSIONS:
                continue
            with contextlib.suppress(OSError):
                if child.stat().st_mtime < wall_cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    log.info("cleaned up orphaned session dir %s", child.name)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness + confirmation that didder is actually callable."""
    binary = DIDDER_INFO.get("path") or DIDDER_BIN
    result = await run_didder([binary, "--version"])
    if not result.ok:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "didder": "not callable",
                "output": result.output,
            },
        )
    return JSONResponse(
        {
            "status": "ok",
            "didder": DIDDER_INFO["version"],
            "mmcq": DIDDER_INFO["mmcq"],
            "sessions": len(SESSIONS),
        }
    )


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    return {
        "didder": DIDDER_INFO["version"],
        "mmcq_supported": DIDDER_INFO["mmcq"],
        "odm_names": list(ODM_NAMES),
        "edm_names": list(EDM_NAMES),
        "bayer_combos": bayer_combos(),
        "compression_types": list(COMPRESSION_TYPES),
        "palette_presets": PALETTE_PRESETS,
        "threads_default": DIDDER_INFO.get("threads_default", detect_cpu_quota()),
        "preview_long_edge": PREVIEW_LONG_EDGE,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_files": MAX_FILES_PER_SESSION,
    }


@app.post("/api/upload")
async def api_upload(
    request: Request,
    files: list[UploadFile],
    session: str | None = None,
) -> dict[str, Any]:
    sess = get_session(session) if session else _new_session()

    if len(sess.images) + len(files) > MAX_FILES_PER_SESSION:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_FILES_PER_SESSION} images per session.",
        )

    added: list[dict[str, Any]] = []
    for upload in files:
        # Keep the user's filename (the displayed command refers to it) but make
        # it inert: basename only, no separators, no leading dots.
        name = SAFE_NAME_RE.sub("_", Path(upload.filename or "image").name).lstrip(". ")
        if not name:
            name = "image"
        suffix = Path(name).suffix.lower()

        content_type = (upload.content_type or "").split(";")[0].strip().lower()
        if content_type and not content_type.startswith("image/"):
            raise HTTPException(
                status_code=415,
                detail=f"{name}: content type {content_type!r} is not an image.",
            )
        if suffix and suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail=f"{name}: {suffix} is not a supported image extension.",
            )

        # Stream to disk with a hard size cap.
        dest = ensure_within(sess.originals_dir / name, sess.originals_dir)
        stem, ext = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = sess.originals_dir / f"{stem}_{counter}{ext}"
            counter += 1

        total = 0
        try:
            with dest.open("wb") as fh:
                while chunk := await upload.read(1024 * 256):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"{name} exceeds the "
                                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."
                            ),
                        )
                    fh.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise

        # Confirm it really is an image Pillow (and so didder) can decode.
        try:
            with Image.open(dest) as im:
                im.verify()
            with Image.open(dest) as im:
                width, height = im.size
                fmt = im.format
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=415, detail=f"{name}: not a readable image ({exc})."
            ) from exc

        img = SourceImage(
            name=dest.name, path=dest, width=width, height=height, bytes=total
        )
        sess.images.append(img)
        added.append(
            {
                "name": img.name,
                "width": width,
                "height": height,
                "bytes": total,
                "format": fmt,
            }
        )

    save_manifest(sess)
    return {
        "session": sess.id,
        "added": added,
        "images": [
            {"name": i.name, "width": i.width, "height": i.height, "bytes": i.bytes}
            for i in sess.images
        ],
    }


@app.post("/api/session/{sid}/remove/{index}")
async def api_remove(sid: str, index: int) -> dict[str, Any]:
    sess = get_session(sid)
    if not 0 <= index < len(sess.images):
        raise HTTPException(status_code=404, detail="No such image.")
    img = sess.images.pop(index)
    with contextlib.suppress(OSError):
        img.path.unlink()
    for stale in sess.preview_dir.glob(f"src_{img.name}.*"):
        with contextlib.suppress(OSError):
            stale.unlink()
    save_manifest(sess)
    return {
        "images": [
            {"name": i.name, "width": i.width, "height": i.height, "bytes": i.bytes}
            for i in sess.images
        ]
    }


@app.post("/api/preview")
async def api_preview(req: RenderRequest) -> JSONResponse:
    sess = get_session(req.session)
    params = parse_params(req.params)

    if params.palette_mode == "mmcq" and not DIDDER_INFO["mmcq"]:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This didder build does not support mmcq palettes.",
                "errors": [{"field": "palette_mode", "message": "mmcq unsupported"}],
            },
        )

    if not sess.images:
        raise HTTPException(status_code=400, detail="Upload an image first.")
    if not 0 <= req.image_index < len(sess.images):
        raise HTTPException(status_code=400, detail="No such image.")

    image = sess.images[req.image_index]

    # Keep the rendered preview near PREVIEW_LONG_EDGE even when upscaling, so
    # the dither-pattern-to-output-size ratio matches the export.
    long_edge = max(120, min(PREVIEW_LONG_EDGE, round(PREVIEW_LONG_EDGE / params.upscale)))

    loop = asyncio.get_running_loop()
    try:
        src, scale = await loop.run_in_executor(
            None, preview_source, sess, image, long_edge
        )
    except (OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not build preview source: {exc}"
        ) from exc

    # Scale explicit -x/-y by the same factor so the preview stays proportional.
    preview_params = params.model_copy(
        update={
            "width": max(1, round(params.width * scale)) if params.width else None,
            "height": max(1, round(params.height * scale)) if params.height else None,
            # Overwriting the preview file each time is intentional; --no-overwrite
            # is an export-only concern.
            "no_overwrite": False,
        }
    )

    async with sess.lock:
        sess.generation += 1
        generation = sess.generation
        kill_inflight(sess)

    out_name = f"p{generation}.{params.format}"
    out_path = sess.preview_dir / out_name

    argv = build_argv(
        preview_params,
        inputs=[str(src)],
        output=str(out_path),
        binary=DIDDER_INFO.get("path") or DIDDER_BIN,
        animate=False,
    )

    result = await run_didder(argv, session=sess, register=True)

    if generation != sess.generation:
        # A newer request arrived; this render is obsolete.
        with contextlib.suppress(OSError):
            out_path.unlink()
        return JSONResponse(status_code=409, content={"stale": True})

    _prune_previews(sess, keep=out_name)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": result.output or f"didder exited {result.returncode} with no output",
                "command": export_command_for(sess, params, image),
            },
        )

    try:
        with Image.open(out_path) as im:
            out_w, out_h = im.size
    except (OSError, UnidentifiedImageError):
        out_w, out_h = 0, 0

    return JSONResponse(
        {
            "ok": True,
            "url": f"/api/file/{sess.id}/preview/{out_name}?v={generation}",
            "source_url": f"/api/file/{sess.id}/preview/{src.name}",
            "width": out_w,
            "height": out_h,
            "source_width": image.width,
            "source_height": image.height,
            "preview_scale": round(scale, 4),
            "duration_ms": result.duration_ms,
            "command": export_command_for(sess, params, image),
            "output": result.output,
        }
    )


def _prune_previews(sess: Session, *, keep: str) -> None:
    for old in sess.preview_dir.glob("p*.*"):
        if old.name != keep:
            with contextlib.suppress(OSError):
                old.unlink()


def export_command_for(sess: Session, params: Params, image: SourceImage) -> str:
    """The full-resolution command, written with plain filenames.

    Shown in the UI so it can be pasted into a shell next to the source image,
    rather than referring to session paths that only exist inside the container.
    """
    multi = len(sess.images) > 1
    animate = multi and params.multi_mode == "animate" and params.format == "gif"

    if multi:
        inputs = [i.name for i in sess.images]
        output = "out.gif" if animate else "./out/"
    else:
        inputs = [image.name]
        output = f"{Path(image.name).stem}_dithered.{params.format}"

    argv = build_argv(
        params, inputs=inputs, output=output, binary="didder", animate=animate
    )
    return display_command(argv)


@app.post("/api/export")
async def api_export(req: RenderRequest) -> JSONResponse:
    sess = get_session(req.session)
    params = parse_params(req.params)

    if params.palette_mode == "mmcq" and not DIDDER_INFO["mmcq"]:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This didder build does not support mmcq palettes.",
                "errors": [{"field": "palette_mode", "message": "mmcq unsupported"}],
            },
        )
    if not sess.images:
        raise HTTPException(status_code=400, detail="Upload an image first.")

    multi = len(sess.images) > 1
    animate = multi and params.multi_mode == "animate"
    if animate and params.format != "gif":
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Animated output requires the GIF format.",
                "errors": [{"field": "format", "message": "must be gif to animate"}],
            },
        )
    if animate and not params.fps:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "didder requires --fps for animated GIF output.",
                "errors": [{"field": "fps", "message": "required for animated GIF"}],
            },
        )

    if animate and not (params.width and params.height):
        sizes = {(i.width, i.height) for i in sess.images}
        if len(sizes) > 1:
            listing = ", ".join(f"{i.name} {i.width}x{i.height}" for i in sess.images)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "didder needs every frame of an animated GIF to be the same "
                        "size. Set both width and height to resize them all, or "
                        f"upload same-size frames. ({listing})"
                    ),
                    "errors": [
                        {"field": "width", "message": "set both width and height"},
                        {"field": "height", "message": "set both width and height"},
                    ],
                },
            )

    # Fresh export directory each time so a download never mixes runs.
    stamp = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
    out_dir = ensure_within(sess.export_dir / stamp, sess.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs = [str(i.path) for i in sess.images] if multi else [str(sess.images[
        req.image_index if 0 <= req.image_index < len(sess.images) else 0
    ].path)]

    if animate:
        output = str(out_dir / "animation.gif")
    elif multi:
        output = str(out_dir)  # existing dir => didder writes one file per input
    else:
        chosen = Path(inputs[0]).stem
        output = str(out_dir / f"{chosen}_dithered.{params.format}")

    argv = build_argv(
        params,
        inputs=inputs,
        output=output,
        binary=DIDDER_INFO.get("path") or DIDDER_BIN,
        animate=animate,
    )

    result = await run_didder(argv)
    if not result.ok:
        shutil.rmtree(out_dir, ignore_errors=True)
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": result.output or f"didder exited {result.returncode} with no output",
            },
        )

    produced = sorted(p for p in out_dir.iterdir() if p.is_file())
    if not produced:
        shutil.rmtree(out_dir, ignore_errors=True)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "didder produced no output files."},
        )

    files = [
        {
            "name": p.name,
            "bytes": p.stat().st_size,
            "url": f"/api/download/{sess.id}/{stamp}/{p.name}",
        }
        for p in produced
    ]

    zip_url = None
    if len(produced) > 1:
        zip_path = out_dir / "dithered.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in produced:
                zf.write(p, arcname=p.name)
        zip_url = f"/api/download/{sess.id}/{stamp}/{zip_path.name}"

    return JSONResponse(
        {
            "ok": True,
            "files": files,
            "zip_url": zip_url,
            "duration_ms": result.duration_ms,
            "output": result.output,
            "command": export_command_for(sess, params, sess.images[0]),
        }
    )


@app.post("/api/command")
async def api_command(req: RenderRequest) -> dict[str, Any]:
    """Validate params and return the command without rendering anything."""
    params = parse_params(req.params)
    # An expired session should not break the live command display.
    sess = SESSIONS.get(req.session) if req.session else None
    if sess is not None:
        sess.touch()
    if sess and sess.images:
        idx = req.image_index if 0 <= req.image_index < len(sess.images) else 0
        return {"command": export_command_for(sess, params, sess.images[idx])}

    argv = build_argv(
        params,
        inputs=["input.png"],
        output=f"output.{params.format}",
        binary="didder",
        animate=False,
    )
    return {"command": display_command(argv)}


class DeriveRequest(BaseModel):
    colors: list[str]


@app.post("/api/derive-grayscale")
async def api_derive_grayscale(req: DeriveRequest) -> dict[str, Any]:
    """Luminance equivalents of the recolor palette, for the derive button."""
    try:
        return {"palette": grayscale_of([c for c in req.colors if c.strip()])}
    except ColorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class ColorCheckRequest(BaseModel):
    colors: list[str]
    allow_alpha: bool = False


@app.post("/api/check-colors")
async def api_check_colors(req: ColorCheckRequest) -> dict[str, Any]:
    """Per-swatch validation so the UI can show inline field errors."""
    out = []
    for raw in req.colors:
        try:
            r, g, b, a = parse_color(raw, allow_alpha=req.allow_alpha)
            out.append(
                {
                    "input": raw,
                    "ok": True,
                    "hex": f"#{r:02x}{g:02x}{b:02x}",
                    "alpha": a,
                    "canonical": canonical_color(raw, allow_alpha=req.allow_alpha),
                }
            )
        except ColorError as exc:
            out.append({"input": raw, "ok": False, "error": str(exc)})
    return {"colors": out}


@app.get("/api/file/{sid}/{kind}/{name}")
async def api_file(sid: str, kind: str, name: str) -> FileResponse:
    sess = get_session(sid)
    if kind not in ("preview", "originals"):
        raise HTTPException(status_code=404, detail="Unknown file kind.")
    root = sess.preview_dir if kind == "preview" else sess.originals_dir
    path = ensure_within(root / Path(name).name, root)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        path, headers={"Cache-Control": "no-store"}
    )


@app.get("/api/download/{sid}/{stamp}/{name}")
async def api_download(sid: str, stamp: str, name: str) -> FileResponse:
    sess = get_session(sid)
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", stamp):
        raise HTTPException(status_code=400, detail="Malformed export id.")
    root = ensure_within(sess.export_dir / stamp, sess.export_dir)
    path = ensure_within(root / Path(name).name, root)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found.")
    return FileResponse(
        path,
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
