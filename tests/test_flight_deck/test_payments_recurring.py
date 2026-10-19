"""Phase 4d — recurring bills end to end: detected and suggested, confirmed by the
owner, prepared when fixed (not flagged as a duplicate of last month), waited for
when variable (and named missing in the digest), covered by a direct debit on
the statement, optionally drafted in Revolut for the due date."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import (
    auth, delivery_routes, payment_reminders as prm, payments_routes as pr, recurring_bills as rb_,
    revolut_business as rb, statements_routes as sr,
)
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore
from captain_claw.payments import recurring as rc

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
AGENT = {"X-Agent-Auth": "tok-A"}
RENT, HEP, GYM = "HR1723600001101234565", "HR1210010051863000160", "HR5623400091110000000"
TODAY = date.today()


LAST = TODAY - timedelta(days=5)              # the latest payment; the next one is ~25 days away


def months_ago(n: int) -> date:
    return rc.add_months(LAST, -n, LAST.day)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    for k, v in {"FD_PAYMENTS": "1", "FD_PUBLIC_URL": "https://deck.example.com"}.items():
        monkeypatch.setenv(k, v)
    for k in ("FD_SCHEDULER_QUIET_HOURS", "FD_PAYMENTS_STATEMENTS"):
        monkeypatch.delenv(k, raising=False)
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    rb_._detected.clear()
    sent = []

    async def deliver(origin, text):
        sent.append(text)
        return True, "ok"

    monkeypatch.setattr(delivery_routes, "deliver_to_origin", deliver)
    monkeypatch.setattr(server, "_agent_caller_ok", lambda request: True)
    monkeypatch.setattr(server, "_find_agent_by_auth", lambda t: (t == "tok-A", OWNER["id"], "slug"))
    app = FastAPI()
    for r in (sr.router, prm.router, rb_.router, pr.router, pr.public_router):
        app.include_router(r)
    app.dependency_overrides[auth.get_current_user] = lambda: OWNER
    yield TestClient(app), store, db, sent
    asyncio.run(store.close())
    pr.set_store(None)
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


def paid_bill(store, iban, cents, paid: date, *, payee="Stan d.o.o.", model="HR00", reference="1234-5") -> str:
    pid = f"pay_{secrets.token_hex(6)}"
    fields = {"payee_name": payee, "iban": iban, "amount_cents": cents, "currency": "EUR", "model": model,
              "reference": reference, "payee_street": "Ilica 1", "payee_city": "10000 Zagreb"}
    asyncio.run(store.create({"id": pid, "owner_id": OWNER["id"], "status": "settled", "fields": fields,
                              "iban": iban, "amount_cents": cents, "reference": reference, "payee_key": payee.lower(),
                              "paid_at": datetime(paid.year, paid.month, paid.day, 12).timestamp(),
                              "barcodes": ["hub3"]}))
    return pid


def statement_row(store, iban, cents, day: date, name="HEP ELEKTRA"):
    asyncio.run(store.upsert_bank_transactions(OWNER["id"], [{
        "account_key": "k", "fp": f"fp-{iban}-{day}", "status": "BOOK", "booking_date": day.isoformat(),
        "amount_cents": cents, "currency": "EUR", "creditor_name": name, "creditor_iban": iban}]))


def run(day: date) -> dict:
    rb_._detected.clear()
    return asyncio.run(rb_.run_owner(OWNER["id"], day))


def test_a_fixed_bill_is_suggested_confirmed_and_prepared(env):
    client, store, db, _ = env
    for n in (2, 1, 0):
        paid_bill(store, RENT, 65000, months_ago(n))
    assert run(TODAY)["suggested"] == 1
    [bill] = client.get("/fd/payments/recurring").json()["bills"]
    assert (bill["status"], bill["cadence"], bill["anchor_day"], bill["can_prepare"], bill["amount_text"]) == (
        "suggested", "monthly", LAST.day, True, "650.00 EUR")
    bill = client.put(f"/fd/payments/recurring/{bill['id']}", json={"status": "active"}).json()["bill"]
    due = date.fromisoformat(bill["next_due"])
    assert due == rc.add_months(LAST, 1, LAST.day)
    assert run(due - timedelta(days=6)) == {"suggested": 0}                  # too early to prepare
    assert run(due - timedelta(days=5)) == {"suggested": 0, "prepared": 1}
    assert run(due - timedelta(days=4)) == {"suggested": 0}                  # once per period
    [row] = asyncio.run(store.list(OWNER["id"], statuses=("ready", "needs_review")))
    assert row["status"] == "ready"                                           # last month's isn't a "duplicate"
    assert row["due_date"] == due.isoformat() and row["recurring_id"] == bill["id"]
    assert row["fields"]["payee_street"] == "Ilica 1" and row["provenance"]["iban"] == "recurring"
    assert any(n["title"].startswith("Prepared from your recurring bill") for n in asyncio.run(db.list_notifications(OWNER["id"])))
    asyncio.run(store.update(row["id"], status="settled", paid_at=time.time()))
    assert run(due + timedelta(days=1)) == {"suggested": 0, "paid": 1}
    periods = client.get("/fd/payments/recurring").json()["bills"][0]["periods"]
    assert [p["status"] for p in periods] == ["paid"] and periods[0]["intent_id"] == row["id"]
    # Back from a long pause: a period opened after its day is never prepared late or called missing.
    assert run(due + timedelta(days=95)) == {"suggested": 0}
    assert run(due + timedelta(days=105)) == {"suggested": 0}
    periods = client.get("/fd/payments/recurring").json()["bills"][0]["periods"]
    assert [p["status"] for p in periods] == ["paid", "expected"]
    assert asyncio.run(store.list(OWNER["id"], statuses=("ready",))) == []


def test_a_variable_bill_waits_for_its_invoice_and_is_named_when_missing(env):
    client, store, _, sent = env
    for n, cents in ((2, 7900), (1, 8420), (0, 9100)):
        statement_row(store, HEP, cents, months_ago(n))
    run(TODAY)
    [bill] = client.get("/fd/payments/recurring").json()["bills"]
    assert (bill["source"], bill["amount_kind"], bill["can_prepare"], bill["amount_text"]) == (
        "statement", "variable", False, "79.00 EUR – 91.00 EUR")
    client.put(f"/fd/payments/recurring/{bill['id']}", json={"status": "active"})
    due = rc.add_months(LAST, 1, LAST.day)
    assert run(due - timedelta(days=3)) == {"suggested": 0}
    assert run(due + timedelta(days=6)) == {"suggested": 0, "missing": 1}
    # The digest says so — once.
    morning = datetime(*(due + timedelta(days=6)).timetuple()[:3], 9, 30).timestamp()
    out = asyncio.run(prm.digest_owner(OWNER["id"], morning))
    assert out["sent"] and out["missing"] == 1
    assert asyncio.run(prm.digest_owner(OWNER["id"], morning + 86400))["sent"] is False


def test_the_invoice_links_to_its_period_and_the_agent_sees_whats_expected(env):
    client, store, _, _ = env
    for n, cents in ((2, 7900), (1, 8420), (0, 9100)):
        statement_row(store, HEP, cents, months_ago(n))
    run(TODAY)
    bill = client.get("/fd/payments/recurring").json()["bills"][0]
    client.put(f"/fd/payments/recurring/{bill['id']}", json={"status": "active"})
    due = date.fromisoformat(client.get("/fd/payments/recurring").json()["bills"][0]["next_due"])
    if due - timedelta(days=rc.LEAD_DAYS) <= TODAY <= due + timedelta(days=7):
        expected = client.get("/fd/payments/agent/list?status=due", headers=AGENT).json()["expected"]
        assert expected and expected[0]["payee_name"] == "HEP ELEKTRA"
    fields = {"payee_name": "HEP Elektra", "iban": HEP, "amount": "88,00", "model": "HR01", "reference": "9-9",
              "due_date": due.isoformat()}
    intent = client.post("/fd/payments/agent/prepare", json={"fields": fields}, headers=AGENT).json()["intent"]
    assert asyncio.run(store.get(intent["id"]))["recurring_id"] == bill["id"]
    periods = client.get("/fd/payments/recurring").json()["bills"][0]["periods"]
    assert periods[-1]["status"] == "prepared" and periods[-1]["intent_id"] == intent["id"]


def test_a_direct_debit_covers_the_period(env):
    client, store, _, _ = env
    pid = paid_bill(store, GYM, 3000, LAST, payee="Gym d.o.o.", model="HR99", reference="")
    bill = client.post(f"/fd/payments/{pid}/recurring", json={"cadence": "monthly"}).json()["bill"]
    assert bill["status"] == "active" and bill["can_prepare"] is False and bill["next_due"]
    bill = client.put(f"/fd/payments/recurring/{bill['id']}", json={"reference_kind": "fixed"}).json()["bill"]
    assert bill["can_prepare"] is True                                       # HR99: no reference — fixed is fine
    due = date.fromisoformat(bill["next_due"])
    statement_row(store, GYM, 3000, due - timedelta(days=7), name="GYM")      # the bank paid it already
    assert run(due - timedelta(days=5)) == {"suggested": 0, "covered": 1}
    [period] = client.get("/fd/payments/recurring").json()["bills"][0]["periods"]
    assert period["status"] == "covered" and "paid outside Captain" in period["note"]
    assert asyncio.run(store.list(OWNER["id"], statuses=("ready",))) == []   # nothing prepared to pay twice


def test_agent_proposes_owner_decides(env):
    client, store, _, _ = env
    pid = paid_bill(store, RENT, 65000, LAST)
    r = client.post(f"/fd/payments/agent/{pid}/recurring", json={"cadence": "monthly"}, headers=AGENT).json()["bill"]
    assert r["status"] == "suggested"
    listed = client.get("/fd/payments/agent/recurring", headers=AGENT).json()["bills"]
    assert listed[0]["status"] == "suggested" and listed[0]["payee_name"] == "Stan d.o.o."
    client.put(f"/fd/payments/recurring/{r['id']}", json={"status": "dismissed"})
    for n in (2, 1):
        paid_bill(store, RENT, 65000, months_ago(n))
    run(TODAY)                                                               # detection doesn't bring it back
    assert client.get("/fd/payments/recurring").json()["bills"] == []
    assert client.put(f"/fd/payments/recurring/{r['id']}", json={"cadence": "weekly"}).status_code == 422


def test_revolut_drafts_are_scheduled_for_the_due_date(env, monkeypatch):
    client, store, _, _ = env
    for n in (2, 1, 0):
        paid_bill(store, RENT, 65000, months_ago(n))
    run(TODAY)
    bill = client.get("/fd/payments/recurring").json()["bills"][0]
    drafts = []

    async def handoff(row, owner, actor, schedule_for=""):
        drafts.append((row["id"], actor, schedule_for))
        return {}

    async def conn(owner):
        return {"account_id": "acc-eur", "refresh_token": "r"}

    monkeypatch.setattr(pr, "_handoff_revolut", handoff)
    monkeypatch.setattr(pr, "_revolut_conn", conn)
    assert client.get("/fd/payments/recurring").json()["revolut"] is True
    client.put(f"/fd/payments/recurring/{bill['id']}", json={"status": "active", "revolut_draft": True})
    due = date.fromisoformat(client.get("/fd/payments/recurring").json()["bills"][0]["next_due"])
    run(due - timedelta(days=5))
    assert drafts and drafts[0][1:] == ("recurring", due.isoformat())


def test_revolut_client_sends_schedule_for(tmp_path):
    bodies = []

    def fake(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "draft-1"})

    async def save(tokens):
        return None

    rb.transport = httpx.MockTransport(fake)
    try:
        client = rb.RevolutClient({"owner_id": "o", "access_token": "a", "access_expires_at": time.time() + 3600,
                                   "environment": "sandbox"}, save)
        asyncio.run(client.create_draft(account_id="acc", counterparty_id="cp", counterparty_account_id="",
                                        amount_cents=65000, reference="HR00 1234-5", title="Captain · Stan",
                                        schedule_for="2026-11-01"))
    finally:
        rb.transport = None
    assert bodies[0]["schedule_for"] == "2026-11-01" and bodies[0]["payments"][0]["amount"] == 650.0
