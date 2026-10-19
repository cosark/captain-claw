"""Phase 4d — spotting recurring bills (pure)."""

from __future__ import annotations

from datetime import date, timedelta

from captain_claw.payments import recurring as rc

TODAY = date(2026, 10, 19)
RENT, HEP, GYM = "HR1723600001101234565", "HR1210010051863000160", "HR5623400091110000000"


def h(day, iban=RENT, amount=65000, ref="2026-1", model="HR00", name="Stan d.o.o."):
    return {"iban": iban, "payee_name": name, "payee_key": name.lower(), "amount_cents": amount, "model": model,
            "reference": ref, "day": date.fromisoformat(day)}


def s(day, iban=HEP, amount=8420, name="HEP ELEKTRA"):
    return {"iban": iban, "creditor_name": name, "amount_cents": amount, "day": date.fromisoformat(day)}


def test_a_monthly_fixed_bill_from_captains_history():
    [p] = rc.detect([h("2026-08-01"), h("2026-09-02"), h("2026-10-01")], [], today=TODAY)
    assert (p.cadence, p.anchor_day, p.amount_kind, p.reference_kind, p.reference, p.source) == (
        "monthly", 1, "fixed", "fixed", "2026-1", "history")
    assert rc.next_due(date.fromisoformat(p.last_paid), p.cadence, p.anchor_day) == date(2026, 11, 1)


def test_utilities_from_the_statement_need_three_payments():
    rows = [s("2026-08-10", amount=7900), s("2026-09-09", amount=8420)]
    assert rc.detect([], rows, today=TODAY) == []
    [p] = rc.detect([], rows + [s("2026-10-10", amount=9100)], today=TODAY)
    assert (p.cadence, p.amount_kind, p.amount_min, p.amount_max, p.reference_kind, p.source, p.payee_name) == (
        "monthly", "variable", 7900, 9100, "per_bill", "statement", "HEP ELEKTRA")


def test_history_and_statement_are_one_payment_not_two():
    hist = [h("2026-09-02", iban=HEP, amount=8420, ref="1-1"), h("2026-10-03", iban=HEP, amount=8420, ref="1-2")]
    rows = [s("2026-09-03"), s("2026-10-03"), s("2026-08-02")]
    [p] = rc.detect(hist, rows, today=TODAY)
    assert p.occurrences == 3 and p.reference_kind == "per_bill" and p.payee_name == "Stan d.o.o."


def test_yearly_quarterly_and_no_pattern():
    [p] = rc.detect([], [s("2024-11-15", iban=GYM, amount=30000), s("2025-11-14", iban=GYM, amount=30000)],
                    today=date(2026, 10, 19))
    assert p.cadence == "yearly" and rc.next_due(date(2025, 11, 14), "yearly", p.anchor_day) == date(2026, 11, 14)
    [q] = rc.detect([h("2026-04-10"), h("2026-07-11")], [], today=TODAY)
    assert q.cadence == "quarterly"
    assert rc.detect([h("2026-08-01"), h("2026-08-20"), h("2026-10-01")], [], today=TODAY) == []   # irregular


def test_a_stopped_bill_isnt_suggested():
    assert rc.detect([h("2026-03-01"), h("2026-04-01"), h("2026-05-01")], [], today=TODAY) == []


def test_month_ends_and_windows():
    assert rc.add_months(date(2026, 1, 31), 1, 31) == date(2026, 2, 28)
    start, end = rc.window(date(2026, 11, 1), "monthly")
    assert start == date(2026, 10, 18) and end == date(2026, 11, 26)
    assert rc.amount_fits({"amount_kind": "fixed", "amount_cents": 65000}, 65000)
    assert not rc.amount_fits({"amount_kind": "fixed", "amount_cents": 65000}, 64999)
    assert rc.amount_fits({"amount_kind": "variable", "amount_min": 7900, "amount_max": 9100}, 12000)
    assert not rc.amount_fits({"amount_kind": "variable", "amount_min": 7900, "amount_max": 9100}, 1000)
