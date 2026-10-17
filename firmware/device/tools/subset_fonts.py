"""Trimmed copies of M5GFX's efontJA bitmap fonts for the StickS3 screen.

    python3 tools/subset_fonts.py [path to M5GFX's lgfx_efont_ja.c]

efontJA covers ~10,000 glyphs (Japanese), about 1.9 MB for the four sizes the
stick uses; the screens only draw Latin text, punctuation, a few symbols and —
for the Matrix rain — katakana. This keeps those glyphs, byte for byte, in
the same u8g2 layout M5GFX reads (header, 8-bit glyphs, unicode lookup table,
unicode glyphs), and writes src/efont_subset.{h,cpp}. Every kept glyph is
checked to come back identical through a Python copy of M5GFX's lookup
(U8g2font::getGlyph).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT / ".pio/libdeps/sticks3/M5GFX/src/lgfx/Fonts/efont/lgfx_efont_ja.c"


def span(a: int, b: int) -> list[int]:
    return list(range(a, b + 1))


# What the stick's screens draw: ASCII, Latin-1, Latin Extended-A (č ć š ž đ),
# Romanian comma letters, punctuation, arrows, a few symbols.
TEXT = sorted(set(
    span(0x20, 0x7E) + span(0xA0, 0xFF) + span(0x100, 0x17F) + span(0x218, 0x21B)
    + span(0x2010, 0x2015) + span(0x2018, 0x201E) + span(0x2020, 0x2022)
    + [0x2026, 0x2030, 0x2032, 0x2033, 0x2039, 0x203A, 0x20AC, 0x2122]
    + span(0x2190, 0x2193) + [0x2212, 0x2248, 0x2264, 0x2265, 0x2713]
    + [0x25A0, 0x25A1, 0x25B2, 0x25B6, 0x25BC, 0x25C0, 0x25CB, 0x25CF]))
KATAKANA = span(0x30A1, 0x30F6)  # the Matrix rain

# (name in the C source, our name, code points)
FONTS = [
    ("lgfx_efont_ja_12", "efont12", TEXT + KATAKANA),
    ("lgfx_efont_ja_14", "efont14", TEXT),
    ("lgfx_efont_ja_16", "efont16", TEXT),
    ("lgfx_efont_ja_24", "efont24", TEXT),
]
LUT_BLOCK = 100  # glyphs per lookup-table entry (as u8g2's bdfconv does)


# ── reading the C source ─────────────────────────────────────────────────

def c_arrays(path: Path) -> dict[str, bytes]:
    src = path.read_text(errors="ignore")
    out = {}
    for m in re.finditer(r"(\w+)\s*\[\s*(\d*)\s*\]\s*=\s*((?:\s*\"(?:[^\"\\]|\\.)*\")+)\s*;", src):
        raw = "".join(re.findall(r"\"((?:[^\"\\]|\\.)*)\"", m.group(3)))
        data = bytearray()
        i = 0
        while i < len(raw):
            c = raw[i]
            if c == "\\":
                n = raw[i + 1]
                if n in "01234567":
                    j, d = i + 1, ""
                    while j < len(raw) and len(d) < 3 and raw[j] in "01234567":
                        d += raw[j]
                        j += 1
                    data.append(int(d, 8))
                    i = j
                    continue
                data.append({"n": 10, "t": 9, "r": 13, "\\": 92, "\"": 34, "'": 39, "?": 63,
                             "a": 7, "b": 8, "f": 12, "v": 11}[n])
                i += 2
                continue
            data.append(ord(c))
            i += 1
        # A C string literal ends in a NUL the source doesn't spell out; u8g2
        # fonts rely on it as their last terminator byte.
        size = int(m.group(2)) if m.group(2) else len(data) + 1
        out[m.group(1)] = bytes(data) + b"\x00" * max(0, size - len(data))
    return out


# ── the u8g2 layout ──────────────────────────────────────────────────────

def u16(d: bytes, i: int) -> int:
    return d[i] << 8 | d[i + 1]


def glyphs(font: bytes) -> tuple[dict[int, bytes], dict[int, bytes]]:
    """(8-bit records, unicode records) — each the whole record, header included."""
    body = font[23:]
    low, p = {}, 0
    while body[p + 1]:
        low[body[p]] = body[p:p + body[p + 1]]
        p += body[p + 1]
    uni_start = u16(font, 21)
    lut = uni_start
    q = uni_start
    while True:  # walk the lookup table to the first glyph
        q += u16(body, lut)
        e = u16(body, lut + 2)
        lut += 4
        if lut - uni_start == 4:
            first = q
        if e == 0xFFFF:
            break
    high, p = {}, first
    while True:
        e = u16(body, p)
        if e == 0:
            break
        high[e] = body[p:p + body[p + 2]]
        p += body[p + 2]
    return low, high


def get_glyph(font: bytes, encoding: int) -> bytes | None:
    """M5GFX's U8g2font::getGlyph, transliterated (returns the glyph's bitmap bytes)."""
    body = font[23:]
    p = 0
    if encoding <= 255:
        if encoding >= ord("a"):
            p += u16(font, 19)
        elif encoding >= ord("A"):
            p += u16(font, 17)
        while body[p + 1]:
            if body[p] == encoding:
                return body[p + 2:p + body[p + 1]]
            p += body[p + 1]
        return None
    p += u16(font, 21)
    lut = p
    while True:
        p += u16(body, lut)
        e = u16(body, lut + 2)
        lut += 4
        if e >= encoding:
            break
    while True:
        e = u16(body, p)
        if e == 0:
            return None
        if e == encoding:
            return body[p + 3:p + body[p + 2]]
        p += body[p + 2]


def subset(font: bytes, keep: list[int]) -> bytes:
    low, high = glyphs(font)
    keep_low = [c for c in sorted(set(keep)) if c <= 255 and c in low]
    keep_high = [c for c in sorted(set(keep)) if c > 255 and c in high]
    body = bytearray()
    pos_A = pos_a = None
    for c in keep_low:
        if pos_A is None and c >= ord("A"):
            pos_A = len(body)
        if pos_a is None and c >= ord("a"):
            pos_a = len(body)
        body += low[c]
    end_low = len(body)
    body += b"\x00\x00"  # end of the 8-bit glyphs (a zero record size)
    uni_start = len(body)
    blocks = [keep_high[i:i + LUT_BLOCK] for i in range(0, len(keep_high), LUT_BLOCK)] or [[]]
    lut = bytearray()
    for k, block in enumerate(blocks):
        step = 4 * len(blocks) if k == 0 else sum(len(high[c]) for c in blocks[k - 1])
        last = 0xFFFF if k == len(blocks) - 1 else block[-1]
        lut += bytes([step >> 8, step & 0xFF, last >> 8, last & 0xFF])
    body += lut
    for c in keep_high:
        body += high[c]
    body += b"\x00\x00"  # end of the unicode glyphs
    head = bytearray(font[:23])
    head[0] = (len(keep_low) + len(keep_high)) & 0xFF
    for off, val in ((17, pos_A if pos_A is not None else end_low),
                     (19, pos_a if pos_a is not None else end_low),
                     (21, uni_start)):
        head[off], head[off + 1] = val >> 8, val & 0xFF
    out = bytes(head + body)
    # Every kept glyph must come back identical through M5GFX's lookup.
    for c in keep_low + keep_high:
        assert get_glyph(out, c) == get_glyph(font, c), f"glyph U+{c:04X} differs"
    assert get_glyph(out, 0x4E00) is None or 0x4E00 in keep_high
    return out


def c_bytes(name: str, data: bytes) -> str:
    rows = [", ".join(f"0x{b:02x}" for b in data[i:i + 20]) for i in range(0, len(data), 20)]
    return f"static const uint8_t {name}_data[{len(data)}] PROGMEM = {{\n  " + ",\n  ".join(rows) + "\n};\n"


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    arrays = c_arrays(src)
    parts, total_in, total_out = [], 0, 0
    for orig, name, keep in FONTS:
        font = arrays[orig]
        out = subset(font, keep)
        total_in += len(font)
        total_out += len(out)
        low, high = glyphs(out)
        print(f"{name}: {len(font) // 1024} KB → {len(out) // 1024} KB ({len(low) + len(high)} glyphs)")
        parts.append((name, out))
    header = (
        "// Generated by tools/subset_fonts.py — do not edit. Trimmed copies of M5GFX's\n"
        "// efontJA (Latin, punctuation, symbols; katakana in efont12 for the Matrix rain).\n"
        "#pragma once\n\n#include <M5GFX.h>\n\nnamespace efont {\n\n"
        + "".join(f"extern const lgfx::U8g2font {n};\n" for n, _ in parts)
        + "\n}  // namespace efont\n")
    body = (
        "// Generated by tools/subset_fonts.py — do not edit.\n"
        "// efont: (c) The /efont/ team, BSD-style licence (see M5GFX's lgfx/Fonts/efont).\n"
        "#include \"efont_subset.h\"\n\nnamespace efont {\n\n"
        + "\n".join(c_bytes(n, d) for n, d in parts)
        + "\n" + "".join(f"const lgfx::U8g2font {n} = {{{n}_data}};\n" for n, _ in parts)
        + "\n}  // namespace efont\n")
    (ROOT / "src/efont_subset.h").write_text(header)
    (ROOT / "src/efont_subset.cpp").write_text(body)
    print(f"total {total_in // 1024} KB → {total_out // 1024} KB")


if __name__ == "__main__":
    main()
