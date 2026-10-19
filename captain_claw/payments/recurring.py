"""Recurring bills — Payments Phase 4d (docs/payments-phase4-plan.md).

Pure: spot bills that repeat (from Captain's own paid bills and the outgoing
payments on the bank statement), and work out when the next one is expected,
which window counts as "this period", and whether a payment covers it.

A pattern is grouped by the payee's IBAN. Its cadence (monthly, bimonthly,
quarterly, yearly) has to hold for every recent interval; its amount is
``fixed`` when every recent payment was the same, else ``variable``; its
reference is ``fixed`` when Captain's bills all carried the same one (rent, a
loan, kindergarten), else ``per_bill`` (utilities quote the invoice number).
Patterns are only ever *suggestions* — nothing acts until the owner confirms.
"""

from __future__ import annotations

import calendar
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

# cadence → (months between payments, typical days, tolerance in days)
CADENCES = {"monthly": (1, 30, 5), "bimonthly": (2, 61, 7), "quarterly": (3, 91, 10), "yearly": (12, 365, 15)}
SAME_PAYMENT_DAYS = 5          # a settled bill and its statement row are one payment
LEAD_DAYS = 10                 # a period opens this long before it's expected
PREPARE_DAYS = 5               # a fixed bill is prepared this long before it's due…
PREPARE_LATE_DAYS = 2          # …and not more than this after (never a bill for a long-gone due date)
MISSING_AFTER_DAYS = 5         # an invoice this late is "missing"
_NON_DIGIT = re.compile(r"\D+")


@dataclass
class Pattern:
    iban: str
    payee_name: str
    payee_key: str
    cadence: str
    anchor_day: int
    amount_kind: str
    amount_cents: int
    amount_min: int
    amount_max: int
    reference_kind: str
    model: str = ""
    reference: str = ""
    last_paid: str = ""
    source: str = "history"
    occurrences: int = 0
    days: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def add_months(day: date, months: int, anchor_day: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(max(1, anchor_day), calendar.monthrange(year, month)[1]))


def next_due(after: date, cadence: str, anchor_day: int) -> date:
    return add_months(after, CADENCES[cadence][0], anchor_day)


def window(expected: date, cadence: str) -> tuple[date, date]:
    """The days whose payment counts for the period expected on *expected*."""
    typical = CADENCES[cadence][1]
    return expected - timedelta(days=min(15, typical // 2 - 1)), expected + timedelta(days=25)


def _cadence(days: list[date]) -> str:
    """The cadence every recent interval fits, or ''."""
    gaps = [(b - a).days for a, b in zip(days, days[1:])][-4:]
    if not gaps:
        return ""
    for name, (_, typical, tolerance) in CADENCES.items():
        if all(abs(g - typical) <= tolerance for g in gaps):
            return name
    return ""


def _dedupe(events: list[dict]) -> list[dict]:
    """One event per payment: drop a later one within a few days with the same amount."""
    out: list[dict] = []
    for e in sorted(events, key=lambda e: e["day"]):
        if out and (e["day"] - out[-1]["day"]).days <= SAME_PAYMENT_DAYS and e["amount_cents"] == out[-1]["amount_cents"]:
            continue
        out.append(e)
    return out


def detect(history: list[dict], statement: list[dict], *, today: date) -> list[Pattern]:
    """*history*: Captain's paid bills {iban, payee_name, payee_key, amount_cents, model,
    reference, day}. *statement*: outgoing bank payments {iban, creditor_name,
    amount_cents, day}. Returns the patterns that hold and are still current."""
    by_iban: dict[str, dict] = {}
    for h in history:
        if h.get("iban") and h.get("day"):
            by_iban.setdefault(h["iban"], {"history": [], "statement": []})["history"].append(h)
    for s in statement:
        if s.get("iban") and s.get("day"):
            by_iban.setdefault(s["iban"], {"history": [], "statement": []})["statement"].append(s)
    out = []
    for iban, group in by_iban.items():
        hist = group["history"]
        events = list(hist)
        for s in group["statement"]:          # a statement row adds a payment Captain didn't see
            if not any(abs((s["day"] - h["day"]).days) <= SAME_PAYMENT_DAYS and s["amount_cents"] == h["amount_cents"]
                       for h in hist):
                events.append(s)
        events = _dedupe(events)
        cadence = _cadence([e["day"] for e in events])
        if not cadence:
            continue
        needed = 2 if hist or cadence == "yearly" else 3
        if len(events) < needed:
            continue
        last = events[-1]["day"]
        if (today - last).days > CADENCES[cadence][1] * 1.5 + CADENCES[cadence][2]:
            continue                          # it stopped
        recent = events[-4:]
        amounts = [e["amount_cents"] for e in recent]
        refs = {_NON_DIGIT.sub("", h.get("reference", "")) for h in hist[-4:]}
        fixed_ref = len(hist) >= 2 and len(refs) == 1 and next(iter(refs)) != ""
        named = next((e for e in reversed(events) if e.get("payee_name")), None) or {}
        out.append(Pattern(
            iban=iban, payee_name=named.get("payee_name") or events[-1].get("creditor_name") or "",
            payee_key=named.get("payee_key", ""), cadence=cadence,
            anchor_day=int(statistics.median(e["day"].day for e in recent)) if cadence != "yearly" else last.day,
            amount_kind="fixed" if len(set(amounts)) == 1 else "variable", amount_cents=amounts[-1],
            amount_min=min(amounts), amount_max=max(amounts),
            reference_kind="fixed" if fixed_ref else "per_bill",
            model=hist[-1].get("model", "") if fixed_ref else "", reference=hist[-1].get("reference", "") if fixed_ref else "",
            last_paid=last.isoformat(), source="history" if hist else "statement", occurrences=len(events),
            days=[e["day"].isoformat() for e in events[-6:]]))
    return out


def amount_fits(bill: dict, amount_cents: int) -> bool:
    """Does a payment of *amount_cents* look like this recurring bill?"""
    if bill["amount_kind"] == "fixed":
        return amount_cents == int(bill["amount_cents"])
    low, high = int(bill.get("amount_min") or 0), int(bill.get("amount_max") or 0)
    return low // 2 <= amount_cents <= max(high * 2, low + 1)
