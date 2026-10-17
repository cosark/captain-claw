"""Render the StopWatch screens to PNGs (and one contact sheet).

    pio run -e preview
    uv run --no-project --with pillow python preview/render.py [out dir]

Runs the preview program (the real ui_round.cpp built for the Mac), then
masks every frame to the round screen with a bezel, the way it looks on the
watch. Frames: <out>/NN_name.png; all of them: <out>/sheet.png.
"""

import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
D = 466
BEZEL = 26


def sample_picture(path: Path) -> None:
    """A landscape picture, fitted inside the circle as the deck sends it
    with round=1 (the largest 4:3 rectangle that fits)."""
    r = 4 / 3
    w, h = round(D * r / math.hypot(1, r)), round(D / math.hypot(1, r))
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        t = y / h
        for x in range(w):
            px[x, y] = (int(250 - 120 * t), int(150 - 60 * t + 20 * (x / w)), int(90 + 110 * t))
    d = ImageDraw.Draw(im)
    d.ellipse((w * 0.62, h * 0.22, w * 0.62 + 70, h * 0.22 + 70), fill=(255, 230, 160))
    d.polygon([(0, h), (0, h * 0.62), (w * 0.25, h * 0.38), (w * 0.48, h * 0.66), (w * 0.7, h * 0.45),
               (w, h * 0.7), (w, h)], fill=(40, 40, 80))
    d.polygon([(0, h), (0, h * 0.8), (w * 0.35, h * 0.62), (w * 0.6, h * 0.82), (w, h * 0.74), (w, h)],
              fill=(20, 22, 45))
    canvas = Image.new("RGB", (D, D))
    canvas.paste(im, ((D - w) // 2, (D - h) // 2))
    canvas.save(path, "JPEG", quality=85)


def framed(ppm: Path) -> Image.Image:
    """The frame as seen on the watch: round, inside a dark bezel."""
    screen = Image.open(ppm).convert("RGB")
    size = D + 2 * BEZEL
    out = Image.new("RGB", (size, size), (16, 16, 18))
    mask = Image.new("L", (D * 4, D * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, D * 4 - 1, D * 4 - 1), fill=255)
    mask = mask.resize((D, D), Image.LANCZOS)
    body = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(body).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    body = body.resize((size, size), Image.LANCZOS)
    case = Image.new("RGB", (size, size), (34, 35, 39))
    bg = Image.new("RGB", (size, size), (244, 244, 246))
    bg.paste(case, (0, 0), body)
    bg.paste(screen, (BEZEL, BEZEL), mask)
    return bg


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "preview" / "out").resolve()
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.ppm"):
        old.unlink()
    sample_picture(out / "sample.jpg")
    program = ROOT / ".pio" / "build" / "preview" / "program"
    subprocess.run([str(program), str(out), str(ROOT)], check=True, stdout=subprocess.DEVNULL)
    frames = []
    for ppm in sorted(out.glob("*.ppm")):
        img = framed(ppm)
        img.save(ppm.with_suffix(".png"))
        ppm.unlink()
        frames.append((ppm.stem, img))
    cols = 5
    cell = D + 2 * BEZEL
    pad, label = 24, 34
    rows = math.ceil(len(frames) / cols)
    sheet = Image.new("RGB", (cols * (cell + pad) + pad, rows * (cell + label + pad) + pad), (244, 244, 246))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for i, (name, img) in enumerate(frames):
        x = pad + (i % cols) * (cell + pad)
        y = pad + (i // cols) * (cell + label + pad)
        sheet.paste(img, (x, y))
        d.text((x + cell // 2, y + cell + 6), name.split("_", 1)[1].replace("_", " "), fill=(60, 62, 70),
               font=font, anchor="ma")
    sheet.save(out / "sheet.png")
    print(f"{len(frames)} screens → {out}/sheet.png")


if __name__ == "__main__":
    main()
