"""Validation and argv construction for the `didder` CLI.

Everything here mirrors didder's own parsing rules (verified against v1.3.0 and
main @408a18a) so that invalid input is rejected with a field-level error
*before* a subprocess is started, rather than surfacing as an opaque failure.

Nothing in this module ever builds a shell string: callers get a list[str] argv
that is handed straight to exec. `display_command()` produces a separately
shell-quoted rendering purely for showing to the user.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .colornames import SVG_COLOR_NAMES

# --------------------------------------------------------------------------- #
# Enumerations of didder's built-in matrices (names as didder spells them).
# didder lowercases and treats '-' and '_' alike, so comparison is normalized.
# --------------------------------------------------------------------------- #

ODM_NAMES: tuple[str, ...] = (
    "ClusteredDot4x4",
    "ClusteredDotDiagonal8x8",
    "Vertical5x3",
    "Horizontal3x5",
    "ClusteredDotDiagonal6x6",
    "ClusteredDotDiagonal8x8_2",
    "ClusteredDotDiagonal16x16",
    "ClusteredDot6x6",
    "ClusteredDotSpiral5x5",
    "ClusteredDotHorizontalLine",
    "ClusteredDotVerticalLine",
    "ClusteredDot8x8",
    "ClusteredDot6x6_2",
    "ClusteredDot6x6_3",
    "ClusteredDotDiagonal8x8_3",
)

EDM_NAMES: tuple[str, ...] = (
    "Simple2D",
    "FloydSteinberg",
    "FalseFloydSteinberg",
    "JarvisJudiceNinke",
    "Atkinson",
    "Stucki",
    "Burkes",
    "Sierra",
    "TwoRowSierra",
    "SierraLite",
    "StevenPigeon",
)

COMPRESSION_TYPES: tuple[str, ...] = ("default", "no", "speed", "size")

# Powers of two didder accepts for `bayer`, plus its three documented
# non-power-of-two exceptions. 1x1 is rejected by didder ("will not dither").
_BAYER_POWERS: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64, 128)
_BAYER_EXCEPTIONS: tuple[tuple[int, int], ...] = ((3, 3), (3, 5), (5, 3))


def bayer_combos() -> list[dict[str, Any]]:
    """Every X/Y pair didder will accept, for the UI dropdown."""
    combos: list[dict[str, Any]] = []
    for x in _BAYER_POWERS:
        for y in _BAYER_POWERS:
            if x == 1 and y == 1:
                continue  # didder: "a 1x1 matrix will not dither the image"
            combos.append(
                {
                    "x": x,
                    "y": y,
                    "label": f"{x}x{y}",
                    "group": "Square" if x == y else "Non-square",
                }
            )
    for x, y in _BAYER_EXCEPTIONS:
        combos.append({"x": x, "y": y, "label": f"{x}x{y}", "group": "Special"})
    return combos


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def valid_bayer(x: int, y: int) -> bool:
    if x == 1 and y == 1:
        return False
    if (x, y) in _BAYER_EXCEPTIONS:
        return True
    return is_power_of_two(x) and is_power_of_two(y)


# --------------------------------------------------------------------------- #
# Colors
# --------------------------------------------------------------------------- #

_HEX6 = re.compile(r"\A#?[0-9a-fA-F]{6}\Z")
_HEX3 = re.compile(r"\A#?([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])\Z")


class ColorError(ValueError):
    """A color literal didder would not accept."""


def parse_color(raw: str, *, allow_alpha: bool = False) -> tuple[int, int, int, int]:
    """Parse one didder color literal to RGBA.

    Accepts the four forms didder accepts -- comma-separated RGB(A) tuples, a
    6-digit hex code, a single 0-255 grayscale number, and an SVG 1.1 color name
    -- plus 3-digit hex as a convenience, which `canonical_color` expands before
    it ever reaches didder.
    """
    s = raw.strip()
    if not s:
        raise ColorError("empty color")
    # Within one swatch, "1, 2, 3" is unambiguous; canonical_color() re-emits
    # it without spaces, since didder splits the palette argument on spaces.
    s = re.sub(r"\s*,\s*", ",", s)
    if " " in s:
        # didder splits its palette argument on spaces, so a space inside one
        # color would silently become two colors.
        raise ColorError(f"{raw!r}: a single color cannot contain a space")

    commas = s.count(",")
    if commas in (2, 3):
        if commas == 3 and not allow_alpha:
            raise ColorError(
                f"{raw!r}: RGBA (4-value) tuples are only allowed in --recolor"
            )
        parts = [p.strip() for p in s.split(",")]
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            raise ColorError(f"{raw!r} is not a valid RGB tuple. Example: 25,200,150") from None
        if any(n < 0 or n > 255 for n in nums):
            raise ColorError(f"{raw!r}: tuple values must each be 0-255")
        if commas == 2:
            return nums[0], nums[1], nums[2], 255
        return nums[0], nums[1], nums[2], nums[3]

    if commas:
        raise ColorError(f"{raw!r}: expected 3 or 4 comma-separated values")

    if _HEX6.match(s):
        h = s.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255

    m = _HEX3.match(s)
    if m and not s.lstrip("#").isdigit():
        # Ambiguity guard: "135" is grayscale 135 to didder, not #113355.
        r, g, b = (int(c * 2, 16) for c in m.groups())
        return r, g, b, 255

    if s.isdigit():
        n = int(s)
        if n > 255:
            raise ColorError(f"{raw!r}: single numbers must be in the range 0-255")
        return n, n, n, 255

    named = SVG_COLOR_NAMES.get(s.lower())
    if named is not None:
        return named[0], named[1], named[2], 255

    raise ColorError(
        f"{raw!r} is not an RGB tuple, hex code, number 0-255, or SVG color name"
    )


def canonical_color(raw: str, *, allow_alpha: bool = False) -> str:
    """Normalize a color to a form didder parses identically.

    SVG names, 6-digit hex, grayscale numbers and RGB(A) tuples are passed
    through as typed so the generated command reads the way the user wrote it;
    3-digit hex is expanded because didder's hex scanner needs exactly 6 digits.
    """
    s = re.sub(r"\s*,\s*", ",", raw.strip())
    r, g, b, a = parse_color(s, allow_alpha=allow_alpha)
    if a != 255:
        return f"{r},{g},{b},{a}"
    if "," in s:
        return f"{r},{g},{b}"
    if s.lower() in SVG_COLOR_NAMES or (s.isdigit() and len(s) <= 3) or _HEX6.match(s):
        return s
    return f"{r:02x}{g:02x}{b:02x}"


def luminance(r: int, g: int, b: int) -> int:
    """Rec.601 luma, matching the grayscale conversion didder applies."""
    return max(0, min(255, round(0.299 * r + 0.587 * g + 0.114 * b)))


def grayscale_of(colors: list[str]) -> list[str]:
    """Luminance equivalents of `colors`, for deriving --palette from --recolor."""
    out: list[str] = []
    for c in colors:
        r, g, b, _ = parse_color(c, allow_alpha=True)
        v = luminance(r, g, b)
        out.append(f"{v:02x}{v:02x}{v:02x}")
    return out


# --------------------------------------------------------------------------- #
# Request model
# --------------------------------------------------------------------------- #

Percent = Annotated[float, Field(ge=-100.0, le=100.0)]


def _fmt_num(v: float) -> str:
    """Render a number without a trailing '.0'."""
    if v == int(v):
        return str(int(v))
    return repr(round(v, 4))


def _fmt_percent(v: float) -> str:
    return f"{_fmt_num(v)}%"


class Params(BaseModel):
    """The full didder parameter set, validated against didder's constraints."""

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["bayer", "odm", "edm", "random"] = "bayer"

    # -- bayer ------------------------------------------------------------- #
    bayer_x: int = Field(default=4, ge=1, le=256)
    bayer_y: int = Field(default=4, ge=1, le=256)

    # -- odm --------------------------------------------------------------- #
    odm_name: str = "ClusteredDot4x4"
    odm_custom: bool = False
    odm_matrix: str = ""

    # -- edm --------------------------------------------------------------- #
    edm_name: str = "FloydSteinberg"
    edm_custom: bool = False
    edm_matrix: str = ""
    serpentine: bool = False

    # -- random ------------------------------------------------------------ #
    random_advanced: bool = False
    random_min: float = Field(default=-0.5, ge=-1.0, le=1.0)
    random_max: float = Field(default=0.5, ge=-1.0, le=1.0)
    random_rgb: list[float] = Field(default_factory=lambda: [-0.5, 0.5] * 3)
    seed: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)

    # -- palette ----------------------------------------------------------- #
    palette_mode: Literal["colors", "mmcq"] = "colors"
    palette: list[str] = Field(default_factory=lambda: ["black", "white"])
    mmcq: int = Field(default=8, ge=2, le=256)

    # -- recolor ----------------------------------------------------------- #
    recolor_enabled: bool = False
    recolor: list[str] = Field(default_factory=list)

    # -- adjustments ------------------------------------------------------- #
    strength: Annotated[float, Field(ge=-100.0, le=100.0)] = 100.0
    brightness: Percent = 0.0
    contrast: Percent = 0.0
    saturation: Percent = 0.0
    grayscale: bool = False
    no_exif_rotation: bool = False

    # -- sizing ------------------------------------------------------------ #
    width: int | None = Field(default=None, ge=1, le=20000)
    height: int | None = Field(default=None, ge=1, le=20000)
    upscale: int = Field(default=1, ge=1, le=32)

    # -- output ------------------------------------------------------------ #
    format: Literal["png", "gif"] = "png"
    compression: Literal["default", "no", "speed", "size"] = "default"
    no_overwrite: bool = False
    multi_mode: Literal["batch", "animate"] = "batch"
    fps: float | None = Field(default=None, gt=0, le=100)
    loop: int = Field(default=0, ge=0, le=65535)
    threads: int | None = Field(default=None, ge=1, le=256)

    # ---------------------------------------------------------------- #
    # Field-level validation
    # ---------------------------------------------------------------- #

    @field_validator("odm_name")
    @classmethod
    def _check_odm(cls, v: str) -> str:
        norm = v.lower().replace("-", "_")
        for name in ODM_NAMES:
            if name.lower().replace("-", "_") == norm:
                return name
        raise ValueError(f"unknown ordered dither matrix {v!r}")

    @field_validator("edm_name")
    @classmethod
    def _check_edm(cls, v: str) -> str:
        norm = v.lower().replace("-", "_")
        for name in EDM_NAMES:
            if name.lower().replace("-", "_") == norm:
                return name
        raise ValueError(f"unknown error diffusion matrix {v!r}")

    @field_validator("palette", "recolor")
    @classmethod
    def _strip_colors(cls, v: list[str]) -> list[str]:
        if len(v) > 256:
            raise ValueError("at most 256 colors")
        return [c.strip() for c in v if c.strip()]

    @field_validator("random_rgb")
    @classmethod
    def _check_rgb_range(cls, v: list[float]) -> list[float]:
        if len(v) != 6:
            raise ValueError("per-channel random needs exactly 6 values")
        if any(x < -1.0 or x > 1.0 for x in v):
            raise ValueError("random values must be between -1.0 and 1.0")
        return v

    # ---------------------------------------------------------------- #
    # Cross-field validation
    # ---------------------------------------------------------------- #

    @model_validator(mode="after")
    def _check_all(self) -> Params:
        # -- algorithm-specific ------------------------------------------- #
        if self.algorithm == "bayer" and not valid_bayer(self.bayer_x, self.bayer_y):
            if self.bayer_x == 1 and self.bayer_y == 1:
                raise ValueError("bayer: a 1x1 matrix will not dither the image")
            raise ValueError(
                f"bayer: {self.bayer_x}x{self.bayer_y} is invalid -- both dimensions "
                "must be powers of two (except 3x3, 3x5 and 5x3)"
            )

        if self.algorithm == "odm" and self.odm_custom:
            _validate_odm_matrix(self.odm_matrix)

        if self.algorithm == "edm" and self.edm_custom:
            _validate_edm_matrix(self.edm_matrix)

        if self.algorithm == "random":
            if self.random_advanced:
                for lo, hi in zip(self.random_rgb[0::2], self.random_rgb[1::2], strict=True):
                    if lo > hi:
                        raise ValueError("random: each min must not exceed its max")
            elif self.random_min > self.random_max:
                raise ValueError("random: min must not exceed max")

        # -- palette ------------------------------------------------------- #
        if self.palette_mode == "mmcq":
            if not is_power_of_two(self.mmcq) or self.mmcq < 2:
                raise ValueError("mmcq: N must be a power of two and at least 2")
            palette_len = self.mmcq
        else:
            if len(self.palette) < 2:
                raise ValueError("palette: at least two colors are required")
            for c in self.palette:
                try:
                    parse_color(c, allow_alpha=False)
                except ColorError as exc:
                    raise ValueError(f"palette: {exc}") from exc
            palette_len = len(self.palette)

        # -- recolor ------------------------------------------------------- #
        if self.recolor_enabled:
            if not self.recolor:
                raise ValueError("recolor: enabled but no colors given")
            for c in self.recolor:
                try:
                    parse_color(c, allow_alpha=True)
                except ColorError as exc:
                    raise ValueError(f"recolor: {exc}") from exc
            if len(self.recolor) != palette_len:
                raise ValueError(
                    f"recolor: must have the same number of colors as the palette "
                    f"({len(self.recolor)} vs {palette_len})"
                )

        # -- output -------------------------------------------------------- #
        if self.format == "gif" and palette_len > 256:
            raise ValueError("gif: the GIF format supports at most 256 palette colors")

        return self

    # ---------------------------------------------------------------- #
    # Derived helpers
    # ---------------------------------------------------------------- #

    @property
    def palette_arg(self) -> str:
        if self.palette_mode == "mmcq":
            return f"mmcq:{self.mmcq}"
        return " ".join(canonical_color(c) for c in self.palette)

    @property
    def recolor_arg(self) -> str:
        return " ".join(canonical_color(c, allow_alpha=True) for c in self.recolor)

    @property
    def strength_applies(self) -> bool:
        """didder ignores --strength for `random`."""
        return self.algorithm != "random"


