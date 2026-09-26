"""Browser-level checks of the UI with Playwright (run inside the playwright image).

    docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp \
      -v "$PWD:/w" -w /w mcr.microsoft.com/playwright/python:v1.55.0-noble \
      sh -c "pip install -q --user playwright==1.55.0 pillow && python tests/ui_check.py"
"""

from __future__ import annotations

import math
import os
import struct
import sys
import zlib

from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
OUT = os.environ.get("OUT", "tests/screens")
os.makedirs(OUT, exist_ok=True)
failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


def make_png(path: str, w: int, h: int) -> None:
    rows = []
    cx, cy = w * 0.6, h * 0.45
    for y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            d = math.hypot(x - cx, y - cy) / (0.45 * h)
            shade = max(0.0, 1.0 - d)
            row += bytes((
                int(40 + 200 * x / w * (0.4 + 0.6 * shade)),
                int(60 + 170 * shade),
                int(200 - 150 * y / h),
            ))
        rows.append(bytes(row))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


src = os.path.join(OUT, "_input.png")
src2 = os.path.join(OUT, "_input2.png")
make_png(src, 1200, 800)
make_png(src2, 1200, 800)


def preview_src(page) -> str:
    return page.eval_on_selector("#preview-canvas", "e => e.dataset.src || ''")


def wait_new_preview(page, old: str) -> str:
    page.wait_for_function(
        "old => { const s = document.querySelector('#preview-canvas').dataset.src; return s && s !== old && !document.querySelector('#canvas').classList.contains('stale'); }",
        arg=old, timeout=20000,
    )
    return preview_src(page)


