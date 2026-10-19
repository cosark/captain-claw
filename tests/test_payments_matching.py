"""Phase 4b — the statement matcher (pure). Exact matches settle; anything less
certain is a suggestion; an open bill wins over a paid one; a second payment
soon after the first is "paid twice", next month's isn't."""

from __future__ import annotations

from datetime import date

import pytest

from captain_claw.payments import matching as m

TODAY = date(2026, 10, 19)
HEP = "HR1210010051863000160"
OTHER = "HR1723600001101234565"


def bill(id="pay_a", status="handed_off", iban=HEP, amount=8420, model="HR01", reference="7269-68499637766-00019",
         payee="HEP Elektra d.o.o.", created=date(2026, 10, 1), paid=None):
    return {"id": id, "status": status, "iban": iban, "amount_cents": amount,
            "ref_digits": m.reference_of(model, reference), "name_tokens": m.name_tokens(payee),
            "created_day": created, "paid_day": paid}


def tx(fp="t1", day="2026-10-10", amount=8420, iban=HEP, refs=("HR01 7269-68499637766-00019 Struja",),
       name="HEP ELEKTRA D.O.O.", status="BOOK"):
    return {"fp": fp, "status": status, "booking_date": day if status == "BOOK" else "", "value_date": day,
            "amount_cents": amount, "currency": "EUR", "creditor_name": name, "creditor_iban": iban,
            "reference_digits": m.reference_digits(*refs)}


def run(bills, txs, links=None):
    return [(d.kind, d.fp, d.intent_id, d.reason) for d in m.match(bills, txs, links or {}, today=TODAY)]


@pytest.mark.parametrize("refs", [
    ("HR01 7269-68499637766-00019",), ("HR0172696849963776600019 rujan 2026",),
    ("7269 68499637766 00019",), ("", "HR01-7269-68499637766-00019"),
])
def test_exact_settles_whatever_the_reference_looks_like(refs):
    assert run([bill()], [tx(refs=refs)]) == [("settle", "t1", "pay_a", "")]


@pytest.mark.parametrize("change", [{"amount": 8419}, {"iban": OTHER}, {"refs": ("HR01 7269-11111111111-00019",)}])
def test_no_match(change):
    assert run([bill()], [tx(**change)]) == []


def test_near_matches_are_suggestions():
    assert run([bill()], [tx(iban="")]) == [("suggest", "t1", "pay_a", "no_iban_on_statement")]
    assert run([bill()], [tx(refs=())]) == [("suggest", "t1", "pay_a", "no_reference_on_statement")]
    assert run([bill(model="HR99", reference="")], [tx(iban="", refs=())]) == [("suggest", "t1", "pay_a", "name_only")]


def test_bills_without_a_reference():
    rent = bill(model="HR99", reference="", payee="Stan d.o.o.")
    assert run([rent], [tx(refs=())]) == [("settle", "t1", "pay_a", "")]
    twin = {**rent, "id": "pay_b"}
    assert run([rent, twin], [tx(refs=())]) == [("suggest", "t1", "pay_a", "ambiguous"),
                                                 ("suggest", "t1", "pay_b", "ambiguous")]
    # A payment from before a no-reference bill existed belongs to the last period.
    assert run([rent], [tx(day="2026-09-20", refs=())]) == []


def test_pending_then_booked():
    assert run([bill()], [tx(status="PDNG")]) == [("pending", "t1", "pay_a", "")]
    assert run([bill()], [tx()], {("t1", "pay_a"): "pending"}) == [("settle", "t1", "pay_a", "")]


def test_paid_twice_but_not_next_month():
    paid = bill(status="settled", paid=date(2026, 10, 10))
    links = {("t1", "pay_a"): "auto"}
    first = tx()
    assert run([paid], [first, tx("t2", day="2026-10-13")], links) == [("duplicate", "t2", "pay_a", "")]
    rent = bill(status="settled", model="HR99", reference="", paid=date(2026, 9, 10), created=date(2026, 9, 1))
    assert run([rent], [tx(day="2026-09-10", refs=()), tx("t2", day="2026-10-10", refs=())], links) == []


def test_an_open_bill_wins_over_a_paid_one():
    september = bill("pay_sep", status="settled", model="HR99", reference="", created=date(2026, 9, 1),
                     paid=date(2026, 9, 28))
    october = bill("pay_oct", model="HR99", reference="", created=date(2026, 10, 1))
    out = run([september, october], [tx("t9", day="2026-09-28", refs=()), tx("t10", day="2026-10-05", refs=())],
              {("t9", "pay_sep"): "auto"})
    assert out == [("settle", "t10", "pay_oct", "")]


def test_reversal_and_evidence():
    assert run([bill(status="settled", paid=date(2026, 10, 10))], [tx(status="RJCT")],
               {("t1", "pay_a"): "auto"}) == [("reversed", "t1", "pay_a", "")]
    # Settled by a bank link (no statement link yet): the statement is evidence, not a second payment.
    assert run([bill(status="settled", paid=date(2026, 10, 9))], [tx()]) == [("evidence", "t1", "pay_a", "")]


def test_paid_before_captain_prepared_it():
    late = bill(created=date(2026, 10, 15))
    assert run([late], [tx(day="2026-10-02")]) == [("suggest", "t1", "pay_a", "before_prepared")]
    assert run([bill(created=date(2026, 12, 15))], [tx(day="2026-10-02")]) == []        # more than 45 days


def test_expired_and_user_paid_bills_still_settle():
    assert run([bill(status="expired")], [tx()]) == [("settle", "t1", "pay_a", "")]
    assert run([bill(status="user_paid", paid=date(2026, 10, 9))], [tx()]) == [("settle", "t1", "pay_a", "")]


def test_earlier_answers_stick_and_future_rows_wait():
    assert run([bill()], [tx(iban="")], {("t1", "pay_a"): "rejected"}) == []
    assert run([bill()], [tx(iban="")], {("t1", "pay_a"): "suggested"}) == []
    assert run([bill()], [tx(day="2026-10-25")]) == []
    # One transaction settles one bill; the next exact one goes to the next bill.
    a, b = bill("pay_a", model="HR99", reference=""), bill("pay_b", model="HR99", reference="", amount=1875)
    assert run([a, b], [tx(refs=()), tx("t2", amount=1875, refs=())]) == [
        ("settle", "t1", "pay_a", ""), ("settle", "t2", "pay_b", "")]


def test_not_seen_after_four_business_days():
    paid = bill(status="user_paid", paid=date(2026, 10, 9))                 # a Friday → due Thu 15th
    assert m.not_seen([paid], {}, read_through="2026-10-19", today=TODAY) == ["pay_a"]
    assert m.not_seen([paid], {}, read_through="2026-10-14", today=TODAY) == []
    assert m.not_seen([paid], {("t1", "pay_a"): "suggested"}, read_through="2026-10-19", today=TODAY) == []
    assert m.not_seen([bill(status="user_paid", paid=date(2026, 10, 16))], {}, read_through="2026-10-19",
                      today=TODAY) == []
