"""Keep payment-card data out of Captain: find card numbers, CVVs and expiry
dates in text and replace them before the text is stored, shown to a model, or
echoed anywhere.

Captain never needs a card number: payments are approved in the user's bank app
(docs/payments-phase0-plan.md). A card number is a 13–19 digit run, grouped the
way cards are printed, that passes the Luhn check and starts with a real card
network's prefix. Payment references and IBANs survive: digits glued to letters
are never a card, and a group of digits right after an IBAN's country + check
digits (``DE89 …``) or a Croatian model (``HR01 …``) is left alone.

flight-deck/src/lib/cardScrub.ts is the same rules for the browser; both are
held to tests/fixtures/card_scrub_cases.json.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NOTE = ("[Note: a payment card number was removed from this message. "
        "Captain never needs card numbers or CVVs.]")

# A digit run with optional single space/dash separators, not glued to letters
# or other digits.
_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9])\d(?:[ \-]?\d){11,18}(?![A-Za-z0-9])")
_CVV_LABEL_RE = re.compile(
    r"(?i)\b(?:cvv2?|cvc2?|csc|cid|sigurnosni\s+(?:kod|broj))\b\s*(?:[:=#]|je|is)?\s*(\d{3,4})\b")
_EXPIRY_RE = re.compile(r"(?<![\d/])(0[1-9]|1[0-2])\s?/\s?(\d{2}|20\d{2})(?![\d/])")
_EXPIRY_LABEL_RE = re.compile(r"(?i)\b(?:exp(?:iry|ires)?|valid\s+thru|vrijedi\s+do|istek)\b[:.\s]*$")
_REF_CONTEXT_RE = re.compile(r"(?i)(?:[A-Z]{2}\d{2}[ \-]?|poziv\s+na\s+broj\s*:?\s*)$")
_BARE_CVV_RE = re.compile(r"(?<![\d/.,])\d{3,4}(?![\d/.,])")


@dataclass(frozen=True)
class Finding:
    kind: str        # "pan" | "cvv" | "expiry"
    start: int
    end: int
    last4: str = ""


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _network_ok(digits: str) -> bool:
    """A real card network's prefix at a length that network issues."""
    n = len(digits)
    p2, p3, p4 = int(digits[:2]), int(digits[:3]), int(digits[:4])
    if digits[0] == "4":                                   # Visa
        return n in (13, 16, 19)
    if 51 <= p2 <= 55 or 2221 <= p4 <= 2720:               # Mastercard
        return n == 16
    if p2 in (34, 37):                                     # Amex
        return n == 15
    if p4 == 6011 or 644 <= p3 <= 649 or p2 == 65:         # Discover
        return 16 <= n <= 19
    if 3528 <= p4 <= 3589:                                 # JCB
        return 16 <= n <= 19
    if 300 <= p3 <= 305 or p2 in (36, 38, 39):             # Diners
        return 14 <= n <= 19
    if p2 == 62:                                           # UnionPay
        return 16 <= n <= 19
    if p2 == 50 or 56 <= p2 <= 69:                         # Maestro
        return 12 <= n <= 19
    return False


def _grouping_ok(raw: str) -> bool:
    """Run together, or grouped the way cards are printed (4-4-4-4, 4-4-4-4-3,
    4-4-4-1, Amex 4-6-5, Diners 4-6-4), with one kind of separator."""
    seps = set(re.findall(r"[ \-]", raw))
    if not seps:
        return True
    if len(seps) > 1:
        return False
    groups = [len(g) for g in re.split(r"[ \-]", raw)]
    if groups in ([4, 6, 5], [4, 6, 4]):
        return True
    return len(groups) >= 2 and all(g == 4 for g in groups[:-1]) and 1 <= groups[-1] <= 4


def find(text: str) -> list[Finding]:
    """Card numbers, CVVs and expiry dates in *text*, in order."""
    text = str(text or "")
    found: list[Finding] = []
    for m in _CANDIDATE_RE.finditer(text):
        if _REF_CONTEXT_RE.search(text[max(0, m.start() - 20):m.start()]):
            continue
        raw = m.group(0)
        # The run may carry what follows the card (a CVV, a year): try the
        # longest leading groups that make a card number.
        ends = [g.end() for g in re.finditer(r"\d+", raw)]
        for end in reversed(ends):
            part = raw[:end]
            digits = re.sub(r"\D", "", part)
            if (12 <= len(digits) <= 19 and _grouping_ok(part) and luhn_ok(digits)
                    and _network_ok(digits)):
                found.append(Finding("pan", m.start(), m.start() + end, digits[-4:]))
                break
    pans = list(found)

    def near_pan(start: int, end: int, reach: int) -> bool:
        return any(start - reach <= p.end and end + reach >= p.start for p in pans)

    def taken(start: int, end: int) -> bool:
        return any(start < f.end and end > f.start for f in found)

    for m in _EXPIRY_RE.finditer(text):
        labelled = bool(_EXPIRY_LABEL_RE.search(text[max(0, m.start() - 20):m.start()]))
        if (labelled or near_pan(m.start(), m.end(), 40)) and not taken(m.start(), m.end()):
            found.append(Finding("expiry", m.start(), m.end()))
    for m in _CVV_LABEL_RE.finditer(text):
        if not taken(m.start(1), m.end(1)):
            found.append(Finding("cvv", m.start(1), m.end(1)))
    # A lone 3-4 digit number on the same line as a card number is its CVV.
    for pan in pans:
        line_start = text.rfind("\n", 0, pan.start) + 1
        line_end = text.find("\n", pan.end)
        line_end = len(text) if line_end < 0 else line_end
        for m in _BARE_CVV_RE.finditer(text, line_start, line_end):
            if not taken(m.start(), m.end()):
                found.append(Finding("cvv", m.start(), m.end()))
    return sorted(found, key=lambda f: f.start)


def _replacement(f: Finding) -> str:
    if f.kind == "pan":
        return f"[card number removed · ends {f.last4}]"
    return "[CVV removed]" if f.kind == "cvv" else "[expiry removed]"


def scrub(text: str) -> tuple[str, list[Finding]]:
    """*text* with every finding replaced, and the findings."""
    text = str(text or "")
    found = find(text)
    out = text
    for f in sorted(found, key=lambda f: f.start, reverse=True):
        out = out[:f.start] + _replacement(f) + out[f.end:]
    return out, found


def scrub_user_text(text: str) -> str:
    """Scrub a person's message; when something was removed, say so at the end
    so the model can tell them why."""
    if not text or not any(ch.isdigit() for ch in str(text)):
        return text
    out, found = scrub(text)
    if not found:
        return text
    return f"{out}\n\n{NOTE}"


def looks_like_card_data(value: str) -> bool:
    return bool(find(str(value or "")))