def _load_matrix_json(raw: str, field: str) -> Any:
    text = raw.strip()
    if not text:
        raise ValueError(f"{field}: a custom matrix is required")
    if len(text) > 100_000:
        raise ValueError(f"{field}: matrix JSON is too large")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field}: invalid JSON -- {exc.msg} (line {exc.lineno})") from None


def _check_rectangular(rows: Any, field: str) -> None:
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{field}: matrix is empty")
    width = None
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError(f"{field}: every row must be a non-empty array")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise ValueError(
                f"{field}: matrix is not rectangular -- all rows must be the same length"
            )
        for cell in row:
            if isinstance(cell, bool) or not isinstance(cell, (int, float)):
                raise ValueError(f"{field}: matrix values must be numbers")


def _validate_odm_matrix(raw: str) -> None:
    data = _load_matrix_json(raw, "odm matrix")
    if not isinstance(data, dict):
        raise ValueError('odm matrix: expected an object like {"matrix": [[...]], "max": N}')
    if "matrix" not in data:
        raise ValueError('odm matrix: missing "matrix" key')
    if "max" not in data:
        raise ValueError('odm matrix: missing "max" key')
    mx = data["max"]
    if isinstance(mx, bool) or not isinstance(mx, (int, float)) or mx == 0:
        raise ValueError("odm matrix: the max value of the matrix cannot be 0")
    _check_rectangular(data["matrix"], "odm matrix")


