"""The warnings a prepared payment carries, and the status they put it in.

Severity: ``error`` — it can't be paid as it stands (fix the fields with a new
prepare); ``blocking`` — only the signed-in owner can clear it, in Flight Deck,
never a tool argument; ``info`` — shown, nothing more.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from captain_claw import card_scrub
from captain_claw.payments import grounding, hr_ref, iban as iban_mod

ERROR, BLOCKING, INFO = "error", "blocking", "info"
REQUIRED = ("payee_name", "iban", "amount_cents")
DUPLICATE_DAYS = 60
DUPLICATE_DAYS_NO_REFERENCE = 7
UNUSUAL_FACTOR = 3
DEFAULT_CAP_CENTS = 500_000
_CLOSED = ("cancelled", "skipped", "expired")
_MONEY_FIELDS = ("iban", "amount_cents", "reference", "model")
_TEXT_FIELDS = ("payee_name", "payee_street", "payee_city", "payee_oib", "iban", "model",
                "reference", "purpose_code", "description")

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:j\.?\s?d\.?\s?o\.?\s?o|d\.?\s?o\.?\s?o|d\.?\s?d|obrt|gmbh|ag|ltd|llc|inc|s\.?r\.?l|s\.?a)\b\.?",
    re.IGNORECASE)


@dataclass(frozen=True)
class PaymentWarning:
    code: str
    severity: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def format_amount(cents: int, currency: str = "EUR") -> str:
    return f"{int(cents) / 100:,.2f} {currency or 'EUR'}"


def payee_key(name: str, oib: str = "") -> str:
    """One payee across invoices: the OIB when there is one, else the name
    without case, punctuation or legal-form suffixes."""
    digits = re.sub(r"\D", "", str(oib or ""))
    if len(digits) == 11:
        return f"oib:{digits}"
    plain = _LEGAL_SUFFIX_RE.sub(" ", str(name or "").casefold())
    plain = " ".join(re.sub(r"[^\w\s]", " ", plain).split())
    return f"name:{plain}" if plain else ""


def missing(fields: dict) -> list[str]:
    return [name for name in REQUIRED if not fields.get(name)]


def barcodes_for(fields: dict) -> list[str]:
    """Which barcodes a payment gets: a Croatian model reference only travels
    in HUB3 (EPC has no model field, so the payee couldn't match it)."""
    account = iban_mod.normalize(fields.get("iban", ""))
    if (fields.get("currency") or "EUR") != "EUR" or not iban_mod.is_sepa(account):
        return []
    if not account.startswith("HR"):
        return ["epc"]
    model = hr_ref.normalize_model(fields.get("model", ""))
    if model and model != hr_ref.NO_REFERENCE_MODEL:
        return ["hub3"]
    return ["hub3", "epc"]


def _day(value) -> date | None:
    if isinstance(value, (int, float)) and value:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _same_reference(a: str, b: str) -> bool:
    return re.sub(r"[\s\-]", "", str(a or "")).upper() == re.sub(r"[\s\-]", "", str(b or "")).upper()


def evaluate(
    fields: dict,
    provenance: dict,
    *,
    intent_id: str = "",
    source_sha256: str = "",
    history: list[dict] = (),
    others: list[dict] = (),
    cap_cents: int = DEFAULT_CAP_CENTS,
    today: date | None = None,
    mismatches: list[tuple[str, object, object]] = (),
    not_in_document: list[str] = (),
    text_notes: list[str] = (),
) -> list[PaymentWarning]:
    """Every warning for a payment. *history*: the owner's paid payments
    (dicts with payee_key, iban, amount_cents, paid_on); *others*: their other
    payments, any status (id, status, iban, amount_cents, reference,
    source_sha256, created_on)."""
    today = today or date.today()
    out: list[PaymentWarning] = []
    name = str(fields.get("payee_name") or "this payee")
    account = iban_mod.normalize(fields.get("iban", ""))
    cents = int(fields.get("amount_cents") or 0)

    for field in _TEXT_FIELDS:
        if card_scrub.looks_like_card_data(str(fields.get(field) or "")):
            out.append(PaymentWarning("card_data", ERROR, (
                f"The {grounding.label(field)} looks like payment-card data. Captain never takes "
                "card numbers or CVVs: payments are approved in your bank app.")))
            break

    if account:
        verdict = iban_mod.check(account)
        if not verdict.ok:
            out.append(PaymentWarning("iban_invalid", ERROR, f"The IBAN isn't valid: {verdict.reason}."))
        elif not iban_mod.is_sepa(account):
            out.append(PaymentWarning("not_sepa", ERROR, (
                f"{verdict.country} isn't in SEPA, so this can't be paid with a bank-app barcode.")))
    currency = str(fields.get("currency") or "EUR").upper()
    if currency != "EUR":
        out.append(PaymentWarning("non_eur", ERROR, f"Only EUR payments can be prepared for now, not {currency}."))

    if account.startswith("HR") and (fields.get("model") or fields.get("reference")):
        ref = hr_ref.check(fields.get("model", ""), fields.get("reference", ""))
        if not ref.ok:
            out.append(PaymentWarning("reference_format", INFO, (
                f"The model / poziv na broj looks off: {ref.reason}. The payee may not be able to "
                "match the payment — check it against the invoice.")))

    for field, from_barcode, from_model in mismatches:
        # The barcode's value is used either way; a differing name or text is
        # just an abbreviation, a differing account or amount may be a tampered
        # barcode (a sticker over the payee's own).
        if field not in _MONEY_FIELDS:
            continue
        shown_b = format_amount(int(from_barcode)) if field == "amount_cents" else from_barcode
        shown_m = format_amount(int(from_model)) if field == "amount_cents" else from_model
        out.append(PaymentWarning("barcode_mismatch", BLOCKING, (
            f"The invoice's own barcode has {grounding.label(field)} {shown_b}, but {shown_m} was "
            "read. The barcode's value is used — check which one is right.")))
    for field in not_in_document:
        value = fields.get(field)
        shown = format_amount(int(value)) if field == "amount_cents" else value
        out.append(PaymentWarning("not_in_document", BLOCKING, (
            f"The {grounding.label(field)} {shown} isn't in the document's text, so it may be "
            "misread. Check it against the invoice.")))
    read_only = [f for f in grounding.CRITICAL
                 if provenance.get(f) == "model" and f not in not_in_document and fields.get(f)]
    if read_only:
        names = ", ".join(grounding.label(f) for f in read_only)
        out.append(PaymentWarning("model_read_only", INFO, (
            f"The {names} {'was' if len(read_only) == 1 else 'were'} read by AI from a picture — "
            "check against the invoice before you pay.")))

    key = payee_key(name, fields.get("payee_oib", ""))
    prior = [h for h in history if key and h.get("payee_key") == key]
    known_ibans = {iban_mod.normalize(h.get("iban", "")) for h in prior}
    if prior and account and account not in known_ibans:
        last = max(prior, key=lambda h: str(h.get("paid_on") or ""))
        out.append(PaymentWarning("iban_changed", BLOCKING, (
            f"You paid {name} to {iban_mod.grouped(last.get('iban', ''))} before"
            f"{' (' + str(last.get('paid_on'))[:10] + ')' if last.get('paid_on') else ''}. This "
            f"invoice asks for {iban_mod.grouped(account)}. Invoice fraud usually changes only the "
            f"IBAN — confirm with {name} by phone, using a number you already know, before paying.")))
    elif not prior and account and not any(iban_mod.normalize(h.get("iban", "")) == account for h in history):
        out.append(PaymentWarning("new_payee", INFO, f"First payment to {name} through Captain."))

    for other in others:
        if other.get("id") == intent_id or other.get("status") in _CLOSED:
            continue
        when = _day(other.get("created_on"))
        age = (today - when).days if when else 0
        same_source = bool(source_sha256) and other.get("source_sha256") == source_sha256
        same_payment = (
            account and iban_mod.normalize(other.get("iban", "")) == account
            and int(other.get("amount_cents") or 0) == cents
        )
        ref, other_ref = fields.get("reference", ""), other.get("reference", "")
        if same_payment and ref and other_ref:
            same_payment = _same_reference(ref, other_ref) and age <= DUPLICATE_DAYS
        elif same_payment:
            same_payment = not ref and not other_ref and age <= DUPLICATE_DAYS_NO_REFERENCE
        if same_source or same_payment:
            state = "paid" if other.get("status") in ("user_paid", "settled") else "prepared"
            what = "invoice file" if same_source else "IBAN, amount and reference"
            out.append(PaymentWarning("possible_duplicate", BLOCKING, (
                f"This looks like a payment already {state}"
                f"{' on ' + when.isoformat() if when else ''} (same {what}). Paying twice is easy "
                "and slow to undo.")))
            break

    if cap_cents and cents > cap_cents:
        out.append(PaymentWarning("over_cap", BLOCKING, (
            f"{format_amount(cents)} is over your limit of {format_amount(cap_cents)}. You can "
            "change the limit on the Payments page.")))
    amounts = [int(h.get("amount_cents") or 0) for h in prior if h.get("amount_cents")]
    if len(amounts) >= 2 and cents > UNUSUAL_FACTOR * statistics.median(amounts):
        out.append(PaymentWarning("amount_unusual", INFO, (
            f"{format_amount(cents)} is much more than you usually pay {name} "
            f"(typically {format_amount(int(statistics.median(amounts)))}).")))

    due = _day(fields.get("due_date"))
    if due and due < today:
        out.append(PaymentWarning("overdue", INFO, f"The due date ({due.isoformat()}) has passed."))
    if text_notes:
        out.append(PaymentWarning("text_adjusted", INFO,
                                  "Changed to fit the barcode: " + "; ".join(text_notes) + "."))
    return out


def status_for(fields: dict, warnings: list[dict], acked: set[str] | frozenset[str] = frozenset()) -> str:
    """draft (missing fields or an error), needs_review (a blocking warning not
    yet cleared by the owner), or ready."""
    if missing(fields) or any(w.get("severity") == ERROR for w in warnings):
        return "draft"
    if any(w.get("severity") == BLOCKING and w.get("code") not in acked for w in warnings):
        return "needs_review"
    return "ready"
