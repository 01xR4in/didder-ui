"""Unit tests for validation and argv construction (no Docker needed).

    pip install -r requirements-dev.txt && pytest -q
"""

from __future__ import annotations

import asyncio
import builtins
import io
import shutil

import pytest
from pydantic import ValidationError

from app.spec import (
    ColorError,
    Params,
    bayer_combos,
    build_argv,
    canonical_color,
    detect_cpu_quota,
    display_command,
    grayscale_of,
    parse_color,
    valid_bayer,
)


def argv(**kw):
    return build_argv(Params(**kw), inputs=["in.png"], output="out.png")


# --------------------------------------------------------------------- bayer


@pytest.mark.parametrize(
    "x,y,ok",
    [
        (2, 2, True), (4, 4, True), (8, 2, True), (1, 2, True), (128, 128, True),
        (3, 3, True), (3, 5, True), (5, 3, True),
        (1, 1, False), (5, 5, False), (6, 4, False), (3, 4, False), (0, 4, False),
    ],
)
def test_bayer_validity_matches_didder(x, y, ok):
    assert valid_bayer(x, y) is ok


def test_bayer_combos_are_all_valid_and_include_specials():
    combos = bayer_combos()
    assert all(valid_bayer(c["x"], c["y"]) for c in combos)
    labels = {c["label"] for c in combos}
    assert {"3x3", "3x5", "5x3", "4x4", "16x2"} <= labels
    assert "1x1" not in labels


def test_bayer_invalid_is_rejected_with_message():
    with pytest.raises(ValidationError, match="powers of two"):
        Params(algorithm="bayer", bayer_x=5, bayer_y=5)


# -------------------------------------------------------------------- colors


@pytest.mark.parametrize(
    "raw,rgba",
    [
        ("black", (0, 0, 0, 255)),
        ("forestGreen", (34, 139, 34, 255)),
        ("#D24242", (0xD2, 0x42, 0x42, 255)),
        ("d24242", (0xD2, 0x42, 0x42, 255)),
        ("#abc", (0xAA, 0xBB, 0xCC, 255)),
        ("23,230,100", (23, 230, 100, 255)),
        ("135", (135, 135, 135, 255)),  # grayscale, not #113355
        ("0", (0, 0, 0, 255)),
    ],
)
def test_parse_color_forms(raw, rgba):
    assert parse_color(raw) == rgba


@pytest.mark.parametrize("raw", ["notacolour", "256", "1,2", "1,2,3,4,5", "300,0,0", "#12345", "forest green", "1 2 3", ""])
def test_parse_color_rejects(raw):
    with pytest.raises(ColorError):
        parse_color(raw)


def test_rgba_only_in_recolor():
    with pytest.raises(ColorError, match="only allowed in --recolor"):
        parse_color("0,0,0,0")
    assert parse_color("0,0,0,0", allow_alpha=True) == (0, 0, 0, 0)


def test_canonical_color_keeps_what_the_user_typed_where_didder_accepts_it():
    assert canonical_color("forestGreen") == "forestGreen"
    assert canonical_color("F273FF") == "F273FF"
    assert canonical_color("#F273FF") == "#F273FF"
    assert canonical_color("135") == "135"
    assert canonical_color(" 1, 2, 3 ") == "1,2,3"
    assert canonical_color("#abc") == "aabbcc"  # didder needs 6 hex digits
    assert canonical_color("0,0,0,0", allow_alpha=True) == "0,0,0,0"


def test_grayscale_of_uses_rec601_luma():
    assert grayscale_of(["black", "white", "0f380f", "F273FF"]) == ["000000", "ffffff", "272727", "a9a9a9"]


def test_palette_rules():
    with pytest.raises(ValidationError, match="at least two"):
        Params(palette=["black"])
    with pytest.raises(ValidationError, match="same number of colors"):
        Params(palette=["black", "white"], recolor_enabled=True, recolor=["red"])
    with pytest.raises(ValidationError, match="power of two"):
        Params(palette_mode="mmcq", mmcq=6)
    with pytest.raises(ValidationError, match="256"):
        Params(format="gif", palette=[f"{i:02x}" * 3 for i in range(256)] + ["red"])
    # mmcq recolor must match N
    Params(palette_mode="mmcq", mmcq=2, recolor_enabled=True, recolor=["red", "blue"])


# ------------------------------------------------------------------- matrices


def test_custom_matrices():
    Params(algorithm="odm", odm_custom=True, odm_matrix='{"matrix": [[0, 2], [3, 1]], "max": 4}')
    Params(algorithm="edm", edm_custom=True, edm_matrix="[[0, 0, 7], [3, 5, 1]]")
    for bad, msg in [
        ('{"matrix": [[1]], "max": 0}', "cannot be 0"),
        ('{"matrix": [[1, 2], [3]], "max": 4}', "rectangular"),
        ('{"matrix": [[1]]}', "max"),
        ("[[1]]", "expected an object"),
        ("{oops", "invalid JSON"),
    ]:
        with pytest.raises(ValidationError, match=msg):
            Params(algorithm="odm", odm_custom=True, odm_matrix=bad)
    with pytest.raises(ValidationError, match="plain 2D array"):
        Params(algorithm="edm", edm_custom=True, edm_matrix='{"matrix": []}')


# ---------------------------------------------------------------------- argv


