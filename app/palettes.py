"""Built-in palette presets.

Entries with a `recolor` demonstrate didder's recommended workflow: dither to a
grayscale palette (one dimension) and recolor to the target hue afterwards,
rather than dithering straight into a low-RGB-spread palette.
"""

from __future__ import annotations

from typing import Any

from .spec import grayscale_of

# Game Boy DMG greens; dithered via their luminance equivalents.
_GAMEBOY = ["0f380f", "306230", "8bac0f", "9bbc0f"]

PALETTE_PRESETS: list[dict[str, Any]] = [
    {
        "id": "bw",
        "name": "1-bit black & white",
        "palette": ["black", "white"],
        "note": "The classic. Images are auto-converted to grayscale.",
    },
    {
        "id": "gameboy",
        "name": "Game Boy (DMG)",
        # Exactly what the "derive grayscale palette" button would produce.
        "palette": grayscale_of(_GAMEBOY),
        "recolor": _GAMEBOY,
        "note": "Grayscale palette + green recolor, the way didder recommends.",
    },
    {
        "id": "gameboy_direct",
        "name": "Game Boy (direct greens)",
        "palette": _GAMEBOY,
        "note": "Dithers straight into the green palette -- lower contrast.",
    },
    {
        "id": "cga",
        "name": "CGA 16-colour",
        "palette": [
            "000000", "0000aa", "00aa00", "00aaaa", "aa0000", "aa00aa", "aa5500",
            "aaaaaa", "555555", "5555ff", "55ff55", "55ffff", "ff5555", "ff55ff",
            "ffff55", "ffffff",
        ],
        "note": "IBM CGA text-mode palette.",
    },
    {
        "id": "macintosh",
        "name": "Macintosh 16-colour",
        "palette": [
            "ffffff", "fbf305", "ff6403", "dd0907", "f20884", "4700a5", "0000d3",
            "02abea", "1fb714", "006412", "562c05", "90713a", "c0c0c0", "808080",
            "404040", "000000",
        ],
        "note": "Classic Mac OS 16-colour system palette.",
    },
    {
        "id": "mac_bw",
        "name": "Macintosh 1-bit",
        "palette": ["000000", "ffffff"],
        "note": "Pair with edm Atkinson for the original Mac look.",
    },
    {
        "id": "atkinson_grays",
        "name": "Atkinson-ish grays (5)",
        "palette": ["000000", "4a4a4a", "969696", "c8c8c8", "ffffff"],
        "note": "Slightly light-biased gray ramp.",
    },
    {
        "id": "eink4",
        "name": "E-ink 4-gray",
        "palette": ["000000", "555555", "aaaaaa", "ffffff"],
        "note": "Evenly spaced grays, as on 4-level e-paper.",
    },
    {
        "id": "eink7",
        "name": "E-ink 7-colour",
        "palette": [
            "000000", "ffffff", "ff0000", "00ff00", "0000ff", "ffff00", "ff8000",
        ],
        "note": "Black, white, red, green, blue, yellow, orange (Spectra-style).",
    },
    {
        "id": "gray8",
        "name": "8 grays",
        "palette": ["00", "36", "6d", "92", "b6", "db", "ed", "ff"],
        "note": "Hex grayscale ramp for smoother gradients.",
    },
]

# The "gray8" entry above uses 2-digit values, which didder would read as hex
# pairs rather than grayscale numbers. Normalize to explicit 6-digit hex.
for _p in PALETTE_PRESETS:
    if _p["id"] == "gray8":
        _p["palette"] = [f"{int(v, 16):02x}" * 3 for v in _p["palette"]]