def pixel_exact(page, k: int) -> tuple[bool, str]:
    """Screenshot the page in device pixels and compare the painted preview with
    the PNG didder produced, upscaled by k with nearest neighbour. This is the
    real requirement: every image pixel painted as an exact k x k device block,
    at the exact position -- no offset tolerance. Only the part visible inside
    the (possibly scrolled) stage is compared."""
    from io import BytesIO

    from PIL import Image, ImageChops

    url = page.eval_on_selector("#preview-canvas", "e => new URL(e.dataset.src, location.href).href")
    ref = Image.open(BytesIO(page.request.get(url).body())).convert("RGB")
    g = page.evaluate("""() => {
        const c = document.querySelector('#preview-canvas');
        const st = document.querySelector('#stage');
        const r = c.getBoundingClientRect();
        const s = st.getBoundingClientRect();
        return {x: r.left, y: r.top, sl: s.left + st.clientLeft, st: s.top + st.clientTop,
                sr: s.left + st.clientLeft + st.clientWidth, sb: s.top + st.clientTop + st.clientHeight,
                dpr: devicePixelRatio, factor: +document.querySelector('#canvas').dataset.factor};
    }""")
    if g["factor"] != k:
        return False, f"factor is {g['factor']}, expected {k}"
    dpr = g["dpr"]
    xd, yd = g["x"] * dpr, g["y"] * dpr
    if abs(xd - round(xd)) > 1e-3 or abs(yd - round(yd)) > 1e-3:
        return False, f"origin not on a device pixel: ({xd:.4f}, {yd:.4f})"
    x0, y0 = round(xd), round(yd)
    shot = Image.open(BytesIO(page.screenshot(scale="device"))).convert("RGB")
    want = ref.resize((ref.width * k, ref.height * k), Image.NEAREST)

    # Visible window = art rect intersected with the stage viewport (device px).
    vx0 = max(x0, int(-(-g["sl"] * dpr // 1)))
    vy0 = max(y0, int(-(-g["st"] * dpr // 1)))
    vx1 = min(x0 + want.width, int(g["sr"] * dpr))
    vy1 = min(y0 + want.height, int(g["sb"] * dpr))
    if vx1 - vx0 < 50 or vy1 - vy0 < 50:
        return False, "almost nothing visible to compare"

    def diff_at(dx: int, dy: int):
        got = shot.crop((vx0 + dx, vy0 + dy, vx1 + dx, vy1 + dy))
        exp = want.crop((vx0 - x0, vy0 - y0, vx1 - x0, vy1 - y0))
        return ImageChops.difference(got, exp).getbbox()

    if diff_at(0, 0) is None:
        return True, f"{vx1 - vx0}x{vy1 - vy0} device px exact"
    # Diagnose: is it merely displaced, or resampled?
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            if diff_at(dx, dy) is None:
                return False, f"painted exactly but displaced by ({dx},{dy}) device px"
    return False, f"resampled/mismatched: region {diff_at(0, 0)} of {vx1 - vx0}x{vy1 - vy0}"


def integer_scale(page) -> tuple[bool, str]:
    """Backing store == CSS box x DPR exactly (so no browser resampling), and
    the art inside it is drawn at a whole-number factor."""
    g = page.evaluate("""() => {
        const c = document.querySelector('#preview-canvas');
        const r = c.getBoundingClientRect();
        const d = document.querySelector('#canvas').dataset;
        return {bw: c.width, bh: c.height, cw: r.width * devicePixelRatio, ch: r.height * devicePixelRatio,
                k: +d.factor, devW: +d.devW};
    }""")
    exact = abs(g["bw"] - g["cw"]) < 1e-6 and abs(g["bh"] - g["ch"]) < 1e-6
    return exact and g["k"] >= 1 and g["devW"] % g["k"] == 0, str(g)


with sync_playwright() as pw:
    browser = pw.chromium.launch()

    for dpr in (1, 1.25, 1.5, 1.75, 2, 3):
        print(f"== devicePixelRatio {dpr}")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=dpr, accept_downloads=True)
        page = ctx.new_page()
        console_errors: list[str] = []
        page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and console_errors.append(m.text))
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        page.goto(BASE)
        expect(page.locator("#didder-version")).not_to_have_text("…")

        page.set_input_files("#file-input", src)
        first = wait_new_preview(page, "")
        factor = int(page.eval_on_selector("#canvas", "e => e.dataset.factor"))
        ok, info = integer_scale(page)
        check(f"fit {factor}x: backing store maps 1:1 to device pixels", ok, info)
        rendering = page.eval_on_selector("#preview-canvas", "e => getComputedStyle(e).imageRendering")
        check("preview has image-rendering: pixelated", rendering == "pixelated", rendering)
        ok, why = pixel_exact(page, factor)
        check(f"fit ({factor}x) paints pixel-exact", ok, why)

        page.click("#zoom-mode button[data-zoom='1']")
        page.wait_for_timeout(100)
        ok, why = pixel_exact(page, 1)
        check("1:1 paints one device pixel per image pixel, exactly", ok, why)
        page.click("#zoom-in")
        page.wait_for_timeout(100)
        ok, why = pixel_exact(page, 2)
        check("2x paints exact 2x2 device-pixel blocks", ok, why)
        # Scroll by an awkward amount; after settling it must be exact again.
        page.eval_on_selector("#stage", "e => e.scrollBy(37, 23)")
        page.wait_for_timeout(400)
        ok, why = pixel_exact(page, 2)
        check("still exact after scrolling the stage", ok, why)
        page.click("#zoom-mode button[data-zoom='fit']")

        if dpr == 1:
            page.screenshot(path=f"{OUT}/01-bayer.png")
            cmd = page.inner_text("#command")
            check("command shown, bare and portable", cmd.startswith("didder ") and "-i _input.png" in cmd and "/work" not in cmd, cmd)
            check("scale warning visible for downscaled preview", page.is_visible("#scale-note") and "downscale" in page.inner_text("#scale-note"), page.inner_text("#scale-note"))

            # Algorithm switch re-renders and updates the command.
            page.click("#algo-tabs button[data-algo='edm']")
            second = wait_new_preview(page, first)
            check("switching to edm re-renders", second != first)
            expect(page.locator("#command")).to_contain_text("edm FloydSteinberg")
            page.check("#serpentine")
            expect(page.locator("#command")).to_contain_text("edm --serpentine FloydSteinberg")
            page.select_option("#edm-name", "Atkinson")
            expect(page.locator("#command")).to_contain_text("edm --serpentine Atkinson")
            page.screenshot(path=f"{OUT}/02-edm-atkinson.png")

            # Non-integer upscale: inline error, and no request is made.
            before = preview_src(page)
            page.fill("#upscale", "1.5")
            expect(page.locator("[data-error-for='upscale']")).to_contain_text("whole number")
            page.wait_for_timeout(600)
            check("non-integer upscale never reaches didder", preview_src(page) == before)
            check("upscale input marked invalid", "invalid" in (page.get_attribute("#upscale", "class") or ""))
            page.fill("#upscale", "2")
            wait_new_preview(page, before)
            expect(page.locator("#command")).to_contain_text("-u 2")
            ok, info = integer_scale(page)
            check("upscaled preview still integer-scaled", ok, info)
            page.fill("#upscale", "1")
            page.wait_for_timeout(400)

            # Invalid palette colour: inline error on that swatch, no crash.
            first_text = page.locator("#palette-swatches .swatch input[type=text]").nth(1)
            first_text.fill("notacolour")
            expect(page.locator("#palette-swatches .swatch .err").nth(1)).to_contain_text("not an RGB tuple")
            expect(page.locator("#palette-swatches .swatch input[type=text]").nth(1)).to_have_class("invalid")
            # Shown on the swatch itself, not repeated under the whole panel.
            expect(page.locator("[data-error-for='palette']")).to_have_text("")
            check("command marked invalid while palette is bad", "invalid" in page.get_attribute(".command", "class"))
            page.screenshot(path=f"{OUT}/03-palette-error.png")
            first_text.fill("#fff")
            expect(page.locator("#palette-swatches .swatch .err").nth(1)).to_have_text("")
            expect(page.locator("#command")).to_contain_text("-p 'black ffffff'")

            # Bayer with dropdown; odm; custom odm JSON error.
            page.click("#algo-tabs button[data-algo='bayer']")
            page.select_option("#bayer", "3x5")
            expect(page.locator("#command")).to_contain_text("bayer 3x5")
            page.click("#algo-tabs button[data-algo='odm']")
            page.check("#odm-custom")
            page.fill("#odm-matrix", '{"matrix": [[1, 2], [3]], "max": 4}')
            expect(page.locator("[data-error-for='odm_matrix']")).to_contain_text("rectangular")
            page.fill("#odm-matrix", '{"matrix": [[0, 2], [3, 1]], "max": 4}')
            expect(page.locator("#command")).to_contain_text('odm \'{"matrix": [[0, 2], [3, 1]], "max": 4}\'')
            page.uncheck("#odm-custom")

            # Random: dual slider + seed.
            page.click("#algo-tabs button[data-algo='random']")
            check("strength disabled for random", page.is_disabled("#strength-range"))
            page.fill(".dual[data-dual='all'] .vals input >> nth=0", "-0.3")
            page.fill("#seed", "1234")
            expect(page.locator("#command")).to_contain_text("random --seed 1234 -0.3 0.5")
            page.check("#random-advanced")
            expect(page.locator("#command")).to_contain_text("random --seed 1234 -0.5 0.5 -0.5 0.5 -0.5 0.5")
            page.screenshot(path=f"{OUT}/04-random.png")

            # Recolor with game boy preset, derive grayscale.
            page.click("#algo-tabs button[data-algo='bayer']")
            page.select_option("#palette-preset", "gameboy")
            expect(page.locator("#command")).to_contain_text("-r '0f380f 306230 8bac0f 9bbc0f'")
            # Preset palette is the luma of the greens (Rec.601).
            expect(page.locator("#command")).to_contain_text("-p '272727 4d4d4d 909090 9e9e9e'")
            page.screenshot(path=f"{OUT}/05-gameboy.png")

            # Derive button on a custom recolor: black + F273FF -> 000000 a9a9a9
            page.select_option("#palette-preset", "bw")
            page.check("#recolor-enabled")
            rec = page.locator("#recolor-swatches .swatch input[type=text]")
            rec.nth(0).fill("black")
            rec.nth(1).fill("F273FF")
            page.click("#derive-gray")
            expect(page.locator("#command")).to_contain_text("-p '000000 a9a9a9' -r 'black F273FF'")
            check("derive grayscale palette from recolor", True)
            page.select_option("#palette-preset", "gameboy")

            # Recolor length mismatch shows inline.
            page.click("#recolor-add")
            expect(page.locator("[data-error-for='recolor']")).to_contain_text("same number of colors")
            page.click("#recolor-match")
            expect(page.locator("[data-error-for='recolor']")).to_have_text("")

            # Sliders with numeric entry.
            page.fill("#strength-num", "64")
            expect(page.locator("#command")).to_contain_text("-s 64%")
            page.fill("#brightness-num", "20")
            expect(page.locator("#command")).to_contain_text("--brightness 20%")

            # Rapid changes: last one wins (debounce + cancellation).
            cur = preview_src(page)
            for v in range(1, 30):
                page.fill("#contrast-num", str(v))
            final = wait_new_preview(page, cur)
            page.wait_for_timeout(800)
            check("preview settles on last change", preview_src(page) == final)
            expect(page.locator("#command")).to_contain_text("--contrast 29%")

            # Before/after compare.
            page.check("#compare")
            box = page.locator("#canvas").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.3, box["y"] + 20)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.7, box["y"] + 20)
            page.mouse.up()
            split = page.eval_on_selector("#canvas", "e => e.style.getPropertyValue('--split')")
            check("before/after divider drags", split.startswith("69") or split.startswith("70"), split)
            page.screenshot(path=f"{OUT}/06-compare.png")
            page.uncheck("#compare")

            # Export triggers a real download of the full-res file.
            with page.expect_download(timeout=30000) as dl:
                page.click("#export-btn")
            d = dl.value
            path = d.path()
            with open(path, "rb") as fh:
                head = fh.read(24)
            w, h = struct.unpack(">II", head[16:24])
            check("export downloads full-resolution PNG", head[:4] == b"\x89PNG" and (w, h) == (1200, 800), (w, h))
            check("download filename", d.suggested_filename == "_input_dithered.png", d.suggested_filename)

            # Presets survive reload.
            page.click("summary:has-text('Saved presets')")
            page.fill("#preset-name", "gb-test")
            page.click("#preset-save")
            expect(page.locator("#preset-list")).to_contain_text("gb-test")
            page.once("dialog", lambda dlg: dlg.accept())
            page.click("#reset-all")
            expect(page.locator("#command")).not_to_contain_text("-r ")
            page.locator("#preset-list li", has_text="gb-test").locator("button", has_text="Load").click()
            expect(page.locator("#command")).to_contain_text("-r '0f380f 306230 8bac0f 9bbc0f'")
            page.reload()
            expect(page.locator("#didder-version")).not_to_have_text("…")
            expect(page.locator("#command")).to_contain_text("-r '0f380f 306230 8bac0f 9bbc0f'")
            check("state restored after reload", True)

            # Multi-image: animated GIF.
            page.set_input_files("#file-input", [src, src2])
            expect(page.locator("#image-list li")).to_have_count(2)
            page.click("#multi-mode button[data-mode='animate']")
            expect(page.locator("#animate-fields")).to_be_visible()
            page.fill("#fps", "6")
            expect(page.locator("#command")).to_contain_text("--fps 6")
            expect(page.locator("#command")).to_contain_text("-o out.gif")
            with page.expect_download(timeout=30000) as dl:
                page.click("#export-btn")
            with open(dl.value.path(), "rb") as fh:
                gif = fh.read()
            check("animated GIF downloaded with 2 frames", gif[:6] == b"GIF89a" and gif.count(b"\x21\xf9\x04") == 2, gif[:6])
            page.screenshot(path=f"{OUT}/07-multi.png", full_page=False)

        check("no JS console errors", not console_errors, console_errors)
        ctx.close()

    # Error banner: surface didder output verbatim. Force a didder-side failure
    # by pointing the page at a monkeypatched fetch that injects an error reply.
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    page.goto(BASE)
    expect(page.locator("#didder-version")).not_to_have_text("…")
    page.set_input_files("#file-input", src)
    wait_new_preview(page, "")
    page.evaluate("""() => {
        const orig = window.fetch;
        window.fetch = (url, opts) => String(url).includes('/api/preview')
          ? Promise.resolve(new Response(JSON.stringify({ok: false, error: "image 'b.png' isn't the same size as 'a.png'"}), {status: 422}))
          : orig(url, opts);
    }""")
    page.fill("#brightness-num", "5")
    expect(page.locator("#error-banner")).to_be_visible()
    expect(page.locator("#error-body")).to_have_text("image 'b.png' isn't the same size as 'a.png'")
    page.screenshot(path=f"{OUT}/08-error-banner.png")
    page.click("#error-close")
    expect(page.locator("#error-banner")).to_be_hidden()
    check("error banner shows didder output and dismisses", True)
    ctx.close()

    browser.close()

print()
print("FAILED: " + ", ".join(failures) if failures else "ALL PASSED")
sys.exit(1 if failures else 0)