def test_argv_is_a_list_with_global_flags_before_subcommand():
    a = argv(algorithm="bayer", bayer_x=8, bayer_y=8, strength=64, brightness=20, grayscale=True)
    assert isinstance(a, list) and all(isinstance(x, str) for x in a)
    sub = a.index("bayer")
    assert a[sub:] == ["bayer", "8x8"]
    for flag in ("-p", "-s", "--brightness", "-g", "-i", "-o", "-f"):
        assert a.index(flag) < sub
    assert a[a.index("-s") + 1] == "64%"


def test_defaults_are_omitted():
    a = argv()
    assert a == ["didder", "-p", "black white", "-f", "png", "-i", "in.png", "-o", "out.png", "bayer", "4x4"]


def test_random_seed_precedes_min_max_and_strength_is_dropped():
    a = argv(algorithm="random", seed=42, random_min=-0.3, random_max=0.6, strength=64)
    assert a[a.index("random"):] == ["random", "--seed", "42", "-0.3", "0.6"]
    assert "-s" not in a[: a.index("random")]
    a = argv(algorithm="random", random_advanced=True, random_rgb=[-0.5, 0.5, -0.2, 0.2, -1, 1])
    assert a[a.index("random"):] == ["random", "-0.5", "0.5", "-0.2", "0.2", "-1", "1"]


def test_edm_serpentine_is_a_subcommand_flag():
    a = argv(algorithm="edm", edm_name="atkinson", serpentine=True)
    assert a[-3:] == ["edm", "--serpentine", "Atkinson"]


def test_custom_matrix_is_one_argv_element():
    m = '{"matrix": [[0, 2], [3, 1]], "max": 4}'
    a = argv(algorithm="odm", odm_custom=True, odm_matrix=m)
    assert a[-2:] == ["odm", m]


def test_animated_gif_gets_fps_and_loop():
    p = Params(format="gif", fps=12.5, loop=3)
    a = build_argv(p, inputs=["a.png", "b.png"], output="o.gif", animate=True)
    assert a[a.index("--fps") + 1] == "12.5" and a[a.index("-l") + 1] == "3"
    assert a.count("-i") == 2


def test_hostile_values_cannot_inject():
    with pytest.raises(ValidationError):
        Params(palette=["black", "white;rm -rf /"])
    with pytest.raises(ValidationError):
        Params(edm_name="$(reboot)")
    with pytest.raises(ValidationError, match="Extra inputs"):
        Params(**{"--in": "/etc/passwd"})
    with pytest.raises(ValidationError):
        Params(upscale=1.5)
    # Even odd-but-valid strings are quoted for display only.
    assert display_command(["didder", "-p", "a b", "odm", '{"x": 1}']) == "didder -p 'a b' odm '{\"x\": 1}'"


# -------------------------------------------------------------------- threads


def _fake_open(files):
    real = builtins.open

    def fake(path, *a, **kw):
        if path in files:
            if files[path] is None:
                raise FileNotFoundError(path)
            return io.StringIO(files[path])
        return real(path, *a, **kw)

    return fake


def test_threads_from_cgroup_v2(monkeypatch):
    monkeypatch.setattr(builtins, "open", _fake_open({"/sys/fs/cgroup/cpu.max": "150000 100000\n"}))
    assert detect_cpu_quota() == 1
    monkeypatch.setattr(builtins, "open", _fake_open({"/sys/fs/cgroup/cpu.max": "400000 100000\n"}))
    assert detect_cpu_quota() == 4


def test_threads_unlimited_falls_back_to_affinity(monkeypatch):
    monkeypatch.setattr(builtins, "open", _fake_open({
        "/sys/fs/cgroup/cpu.max": "max 100000\n",
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": None,
    }))
    monkeypatch.setattr("os.sched_getaffinity", lambda _pid: {0, 1, 2}, raising=False)
    assert detect_cpu_quota() == 3


# ------------------------------------------------------- real didder (if any)


@pytest.mark.skipif(shutil.which("didder") is None, reason="didder not on PATH")
def test_didder_errors_are_captured_from_stdout(tmp_path):
    """didder prints its error messages on stdout; they must still surface."""
    from app.main import run_didder

    res = asyncio.run(run_didder(["didder", "-p", "black", "-i", str(tmp_path / "x.png"), "-o", str(tmp_path / "o.png"), "bayer", "2x2"]))
    assert not res.ok
    assert "at least two colors" in res.output


# ------------------------------------------------------------ session cleanup


def test_cleanup_removes_stale_sessions_and_orphans(tmp_path, monkeypatch):
    import os
    import time

    import app.main as m

    monkeypatch.setattr(m, "WORK_DIR", tmp_path)
    monkeypatch.setattr(m, "SESSIONS", {})
    stale = m._new_session()
    stale.last_used -= m.SESSION_TTL_SECONDS + 1
    fresh = m._new_session()
    orphan = tmp_path / "sessions" / "0123456789abcdef0123456789abcdef"
    orphan.mkdir(parents=True)
    old = time.time() - m.SESSION_TTL_SECONDS - 60
    os.utime(orphan, (old, old))

    m.cleanup_once()

    assert stale.id not in m.SESSIONS and not stale.dir.exists()
    assert fresh.id in m.SESSIONS and fresh.dir.exists()
    assert not orphan.exists()
