"""Bank statement ↔ payment matching (Payments Phase 4, docs/payments-phase4-plan.md).

Pure helpers, no I/O. 4a uses the normalising and fingerprint helpers to store
outgoing transactions; 4b adds the matcher.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

_NON_DIGIT = re.compile(r"\D+")


def amount_cents(value) -> int | None:
    """A bank amount ("84.20", "-84.2", 84.2) as positive cents; None if unreadable."""
    try:
        cents = (abs(Decimal(str(value).strip().replace(",", "."))) * 100).quantize(Decimal(1))
    except (InvalidOperation, ValueError):
        return None
    return int(cents)


def reference_digits(*texts: str) -> str:
    """The digits of each reference text, one group per text: "HR01 7269-68499637766-00019"
    → "0172696849963776600019". A bill's reference is found when its own digits
    (model + reference) appear inside one group."""
    groups = [_NON_DIGIT.sub("", t or "") for t in texts]
    return " ".join(g for g in groups if g)[:200]


def bill_digits(model: str, reference: str) -> str:
    """A bill's model + reference as digits ("HR01", "7269-68499637766-00019" → "0172696849963776600019")."""
    return _NON_DIGIT.sub("", f"{model or ''}{reference or ''}")


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()


# ── the matcher (4b) ──────────────────────────────────────────────────

CANDIDATE_STATES = ("ready", "handed_off", "needs_review", "user_paid", "expired")
SETTLING = ("auto", "confirmed")
LOOK_BACK_DAYS = 45             # a payment booked this long before a bill with a reference was prepared
MIN_REFERENCE_DIGITS = 4        # shorter references would match anything
TWICE_WITHIN_DAYS = 20          # a second payment this soon after the first is "paid twice", not next month's
_NAME_STOP = frozenset({"doo", "jdoo", "dd", "obrt", "the", "ltd", "llc", "gmbh", "inc", "and", "za"})


@dataclass(frozen=True)
class Decision:
    """settle | pending | suggest | evidence | duplicate | reversed | not_seen"""
    kind: str
    fp: str
    intent_id: str
    reason: str = ""


def reference_of(model: str, reference: str) -> str:
    """The digits a statement must show for this bill, or '' when it has no usable reference."""
    if (model or "").upper() == "HR99":
        return ""
    digits = _NON_DIGIT.sub("", reference or "")
    return digits if len(digits) >= MIN_REFERENCE_DIGITS else ""


def name_tokens(name: str) -> frozenset[str]:
    words = re.findall(r"[^\W_]+", (name or "").casefold().replace(".", ""))
    return frozenset(w for w in words if len(w) >= 3 and w not in _NAME_STOP)


def tx_day(tx: dict) -> str:
    return tx.get("booking_date") or tx.get("value_date") or ""


def business_days_after(start: date, n: int) -> date:
    day = start
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def grade(intent: dict, tx: dict) -> tuple[str, str]:
    """(tier, reason) of one bill against one outgoing transaction.
    tier: exact | probable | before | '' (no match)."""
    if int(tx.get("amount_cents") or -1) != int(intent.get("amount_cents") or -2):
        return "", ""
    if str(tx.get("currency") or "EUR").upper() != "EUR":
        return "", ""
    tx_iban, bill_iban = tx.get("creditor_iban") or "", intent.get("iban") or ""
    if tx_iban and bill_iban and tx_iban != bill_iban:
        return "", ""                                   # a different payee
    ref = intent.get("ref_digits") or ""
    groups = (tx.get("reference_digits") or "").split()
    ref_found = bool(ref) and any(ref in g for g in groups)
    day, created = tx_day(tx), intent["created_day"]
    if not day:
        return "", ""
    try:
        booked = date.fromisoformat(day)
    except ValueError:
        return "", ""
    # Without a reference, a payment from before the bill existed is last period's.
    earliest = created - timedelta(days=LOOK_BACK_DAYS if ref_found else 1)
    if booked < earliest:
        return "", ""
    if tx_iban and tx_iban == bill_iban:
        if ref_found or not ref:
            tier, reason = "exact", "exact"
        elif not groups:
            tier, reason = "probable", "no_reference_on_statement"
        else:
            return "", ""                               # same payee, another bill's reference
    elif ref_found:
        tier, reason = "probable", "no_iban_on_statement"
    elif not ref and intent.get("name_tokens") and intent["name_tokens"] & name_tokens(tx.get("creditor_name", "")):
        tier, reason = "probable", "name_only"
    else:
        return "", ""
    if booked < created - timedelta(days=1):
        return "before", "before_prepared"
    return tier, reason