def _validate_edm_matrix(raw: str) -> None:
    data = _load_matrix_json(raw, "edm matrix")
    if not isinstance(data, list):
        raise ValueError("edm matrix: expected a plain 2D array, e.g. [[0,0,7],[3,5,1]]")
    _check_rectangular(data, "edm matrix")


# --------------------------------------------------------------------------- #
# argv construction
# --------------------------------------------------------------------------- #


def build_argv(
    params: Params,
    *,
    inputs: list[str],
    output: str,
    binary: str = "didder",
    animate: bool = False,
) -> list[str]:
    """Build the didder argv.

    urfave/cli v2 requires global flags to precede the subcommand, so the layout
    is: <binary> <global flags> -i IN... -o OUT <command> <command args>.
    """
    argv: list[str] = [binary, "-p", params.palette_arg]

    if params.recolor_enabled and params.recolor:
        argv += ["-r", params.recolor_arg]

    # Omit values equal to didder's own defaults so the command stays readable.
    if params.strength_applies and params.strength != 100.0:
        argv += ["-s", _fmt_percent(params.strength)]
    if params.brightness:
        argv += ["--brightness", _fmt_percent(params.brightness)]
    if params.contrast:
        argv += ["--contrast", _fmt_percent(params.contrast)]
    if params.saturation:
        argv += ["--saturation", _fmt_percent(params.saturation)]
    if params.grayscale:
        argv.append("-g")
    if params.no_exif_rotation:
        argv.append("--no-exif-rotation")

    if params.width:
        argv += ["-x", str(params.width)]
    if params.height:
        argv += ["-y", str(params.height)]
    if params.upscale > 1:
        argv += ["-u", str(params.upscale)]

    argv += ["-f", params.format]
    if params.format == "png" and params.compression != "default":
        argv += ["-c", params.compression]
    if params.no_overwrite:
        argv.append("--no-overwrite")

    if animate:
        # didder requires --fps whenever it writes an animated GIF.
        argv += ["--fps", _fmt_num(params.fps if params.fps else 10)]
        if params.loop:
            argv += ["-l", str(params.loop)]

    if params.threads:
        argv += ["-j", str(params.threads)]

    for path in inputs:
        argv += ["-i", path]
    argv += ["-o", output]

    argv += _subcommand_argv(params)
    return argv


