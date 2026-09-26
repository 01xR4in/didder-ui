"""End-to-end smoke test against a running instance (stdlib only).

    python tests/e2e_smoke.py [http://127.0.0.1:8000]
"""

from __future__ import annotations

import json
import struct
import sys
import threading
import urllib.error
import urllib.request
import uuid
import zlib

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
failures: list[str] = []


def make_png(w: int, h: int) -> bytes:
    raw = b"".join(
        b"\x00" + bytes(v for x in range(w) for v in (255 * x // (w - 1), 255 * y // (h - 1), 128))
        for y in range(h)
    )

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def request(method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post_json(path: str, obj) -> tuple[int, dict]:
    status, body, _ = request("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"})
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, {"raw": body.decode(errors="replace")}


def upload(files: list[tuple[str, bytes, str]], session: str | None = None) -> tuple[int, dict]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, data, ctype in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n"
        )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    q = f"?session={session}" if session else ""
    status, raw, _ = request("POST", "/api/upload" + q, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return status, json.loads(raw)


def check(name: str, cond: bool, detail: object = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


print(f"== {BASE}")

s, b, _ = request("GET", "/healthz")
health = json.loads(b)
check("healthz 200 + didder callable", s == 200 and health.get("status") == "ok", b)

s, b, _ = request("GET", "/api/config")
cfg = json.loads(b)
check("config lists 15 odm + 11 edm", len(cfg["odm_names"]) == 15 and len(cfg["edm_names"]) == 11, cfg)
check("bayer combos exclude 1x1, include 3x5", all(c["label"] != "1x1" for c in cfg["bayer_combos"]) and any(c["label"] == "3x5" for c in cfg["bayer_combos"]))

# -- upload validation ----------------------------------------------------- #
s, b = upload([("evil.txt", b"hello", "text/plain")])
check("rejects non-image content type", s == 415, b)
s, b = upload([("fake.png", b"not really a png", "image/png")])
check("rejects undecodable image", s == 415, b)
s, b = upload([("../../etc/passwd.png", make_png(64, 40), "image/png")])
check("path traversal in filename is neutralized", s == 200 and "/" not in b["added"][0]["name"] and ".." not in b["added"][0]["name"], b)

s, up = upload([("photo.png", make_png(1600, 1000), "image/png")])
check("upload ok", s == 200 and up["images"][0]["width"] == 1600, up)
sid = up["session"]

base = {"algorithm": "bayer", "bayer_x": 4, "bayer_y": 4, "palette": ["black", "white"]}

# -- preview --------------------------------------------------------------- #
s, pv = post_json("/api/preview", {"session": sid, "params": base})
check("preview ok", s == 200 and pv.get("ok"), pv)
check("preview downscaled to 800 long edge", pv.get("width") == 800 and pv.get("height") == 500, pv)
check("command is bare and portable", pv.get("command", "").startswith("didder ") and "/work" not in pv["command"], pv.get("command"))
check("command uses original filename", "-i photo.png" in pv.get("command", ""), pv.get("command"))

s, _, h = request("GET", pv["url"])
check("preview image served as png", s == 200 and h.get("content-type") == "image/png", h)

s, pv2 = post_json("/api/preview", {"session": sid, "params": {**base, "upscale": 2}})
check("upscale keeps preview ~800 wide", s == 200 and pv2["width"] == 800, pv2)

s, pv3 = post_json("/api/preview", {"session": sid, "params": {**base, "width": 400}})
check("explicit -x scaled into preview", s == 200 and pv3["width"] == 200 and "-x 400" in pv3["command"], pv3)

# -- every algorithm / option family --------------------------------------- #
variants = {
    "odm builtin": {"algorithm": "odm", "odm_name": "ClusteredDotSpiral5x5"},
    "odm custom": {"algorithm": "odm", "odm_custom": True, "odm_matrix": '{"matrix":[[1,3],[2,0]],"max":4}'},
    "edm serpentine": {"algorithm": "edm", "edm_name": "Atkinson", "serpentine": True},
    "edm custom": {"algorithm": "edm", "edm_custom": True, "edm_matrix": "[[0,0,7],[3,5,1]]"},
    "random seed": {"algorithm": "random", "seed": 42, "random_min": -0.3, "random_max": 0.6},
    "random rgb": {"algorithm": "random", "random_advanced": True, "random_rgb": [-0.5, 0.5, -0.2, 0.2, -0.7, 0.7]},
    "mmcq": {"palette_mode": "mmcq", "mmcq": 8, "algorithm": "edm"},
    "recolor rgba": {"palette": ["black", "white"], "recolor_enabled": True, "recolor": ["0,0,0,0", "F273FF"]},
    "adjustments": {"strength": 64, "brightness": 20, "contrast": -10, "saturation": 15, "grayscale": True, "no_exif_rotation": True},
    "gif + compression": {"format": "gif", "compression": "size"},
    "mixed color formats": {"palette": ["23,230,100", "D24242", "135", "forestGreen", "#abc"]},
    "threads": {"threads": 1},
}
for label, extra in variants.items():
    s, r = post_json("/api/preview", {"session": sid, "params": {**base, **extra}})
    check(f"preview: {label}", s == 200 and r.get("ok"), r)

s, r = post_json("/api/preview", {"session": sid, "params": {**base, "algorithm": "random", "seed": 7}})
check("random seed goes before min/max", "random --seed 7 -0.5 0.5" in r.get("command", ""), r.get("command"))
check("random omits --strength", " -s " not in r.get("command", ""), r.get("command"))
s, r = post_json("/api/preview", {"session": sid, "params": {**base, "algorithm": "edm", "serpentine": True}})
check("edm --serpentine after subcommand", r.get("command", "").endswith("edm --serpentine FloydSteinberg"), r.get("command"))

# -- validation: inline field errors, never a crash ------------------------ #
bad = {
    "bayer 5x5": ({"bayer_x": 5, "bayer_y": 5}, "powers of two"),
    "bayer 1x1": ({"bayer_x": 1, "bayer_y": 1}, "1x1"),
    "bad palette color": ({"palette": ["black", "notacolor"]}, "notacolor"),
    "palette one color": ({"palette": ["black"]}, "at least two"),
    "rgba in palette": ({"palette": ["0,0,0,0", "white"]}, "RGBA"),
    "recolor length mismatch": ({"recolor_enabled": True, "recolor": ["red"]}, "same number"),
    "mmcq non-power-of-two": ({"palette_mode": "mmcq", "mmcq": 6}, "power of two"),
    "odm bad json": ({"algorithm": "odm", "odm_custom": True, "odm_matrix": "{nope"}, "invalid JSON"),
    "odm max 0": ({"algorithm": "odm", "odm_custom": True, "odm_matrix": '{"matrix":[[1]],"max":0}'}, "cannot be 0"),
    "edm ragged": ({"algorithm": "edm", "edm_custom": True, "edm_matrix": "[[1,2],[3]]"}, "rectangular"),
    "unknown edm": ({"algorithm": "edm", "edm_name": "Nope"}, "unknown"),
    "brightness out of range": ({"brightness": 150}, "less than or equal"),
    "non-integer upscale": ({"upscale": 1.5}, "integer"),
    "bad format": ({"format": "jpg"}, "png"),
    "bad compression": ({"compression": "max"}, "default"),
    "unknown field": ({"shell": "; rm -rf /"}, "Extra inputs"),
    "random min > max": ({"algorithm": "random", "random_min": 0.5, "random_max": -0.5}, "min"),
}
for label, (extra, needle) in bad.items():
    s, r = post_json("/api/preview", {"session": sid, "params": {**base, **extra}})
    errs = json.dumps(r)
    check(f"rejects {label} (422, field error)", s == 422 and needle.lower() in errs.lower() and "errors" in errs, (s, errs[:240]))

# -- injection: hostile strings stay single argv entries ------------------- #
s, r = post_json("/api/preview", {"session": sid, "params": {**base, "palette": ["black", "white;touch /tmp/pwned"]}})
check("shell metachar color rejected", s == 422, r)

# -- stale requests / cancellation ----------------------------------------- #
slow = {**base, "algorithm": "edm", "edm_name": "JarvisJudiceNinke", "palette_mode": "mmcq", "mmcq": 64}
results: dict[int, int] = {}


def fire(i: int) -> None:
    results[i] = post_json("/api/preview", {"session": sid, "params": {**slow, "brightness": i}})[0]


threads = [threading.Thread(target=fire, args=(i,)) for i in range(6)]
for t in threads:
    t.start()
    t.join(0.03)
for t in threads:
    t.join()
latest = max(results)
check("only the newest concurrent preview wins", results[latest] == 200 and all(v == 409 for k, v in results.items() if k != latest), results)

# -- export ---------------------------------------------------------------- #
s, ex = post_json("/api/export", {"session": sid, "params": {**base, "upscale": 2}})
check("export ok", s == 200 and ex.get("ok") and len(ex["files"]) == 1, ex)
s, blob, h = request("GET", ex["files"][0]["url"])
w, hh = struct.unpack(">II", blob[16:24])
check("export is full-res x upscale (3200x2000)", (w, hh) == (3200, 2000), (w, hh))
check("export served as attachment", "attachment" in h.get("content-disposition", ""), h)

# -- multi-image: batch + animated gif ------------------------------------- #
s, up2 = upload([("frame2.png", make_png(320, 200), "image/png")], session=sid)
s, up2 = upload([("frame3.png", make_png(1600, 1000), "image/png")], session=sid)
check("multi upload", s == 200 and len(up2["images"]) >= 3, up2)

s, ex = post_json("/api/export", {"session": sid, "params": {**base, "multi_mode": "batch"}})
check("batch export -> one file per input + zip", s == 200 and len(ex["files"]) == len(up2["images"]) and ex["zip_url"], ex)
s, zipb, _ = request("GET", ex["zip_url"])
check("zip downloads", s == 200 and zipb[:2] == b"PK")

s, ex = post_json("/api/export", {"session": sid, "params": {**base, "multi_mode": "animate", "format": "gif"}})
check("animate without fps rejected", s == 422 and "fps" in json.dumps(ex), ex)
anim = {**base, "multi_mode": "animate", "format": "gif", "fps": 4, "loop": 2}
s, ex = post_json("/api/export", {"session": sid, "params": anim})
check("mixed-size frames rejected up front with explanation", s == 422 and "same size" in json.dumps(ex), ex)

s, ex = post_json("/api/export", {"session": sid, "params": {**anim, "width": 160, "height": 100}})
check("animated gif export (frames resized via -x/-y)", s == 200 and ex.get("files", [{}])[0].get("name") == "animation.gif", ex)
check("animated command has --fps/-l", "--fps 4" in ex.get("command", "") and "-l 2" in ex.get("command", ""), ex.get("command"))
if ex.get("files"):
    s, gif, _ = request("GET", ex["files"][0]["url"])
    check("gif magic + one frame per input", gif[:6] == b"GIF89a" and gif.count(b"\x21\xf9\x04") == len(up2["images"]), gif.count(b"\x21\xf9\x04"))

big = [f"{i:02x}{i:02x}{i:02x}" for i in range(256)] + ["red"]
s, r = post_json("/api/preview", {"session": sid, "params": {**base, "format": "gif", "palette": big}})
check("gif with >256 colors rejected before didder", s == 422 and "256" in json.dumps(r), r)

# -- file access safety ---------------------------------------------------- #
for path in (
    f"/api/file/{sid}/preview/..%2F..%2F..%2Fetc%2Fpasswd",
    f"/api/file/{sid}/originals/../../../../etc/passwd",
    f"/api/file/{sid}/export/x",
    f"/api/download/{sid}/..%2F..%2F/passwd",
    "/api/file/../../etc/preview/passwd",
):
    s, body, _ = request("GET", path)
    check(f"no escape: {path[:60]}", s in (400, 404) and b"root:" not in body, (s, body[:80]))

s, body, _ = request("GET", "/api/file/nonexistentsession1234/preview/x.png")
check("unknown session 404", s == 404)

print()
print("FAILED:" if failures else "ALL PASSED", ", ".join(failures))
sys.exit(1 if failures else 0)