def _near_payment(bill: dict, tx: dict, first_day: str) -> bool:
    """A paid bill only claims transactions close to when it was paid."""
    anchor = first_day or (bill["paid_day"].isoformat() if bill.get("paid_day") else "")
    if not anchor:
        return False
    try:
        delta = (date.fromisoformat(tx_day(tx)) - date.fromisoformat(anchor)).days
    except ValueError:
        return False
    return -3 <= delta <= TWICE_WITHIN_DAYS


def match(intents: list[dict], transactions: list[dict], links: dict[tuple[str, str], str], *,
          today: date) -> list[Decision]:
    """Decisions for one owner. *intents*: dicts with id, status, iban, amount_cents,
    ref_digits, name_tokens, created_day (date) and paid_day (date | None).
    *links*: (fp, intent_id) → state from earlier runs (auto | confirmed |
    suggested | rejected | duplicate | pending).

    An open bill wins a transaction over a paid one; a paid bill only claims a
    transaction close to its first payment (its evidence, or a second payment)."""
    out: list[Decision] = []
    tx_by_fp = {t["fp"]: t for t in transactions}
    settled_fp: dict[str, str] = {}
    used: set[str] = set()
    for (fp, iid), state in links.items():
        if state in SETTLING:
            settled_fp.setdefault(iid, fp)
            used.add(fp)
            if (tx_by_fp.get(fp) or {}).get("status") in ("RJCT", "CNCL"):
                out.append(Decision("reversed", fp, iid))
        elif state == "duplicate":
            used.add(fp)
    live = [i for i in intents if i["status"] in CANDIDATE_STATES and i["id"] not in settled_fp]
    done = [i for i in intents if i["status"] == "settled" or i["id"] in settled_fp]
    same_payee = {}
    for i in live:
        key = (i.get("iban") or "", int(i.get("amount_cents") or 0))
        same_payee[key] = same_payee.get(key, 0) + 1

    for tx in sorted(transactions, key=lambda t: (tx_day(t), t["fp"])):
        fp = tx["fp"]
        if fp in used or tx.get("status") in ("RJCT", "CNCL") or tx_day(tx) > today.isoformat():
            continue
        booked = tx.get("status") == "BOOK"
        graded = []
        for i in live + done:
            state = links.get((fp, i["id"]))
            if state in ("rejected", "suggested"):
                continue
            if i in done and not _near_payment(i, tx, tx_day(tx_by_fp.get(settled_fp.get(i["id"], ""), {}))):
                continue
            tier, reason = grade(i, tx)
            if tier:
                graded.append((tier, reason, i))
        exact_live = [i for t, _, i in graded if t == "exact" and i in live]
        exact_done = [i for t, _, i in graded if t == "exact" and i in done]
        unique = (len(exact_live) == 1 and (exact_live[0].get("ref_digits")
                  or same_payee.get((exact_live[0].get("iban") or "", int(exact_live[0].get("amount_cents") or 0))) == 1))
        if unique:
            bill = exact_live[0]
            if not booked:
                out.append(Decision("pending", fp, bill["id"]))
                continue
            out.append(Decision("settle", fp, bill["id"]))
            used.add(fp)
            settled_fp[bill["id"]] = fp
            live.remove(bill)
            done.append(bill)
            continue
        if not booked:
            continue
        if not exact_live and len(exact_done) == 1:
            bill = exact_done[0]
            first = settled_fp.get(bill["id"])
            if first and first != fp and (tx_by_fp.get(first) or {}).get("status") == "BOOK":
                out.append(Decision("duplicate", fp, bill["id"]))
            elif not first:
                out.append(Decision("evidence", fp, bill["id"]))
                settled_fp[bill["id"]] = fp
            used.add(fp)
            continue
        for tier, reason, bill in graded:
            if bill not in live:
                continue
            if tier == "exact":
                out.append(Decision("suggest", fp, bill["id"], "ambiguous"))
            elif tier in ("probable", "before"):
                out.append(Decision("suggest", fp, bill["id"], reason))
    return out


def not_seen(intents: list[dict], links: dict[tuple[str, str], str], *, read_through: str, today: date,
             grace_days: int = 4) -> list[str]:
    """Bills the user said they paid that the statement still doesn't show, though
    it has been read past *grace_days* business days after the payment."""
    linked = {iid for (_, iid), state in links.items() if state in (*SETTLING, "pending", "suggested")}
    out = []
    for i in intents:
        if i["status"] != "user_paid" or i["id"] in linked or not i.get("paid_day"):
            continue
        due = business_days_after(i["paid_day"], grace_days)
        if due <= today and read_through and read_through >= due.isoformat():
            out.append(i["id"])
    return out