def _subcommand_argv(params: Params) -> list[str]:
    if params.algorithm == "bayer":
        return ["bayer", f"{params.bayer_x}x{params.bayer_y}"]

    if params.algorithm == "odm":
        return ["odm", params.odm_matrix.strip() if params.odm_custom else params.odm_name]

    if params.algorithm == "edm":
        argv = ["edm"]
        if params.serpentine:
            argv.append("--serpentine")
        argv.append(params.edm_matrix.strip() if params.edm_custom else params.edm_name)
        return argv

    # random: didder skips flag parsing for this command and reads --seed
    # positionally, so the seed must come first, before min/max.
    argv = ["random"]
    if params.seed is not None:
        argv += ["--seed", str(params.seed)]
    if params.random_advanced:
        argv += [_fmt_num(v) for v in params.random_rgb]
    else:
        argv += [_fmt_num(params.random_min), _fmt_num(params.random_max)]
    return argv


def display_command(argv: list[str]) -> str:
    """Shell-quote an argv purely for display. Never used to invoke anything."""
    return " ".join(shlex.quote(a) for a in argv)


def detect_cpu_quota() -> int:
    """Threads to use by default.

    os.cpu_count() reports the host's cores even when the container is limited
    to a fraction of them, so prefer the cgroup quota when one is set.
    """
    try:  # cgroup v2
        with open("/sys/fs/cgroup/cpu.max", encoding="ascii") as fh:
            quota_s, period_s = fh.read().split()
        if quota_s != "max":
            quota, period = int(quota_s), int(period_s)
            if period > 0 and quota > 0:
                return max(1, math.floor(quota / period))
    except (OSError, ValueError):
        pass

    try:  # cgroup v1
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", encoding="ascii") as fh:
            quota = int(fh.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", encoding="ascii") as fh:
            period = int(fh.read().strip())
        if quota > 0 and period > 0:
            return max(1, math.floor(quota / period))
    except (OSError, ValueError):
        pass

    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, os.cpu_count() or 1)
