"""Smooth (antialiased) VLW fonts for the StopWatch's round screen.

    uv run --no-project --with pillow python tools/make_fonts.py <dir with Inter-*.ttf>

Writes fonts/*.vlw (the format M5GFX's loadFont reads: big-endian header,
one 28-byte record per glyph, then 8-bit coverage bitmaps). The source is
Inter 3.19 (rsms/inter, SIL Open Font License 1.1, see fonts/OFL.txt); any
TTF with the same file names works.
"""

import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "fonts"


def span(a: int, b: int) -> list[int]:
    return list(range(a, b + 1))


# Reading text: ASCII, Latin-1, Latin Extended-A (č ć š ž đ Đ …), Romanian
# comma letters, and the punctuation replies use.
TEXT = (span(0x20, 0x7E) + span(0xA0, 0xFF) + span(0x100, 0x17F) + span(0x218, 0x21B)
        + span(0x2010, 0x2014) + span(0x2018, 0x201E) + span(0x2020, 0x2022)
        + [0x2026, 0x2030, 0x2032, 0x2033, 0x2039, 0x203A, 0x20AC, 0x2122]
        + span(0x2190, 0x2193) + [0x2212, 0x2248, 0x2264, 0x2265, 0x2713])
# The big badge text: values ("75%", "+6 dB", "15 s"), initials, words.
# Initials are capitals, so only the capital accented letters.
BADGE = (span(0x20, 0x7E) + [c for c in span(0xC0, 0x17F) if chr(c).isupper()]
         + [0x2026, 0x2713])
DIGITS = [ord(c) for c in " 0123456789:.-"]

# name, file, pixel size, characters, tabular digits
FONTS = [
    ("small", "Inter-Medium.ttf", 22, TEXT, False),
    ("body_s", "Inter-Regular.ttf", 27, TEXT, False),
    ("body_m", "Inter-Regular.ttf", 32, TEXT, False),
    ("body_l", "Inter-Regular.ttf", 39, TEXT, False),
    ("title", "Inter-SemiBold.ttf", 34, TEXT, False),
    ("badge", "Inter-Bold.ttf", 58, BADGE, True),
    ("digits", "Inter-Medium.ttf", 104, DIGITS, True),
]


def glyph(font: ImageFont.FreeTypeFont, ch: str):
    """(bitmap bytes, width, height, xAdvance, top above baseline, left)."""
    adv = round(font.getlength(ch))
    x0, y0, x1, y1 = font.getbbox(ch, anchor="ls")
    w, h = max(0, x1 - x0), max(0, y1 - y0)
    if ch == " " or w == 0 or h == 0:
        return b"", 0, 0, adv, 0, 0
    im = Image.new("L", (w, h), 0)
    ImageDraw.Draw(im).text((-x0, -y0), ch, font=font, fill=255, anchor="ls")
    return im.tobytes(), w, h, adv, -y0, x0


def build(name: str, ttf: Path, size: int, chars: list[int], tabular: bool) -> int:
    font = ImageFont.truetype(str(ttf), size)
    cmap = sorted({c for c in chars if c == 0x20 or font.getmask(chr(c)).getbbox()})
    glyphs = {c: glyph(font, chr(c)) for c in cmap}
    if tabular:  # digits share one advance, centred in it, so timers don't jitter
        digits = [ord(d) for d in "0123456789" if ord(d) in glyphs]
        wide = max(glyphs[d][3] for d in digits)
        for d in digits:
            bm, w, h, adv, top, left = glyphs[d]
            glyphs[d] = (bm, w, h, wide, top, left + (wide - adv) // 2)
    ascent = glyphs.get(ord("d"), glyphs[cmap[-1]])[4]
    p = glyphs.get(ord("p"))
    descent = (p[2] - p[4]) if p else 0
    head = struct.pack(">6i", len(cmap), 11, size, 0, ascent, descent)
    recs, bitmaps = [], []
    for c in cmap:
        bm, w, h, adv, top, left = glyphs[c]
        assert w < 256 and adv < 256 and -128 <= left < 128, (name, hex(c))
        recs.append(struct.pack(">7i", c, h, w, adv, top, left, 0))
        bitmaps.append(bm)
    data = head + b"".join(recs) + b"".join(bitmaps)
    (OUT / f"{name}.vlw").write_bytes(data)
    return len(data)


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    OUT.mkdir(exist_ok=True)
    total = 0
    for name, file, size, chars, tabular in FONTS:
        n = build(name, src / file, size, chars, tabular)
        total += n
        print(f"{name:7} {file:20} {size:3}px  {n / 1024:6.1f} KB")
    print(f"total {total / 1024:.0f} KB")


if __name__ == "__main__":
    main()
