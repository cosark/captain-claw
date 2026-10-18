"""PNG screenshots → the guide's WebP assets: scaled to at most 1600 px wide."""

import sys
from pathlib import Path

from PIL import Image

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
dst.mkdir(parents=True, exist_ok=True)
for png in sorted(src.glob("*.png")):
    img = Image.open(png).convert("RGB")
    if img.width > 1600:
        img = img.resize((1600, round(img.height * 1600 / img.width)), Image.LANCZOS)
    out = dst / (png.stem + ".webp")
    img.save(out, "WEBP", quality=80, method=6)
    print(f"{out.name}: {img.width}x{img.height}, {out.stat().st_size // 1024} KB")
