#!/usr/bin/env python3
"""Generate the project logo from one set of shapes.

    python tools/make_logo.py

Writes assets/logo.svg, assets/logo-256.png, assets/st25.ico (16-256 px,
including the 20/40 px sizes Windows uses at 125-150% scaling), and the installer
wizard images (assets/wizard-small-*.bmp, assets/wizard-large-*.bmp). The design:
audio level bars whose outline is a ghost - the tops form its dome, the bottoms
its wavy hem - with two eyes and a glint in each.
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ASSETS = os.path.join(ROOT, "assets")

BG, BAR, EYE, GLINT = "#15201c", "#7fe0bf", "#15201c", "#ecece8"

# The art, on a 64-unit canvas.
SIZE, RADIUS, BAR_W = 64, 14, 5.4
BARS = [(10, 27, 25), (16.6, 17, 31), (23.2, 12, 40), (29.8, 10, 38),
        (36.4, 12, 40), (43, 17, 31), (49.6, 27, 25)]                # (x, y, height)
EYES = [(25.9, 27, 4.6, 5.4), (39.1, 27, 4.6, 5.4)]                   # (cx, cy, rx, ry)
GLINTS = [(27.2, 25.6, 1.4), (40.4, 25.6, 1.4)]                      # (cx, cy, r)

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
# Installer wizard images at 100/125/150/200% scaling (Inno Setup picks the best fit).
WIZARD_SMALL = [55, 69, 83, 110]                                     # top-right of inner pages
WIZARD_LARGE = [(164, 314), (205, 393), (246, 471), (328, 628)]      # left panel, finish page


def _n(v):
    return f"{v:g}"


def svg():
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="256" height="256">',
           f'  <rect width="{SIZE}" height="{SIZE}" rx="{RADIUS}" fill="{BG}"/>',
           f'  <g fill="{BAR}">']
    for x, y, h in BARS:
        out.append(f'    <rect x="{_n(x)}" y="{_n(y)}" width="{_n(BAR_W)}" height="{_n(h)}" rx="{_n(BAR_W / 2)}"/>')
    out.append("  </g>")
    for cx, cy, rx, ry in EYES:
        out.append(f'  <ellipse cx="{_n(cx)}" cy="{_n(cy)}" rx="{_n(rx)}" ry="{_n(ry)}" fill="{EYE}"/>')
    for cx, cy, r in GLINTS:
        out.append(f'  <circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(r)}" fill="{GLINT}"/>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def raster(px, oversample=8):
    """The logo at px x px (drawn large, then downscaled for smooth edges)."""
    k = px * oversample / SIZE
    big = Image.new("RGBA", (px * oversample,) * 2, (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([0, 0, SIZE * k - 1, SIZE * k - 1], radius=RADIUS * k, fill=BG)
    for x, y, h in BARS:
        d.rounded_rectangle([x * k, y * k, (x + BAR_W) * k, (y + h) * k], radius=BAR_W / 2 * k, fill=BAR)
    for cx, cy, rx, ry in EYES:
        d.ellipse([(cx - rx) * k, (cy - ry) * k, (cx + rx) * k, (cy + ry) * k], fill=EYE)
    for cx, cy, r in GLINTS:
        d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=GLINT)
    return big.resize((px, px), Image.LANCZOS)


def wizard_small(px):
    img = Image.new("RGB", (px, px), "white")
    logo = raster(px)
    img.paste(logo, (0, 0), logo)
    return img


def wizard_large(w, h):
    img = Image.new("RGB", (w, h), BG)
    side = int(w * 0.7)
    logo = raster(side)
    img.paste(logo, ((w - side) // 2, (h - side) // 3), logo)
    return img


def main():
    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, "logo.svg"), "w", newline="\n") as f:
        f.write(svg())
    raster(256).save(os.path.join(ASSETS, "logo-256.png"))
    frames = [raster(px) for px in ICO_SIZES]
    frames[-1].save(os.path.join(ASSETS, "st25.ico"), format="ICO",
                    sizes=[(px, px) for px in ICO_SIZES], append_images=frames[:-1])
    for px in WIZARD_SMALL:
        wizard_small(px).save(os.path.join(ASSETS, f"wizard-small-{px}.bmp"))
    for w, h in WIZARD_LARGE:
        wizard_large(w, h).save(os.path.join(ASSETS, f"wizard-large-{w}.bmp"))
    print("wrote assets/logo.svg, logo-256.png, st25.ico and the wizard images")


if __name__ == "__main__":
    main()
