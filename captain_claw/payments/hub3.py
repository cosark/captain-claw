"""The HUB3 PDF417 barcode Croatian bank apps scan to fill in a payment.

Format from HUB's "Uputa o upotrebi PDF417 2D bar koda … (obrazac HUB3A)",
version 6 (September 2022), for EUR slips: 14 fields, each followed by LF,
UTF-8, lengths in characters; the amount is cents padded to 15 digits. The
symbol: 9 data columns, error-correction level 4, binary (byte) compaction,
module height:width 3:1, no Compact or Macro PDF417.
"""

from __future__ import annotations

import contextlib
import io
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Iterator

from captain_claw.payments import hr_ref, iban as iban_mod

HEADER = "HRVHUB30"
CURRENCY = "EUR"

# (field, max characters), in barcode order after the header, currency, amount.
TEXT_FIELDS: tuple[tuple[str, int], ...] = (
    ("payer_name", 30), ("payer_street", 27), ("payer_city", 27),
    ("payee_name", 25), ("payee_street", 25), ("payee_city", 27),
    ("iban", 21), ("model", 4), ("reference", 22), ("purpose_code", 4),
    ("description", 35),
)
_LABELS = {
    "payer_name": "payer", "payer_street": "payer street", "payer_city": "payer city",
    "payee_name": "payee", "payee_street": "payee street", "payee_city": "payee city",
    "reference": "poziv na broj", "description": "description",
}

# Allowed: digits, the Croatian alphabet plus Q W X Y (both cases), space and , . : - + ? ' / ( )
_ALLOWED = set("0123456789 ,.:-+?'/()") | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ") \
    | set("abcdefghijklmnopqrstuvwxyz") | set("ČĆĐŠŽčćđšž")
_SUBSTITUTES = {
    "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "œ": "oe", "Œ": "OE",
    "&": "+", "\"": "'", "„": "'", "“": "'", "”": "'", "’": "'", "‘": "'", "`": "'",
    "–": "-", "—": "-", "_": "-", ";": ",", "!": ".",
}


def clean_text(value: str, limit: int) -> tuple[str, bool]:
    """Fit *value* to the HUB3 character set and *limit*: transliterate what has
    a plain form (ä → a, ß → ss), turn the rest into spaces, cut to length.
    Returns the text and whether it changed (beyond trimming spaces)."""
    original = " ".join(str(value or "").split())
    out: list[str] = []
    for ch in original:
        if ch in _ALLOWED:
            out.append(ch)
        elif ch in _SUBSTITUTES:
            out.append(_SUBSTITUTES[ch])
        else:
            base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            out.append(base if base and all(c in _ALLOWED for c in base) else " ")
    text = " ".join("".join(out).split())[:limit].rstrip()
    return text, text != original


@dataclass(frozen=True)
class Hub3:
    payload: str
    notes: tuple[str, ...] = ()   # what had to change to fit the barcode


def build_payload(
    *,
    amount_cents: int,
    iban: str,
    payee_name: str,
    payee_street: str = "",
    payee_city: str = "",
    model: str = "",
    reference: str = "",
    purpose_code: str = "",
    description: str = "",
    payer_name: str = "",
    payer_street: str = "",
    payer_city: str = "",
) -> Hub3:
    """The text HUB3 encodes. Raises ValueError for what can't be a HUB3 slip."""
    cents = int(amount_cents)
    if not 0 <= cents < 10 ** 15:
        raise ValueError("the amount doesn't fit a HUB3 slip")
    account = iban_mod.normalize(iban)
    if not account.startswith("HR") or len(account) != 21:
        raise ValueError("HUB3 slips are for Croatian IBANs")
    values = {
        "payer_name": payer_name, "payer_street": payer_street, "payer_city": payer_city,
        "payee_name": payee_name, "payee_street": payee_street, "payee_city": payee_city,
        "iban": account, "model": hr_ref.normalize_model(model),
        "reference": hr_ref.normalize_reference(reference),
        "purpose_code": re.sub(r"[^A-Z]", "", str(purpose_code or "").upper())[:4],
        "description": description,
    }
    fields = [HEADER, CURRENCY, f"{cents:015d}"]
    notes: list[str] = []
    for name, limit in TEXT_FIELDS:
        text, changed = clean_text(values[name], limit)
        if changed and name in _LABELS:
            notes.append(f"{_LABELS[name]}: '{' '.join(str(values[name]).split())}' → '{text}'")
        fields.append(text)
    return Hub3("".join(f + "\n" for f in fields), tuple(notes))


def parse(text: str) -> dict | None:
    """Fields of a decoded HUB3 barcode, or None when *text* isn't one."""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != HEADER:
        return None
    lines = (lines + [""] * 14)[:14]
    amount = re.sub(r"\D", "", lines[2])
    names = [name for name, _ in TEXT_FIELDS]
    out: dict = {"currency": lines[1].strip().upper(), "amount_cents": int(amount) if amount else 0}
    for name, value in zip(names, lines[3:14]):
        out[name] = value.strip()
    return out


_COMPACTION_LOCK = threading.Lock()


@contextlib.contextmanager
def _byte_compaction() -> Iterator[None]:
    """pdf417gen picks text/numeric/byte compaction per character and has no
    switch for binary-only; HUB3 asks for binary, so swap its chooser for the
    byte compactor during one encode."""
    from pdf417gen import compaction

    with _COMPACTION_LOCK:
        original = compaction.get_optimal_compactor_fn
        compaction.get_optimal_compactor_fn = lambda _char: compaction.compact_bytes
        try:
            yield
        finally:
            compaction.get_optimal_compactor_fn = original


def data_codewords(payload: str) -> list[int]:
    """The data codewords HUB3 encodes (for tests: they start with the byte latch)."""
    from pdf417gen import compaction

    with _byte_compaction():
        return list(compaction.compact(payload.encode("utf-8")))


def render_png(payload: str, *, scale: int = 4) -> bytes:
    """The HUB3 symbol as a PNG (white background, quiet zone around it)."""
    import pdf417gen

    with _byte_compaction():
        codes = pdf417gen.encode(payload, columns=9, security_level=4, encoding="utf-8")
    image = pdf417gen.render_image(codes, scale=scale, ratio=3, padding=scale * 4)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
