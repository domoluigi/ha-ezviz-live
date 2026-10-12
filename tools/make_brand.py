"""Generate the integration's brand/ images (HA 2026.3+): a front door with a peephole camera.

Original drawing, no third-party marks. Drawn at 4x and scaled down for smooth edges.
Run from the repository root: python tools/make_brand.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "custom_components" / "ezviz_live" / "brand"
DOOR = (38, 90, 140, 255)
DOOR_EDGE = (22, 58, 96, 255)
PANEL = (52, 108, 162, 255)
RING = (232, 236, 242, 255)
LENS = (24, 28, 36, 255)
GLINT = (150, 200, 255, 255)
LIVE = (232, 52, 52, 255)
HANDLE = (236, 190, 90, 255)


def draw_icon(size: int) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    door = [s * 0.20, s * 0.08, s * 0.80, s * 0.94]

    # soft shadow under the door
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [door[0], door[1] + s * 0.02, door[2], door[3] + s * 0.02], radius=s * 0.07, fill=(0, 0, 0, 80)
    )
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(s * 0.02)))
    d = ImageDraw.Draw(img)

    # door leaf with two recessed panels
    d.rounded_rectangle(door, radius=s * 0.07, fill=DOOR, outline=DOOR_EDGE, width=int(s * 0.02))
    d.rounded_rectangle([s * 0.29, s * 0.50, s * 0.71, s * 0.86], radius=s * 0.03, fill=PANEL)

    # handle
    d.rounded_rectangle([s * 0.66, s * 0.47, s * 0.74, s * 0.51], radius=s * 0.02, fill=HANDLE)

    # peephole camera: white ring, dark lens, glint
    cx, cy = s * 0.50, s * 0.30
    r_ring, r_lens = s * 0.15, s * 0.095
    d.ellipse([cx - r_ring, cy - r_ring, cx + r_ring, cy + r_ring], fill=RING)
    d.ellipse([cx - r_lens, cy - r_lens, cx + r_lens, cy + r_lens], fill=LENS)
    g = s * 0.03
    d.ellipse([cx - r_lens * 0.45 - g, cy - r_lens * 0.45 - g, cx - r_lens * 0.45 + g, cy - r_lens * 0.45 + g], fill=GLINT)

    # live dot with a light halo, readable on dark and light backgrounds
    lx, ly, lr = s * 0.79, s * 0.12, s * 0.065
    d.ellipse([lx - lr * 1.3, ly - lr * 1.3, lx + lr * 1.3, ly + lr * 1.3], fill=RING)
    d.ellipse([lx - lr, ly - lr, lx + lr, ly + lr], fill=LIVE)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw_icon(256).save(OUT / "icon.png", optimize=True)
    draw_icon(512).save(OUT / "icon@2x.png", optimize=True)
    # the dark theme asks for dark_icon*: without it HA shows "icon not available";
    # the drawing works on both backgrounds
    draw_icon(256).save(OUT / "dark_icon.png", optimize=True)
    draw_icon(512).save(OUT / "dark_icon@2x.png", optimize=True)
    for f in sorted(OUT.iterdir()):
        with Image.open(f) as im:
            print(f.name, im.size, im.mode, f.stat().st_size, "byte")


if __name__ == "__main__":
    main()
