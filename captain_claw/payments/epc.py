"""The EPC QR code ("GiroCode") SEPA bank apps scan to fill in a credit transfer.

Format from EPC069-12 v3.0 (13 September 2022): version 002, UTF-8, LF between
elements, nothing after the last populated one; a structured RF reference or
free text, never both; at most 331 bytes; error correction M, QR version ≤ 13.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from captain_claw.payments import hr_ref, iban as iban_mod

MAX_BYTES = 331
MAX_VERSION = 13


@dataclass(frozen=True)
class Epc:
    payload: str
    notes: tuple[str, ...] = ()


def _one_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _cut_bytes(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")[:max(0, max_bytes)]
    return data.decode("utf-8", errors="ignore").rstrip()


def amount_text(amount_cents: int) -> str:
    cents = int(amount_cents)
    return f"EUR{cents // 100}.{cents % 100:02d}"


def build_payload(
    *,
    name: str,
    iban: str,
    amount_cents: int,
    reference: str = "",
    text: str = "",
    purpose: str = "",
    info: str = "",
    bic: str = "",
) -> Epc:
    """The text the QR encodes. An RF *reference* goes in the structured
    element; anything else travels as remittance text."""
    cents = int(amount_cents)
    if not 1 <= cents <= 99_999_999_999:
        raise ValueError("an EPC QR amount is between 0.01 and 999999999.99 EUR")
    account = iban_mod.normalize(iban)
    notes: list[str] = []
    payee = _one_line(name)
    if len(payee) > 70:
        notes.append("payee name cut to 70 characters")
        payee = payee[:70].rstrip()
    structured = hr_ref.normalize_reference(reference) if hr_ref.is_rf(reference) else ""
    remittance = _one_line(text)
    if reference and not structured:
        remittance = _one_line(f"{reference} {remittance}")
    if structured:
        remittance = ""
    if len(remittance) > 140:
        notes.append("payment text cut to 140 characters")
        remittance = remittance[:140].rstrip()
    elements = [
        "BCD", "002", "1", "SCT", re.sub(r"\s", "", bic or "").upper(), payee, account,
        amount_text(cents), re.sub(r"[^A-Z]", "", (purpose or "").upper())[:4],
        structured, remittance, _one_line(info)[:70],
    ]
    while elements and not elements[-1]:
        elements.pop()
    payload = "\n".join(elements)
    over = len(payload.encode("utf-8")) - MAX_BYTES
    if over > 0 and remittance:
        cut = _cut_bytes(remittance, len(remittance.encode("utf-8")) - over)
        notes.append("payment text shortened to fit the QR code")
        elements[10] = cut
        while elements and not elements[-1]:
            elements.pop()
        payload = "\n".join(elements)
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise ValueError("the payment doesn't fit an EPC QR code")
    return Epc(payload, tuple(notes))


def parse(text: str) -> dict | None:
    """Fields of a decoded EPC QR, or None when *text* isn't one."""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    if len(lines) < 7 or lines[0].strip() != "BCD" or lines[3].strip() != "SCT":
        return None
    lines = (lines + [""] * 12)[:12]
    amount = lines[7].strip().upper()
    cents = 0
    m = re.fullmatch(r"EUR(\d+)(?:\.(\d{1,2}))?", amount)
    if m:
        cents = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
    return {
        "currency": "EUR" if amount.startswith("EUR") or not amount else amount[:3],
        "bic": lines[4].strip(), "payee_name": lines[5].strip(),
        "iban": iban_mod.normalize(lines[6]), "amount_cents": cents,
        "purpose_code": lines[8].strip(), "reference": lines[9].strip(),
        "description": lines[10].strip(), "info": lines[11].strip(),
    }


def render_png(payload: str, *, scale: int = 8) -> bytes:
    import segno

    qr = segno.make_qr(payload, error="m", mode="byte", encoding="utf-8", boost_error=False)
    if int(qr.version) > MAX_VERSION:
        raise ValueError("the payment doesn't fit an EPC QR code")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=4)
    return buf.getvalue()
