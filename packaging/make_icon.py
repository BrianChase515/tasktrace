"""Draw the TaskTrace app icon and build TaskTrace.icns (iconutil runs on macOS only)."""

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).parent
S = 1024


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # macOS icon grid: the shape sits inside ~824px with a soft shadow
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([100, 112, 924, 936], radius=185, fill=(0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    bg = Image.new("RGBA", (S, S))
    top, bottom = (36, 44, 66), (14, 18, 30)
    px = bg.load()
    for y in range(S):
        t = y / S
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)) + (255,)
        for x in range(S):
            px[x, y] = c
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([100, 100, 924, 924], radius=185, fill=255)
    img.paste(bg, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # a dotted trail leading into a "record" mark: a path being captured
    trail = [(250, 700), (330, 640), (410, 610), (490, 600)]
    for i, (x, y) in enumerate(trail):
        r = 16 + i * 4
        d.ellipse([x - r, y - r, x + r, y + r], fill=(120, 200, 255, 110 + i * 35))
    cx, cy = 640, 470
    d.ellipse([cx - 200, cy - 200, cx + 200, cy + 200], outline=(235, 242, 255, 255), width=44)
    d.ellipse([cx - 95, cy - 95, cx + 95, cy + 95], fill=(255, 77, 77, 255))
    return img


def main() -> int:
    icon = draw()
    icon.save(HERE / "icon-1024.png")
    iconset = HERE / "TaskTrace.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
    if sys.platform == "darwin":
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "TaskTrace.icns")], check=True)
        print("wrote TaskTrace.icns")
    else:
        print("wrote iconset (run on macOS to build .icns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
