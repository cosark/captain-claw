"""Where each field of a payment came from, and whether the document backs it.

Order of trust: a barcode the payee printed, then the document's text, then the
model's reading alone. The critical fields (IBAN, amount, reference) a model
gives must appear in the document's text when there is text; a value the
barcode holds always wins over the model's."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from captain_claw.payments import hr_ref, iban as iban_mod

CRITICAL = ("iban", "amount_cents", "reference")
FIELDS = (
    "payee_name", "payee_street", "payee_city", "payee_oib", "iban", "amount_cents",
    "currency", "model", "reference", "purpose_code", "description", "due_date",
)
_LABELS = {"iban": "IBAN", "amount_cents": "amount", "reference": "poziv na broj"}


def _squash(text: str) -> str:
    return re.sub(r"\s", "", str(text or "").replace(" ", " ")).upper()


def amount_forms(cents: int) -> list[str]:
    """How an amount is written on invoices: 1234,55 · 1.234,55 · 1 234,55 ·
    1234.55 · 1,234.55."""
    euros, rest = divmod(int(cents), 100)
    plain, grouped, r = str(euros), f"{euros:,}", f"{rest:02d}"
    forms = {
        f"{plain},{r}", f"{plain}.{r}",
        f"{grouped.replace(',', '.')},{r}", f"{grouped}.{r}",
        f"{grouped.replace(',', ' ')},{r}", f"{grouped.replace(',', ' ')}.{r}",
    }
    return sorted(forms, key=len, reverse=True)


def iban_in(text: str, value: str) -> bool:
    account = iban_mod.normalize(value)
    return bool(account) and account in _squash(text)


def amount_in(text: str, cents: int) -> bool:
    body = str(text or "").replace(" ", " ").replace(" ", " ")
    for form in amount_forms(cents):
        if re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d]|[.,]\d)", body):
            return True
    return False


def reference_in(text: str, value: str) -> bool:
    ref = hr_ref.normalize_reference(value)
    if not ref:
        return True
    squashed = _squash(text)
    if ref.upper() in squashed:
        return True
    digits = re.sub(r"\D", "", ref)
    return len(digits) >= 6 and digits in re.sub(r"[\s\-]", "", str(text or ""))


def _loosely_in(text: str, value: str) -> bool:
    words = " ".join(str(value or "").casefold().split())
    return bool(words) and words in " ".join(str(text or "").casefold().split())


def _same(name: str, a, b) -> bool:
    if name == "iban":
        return iban_mod.normalize(a) == iban_mod.normalize(b)
    if name == "amount_cents":
        return int(a or 0) == int(b or 0)
    if name == "reference":
        return re.sub(r"[\s\-]", "", str(a or "")).upper() == re.sub(r"[\s\-]", "", str(b or "")).upper()
    if name == "model":
        return hr_ref.normalize_model(a) == hr_ref.normalize_model(b)
    return " ".join(str(a or "").casefold().split()) == " ".join(str(b or "").casefold().split())


@dataclass
class Merged:
    fields: dict
    provenance: dict
    mismatches: list[tuple[str, object, object]] = field(default_factory=list)   # (field, barcode, model)
    not_in_document: list[str] = field(default_factory=list)


def _empty(value) -> bool:
    return value is None or value == "" or (isinstance(value, int) and value == 0)


def merge(model_fields: dict, barcode_fields: dict | None, document_text: str,
          *, user_fields: frozenset[str] = frozenset()) -> Merged:
    """Combine what the model read with the barcode and the document text.
    *user_fields* are fields the user typed themselves (provenance 'user')."""
    barcode = {k: v for k, v in (barcode_fields or {}).items() if k in FIELDS and not _empty(v)}
    out = Merged(fields={}, provenance={})
    for name, value in barcode.items():
        out.fields[name] = value
        out.provenance[name] = "barcode"
    text = str(document_text or "")
    for name in FIELDS:
        value = model_fields.get(name)
        if _empty(value):
            continue
        if name in barcode:
            if not _same(name, barcode[name], value):
                out.mismatches.append((name, barcode[name], value))
            continue
        out.fields[name] = value
        if name in user_fields:
            out.provenance[name] = "user"
        elif not text.strip():
            out.provenance[name] = "model"
        elif name in CRITICAL:
            backed = {"iban": iban_in, "amount_cents": amount_in, "reference": reference_in}[name](text, value)
            out.provenance[name] = "document" if backed else "model"
            if not backed:
                out.not_in_document.append(name)
        else:
            out.provenance[name] = "document" if _loosely_in(text, str(value)) else "model"
    return out


def label(name: str) -> str:
    return _LABELS.get(name, name.replace("_", " "))
