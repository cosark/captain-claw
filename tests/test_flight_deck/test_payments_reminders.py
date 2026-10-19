"""Phase 4c — due-date reminders end to end: one digest a day after the owner's
time and outside quiet hours, to the bell and the chat the bills came from,
with a fresh phone-page link per payable bill; opt-outs per bill (card, phone
page, agent) and altogether; statements read first so paid bills drop out."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import (
    auth, delivery_routes, payment_reminders as prm, payments_routes as pr, statements_routes as sr,
)
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
AGENT = {"X-Agent-Auth": "tok-A"}
HEP = "HR1210010051863000160"
WA = {"kind": "whatsapp", "address": "385911234567"}
TODAY = date.today()


def at(day: date, hh: int, mm: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hh, mm).timestamp()


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
    sent: list[tuple[dict, str]] = []

    async def deliver(origin, text):
        sent.append((origin, text))
        return True, "ok"

    monkeypatch.setattr(delivery_routes, "deliver_to_origin", deliver)
    monkeypatch.setattr(server, "_agent_caller_ok", lambda request: True)
    monkeypatch.setattr(server, "_find_agent_by_auth", lambda t: (t == "tok-A", OWNER["id"], "slug"))
    app = FastAPI()
    for r in (sr.router, prm.router, pr.router, prm.public_router, pr.public_router):
        app.include_router(r)
    app.dependency_overrides[auth.get_current_user] = lambda: OWNER
    yield TestClient(app), store, db, sent
    asyncio.run(store.close())
    pr.set_store(None)
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


def _prepare(client, due: date, origin=WA, **fields) -> dict:
    body = {"payee_name": "HEP Elektra d.o.o.", "iban": HEP, "amount": "84,20", "model": "HR01",
            "reference": "7269-68499637766-00019", "due_date": due.isoformat(), **fields}
    r = client.post("/fd/payments/agent/prepare", json={"fields": body, "origin": origin}, headers=AGENT)
    return r.json()["intent"]


def digest(when: float) -> dict:
    return asyncio.run(prm.digest_owner(OWNER["id"], when))


def test_one_digest_a_day_with_a_working_link(env):
    client, store, db, sent = env
    due = TODAY + timedelta(days=3)
    bill = _prepare(client, due)
    assert digest(at(TODAY, 8, 59))["sent"] is False                         # before the owner's time
    assert digest(at(TODAY, 9, 30)) == {"sent": True, "bills": 1, "missing": 0, "chat": "whatsapp"}
    [(origin, text)] = sent
    assert origin == WA and "HEP Elektra d.o.o. · 84.20 EUR — due in 3 days" in text
    link = next(line.strip() for line in text.splitlines() if "/p/" in line)
    assert link.startswith("https://deck.example.com/p/")
    assert client.get(link.removeprefix("https://deck.example.com")).status_code == 200   # barcode + "I paid"
    note = asyncio.run(db.list_notifications(OWNER["id"]))[0]
    assert note["title"].startswith("HEP Elektra d.o.o. · 84.20 EUR — due in 3 days") and "/p/" not in note["body"]
    assert digest(at(TODAY, 15))["sent"] is False                            # once a day
    assert digest(at(TODAY + timedelta(days=1), 9, 30))["sent"] is False     # nothing new to say
    assert digest(at(due, 9, 30))["sent"] is True and "due today" in sent[-1][1]
    events = [e["event"] for e in client.get(f"/fd/payments/{bill['id']}").json()["events"]]
    assert events.count("reminded_before") == 1 and events.count("reminded_due") == 1
    # The day's new link replaced yesterday's.
    assert client.get(link.removeprefix("https://deck.example.com")).status_code == 404


def test_quiet_hours_and_bills_that_need_review(env, monkeypatch):
    client, store, _, sent = env
    bill = _prepare(client, TODAY)
    asyncio.run(store.update(bill["id"], status="needs_review"))
    monkeypatch.setenv("FD_SCHEDULER_QUIET_HOURS", "9-11")
    assert digest(at(TODAY, 9, 30))["sent"] is False
    assert digest(at(TODAY, 11, 5))["sent"] is True
    assert "needs your review in Flight Deck first" in sent[-1][1] and "/p/" not in sent[-1][1]


def test_opting_out(env):
    client, store, _, sent = env
    a = _prepare(client, TODAY)
    b = _prepare(client, TODAY, payee_name="Vodovod", amount="21,40", reference="1111-22")
    assert client.put(f"/fd/payments/{a['id']}/reminders", json={"on": False}).json()["intent"]["reminders"]["off"]
    r = client.post(f"/fd/payments/agent/{b['id']}/reminders", json={"on": False}, headers=AGENT)
    assert r.json()["intent"]["reminders"]["off"] is True
    assert digest(at(TODAY, 9, 30))["sent"] is False
    client.put(f"/fd/payments/{a['id']}/reminders", json={"on": True})
    assert client.put("/fd/payments/reminders", json={"enabled": False}).json()["enabled"] is False
    assert digest(at(TODAY, 9, 30))["sent"] is False
    settings = client.put("/fd/payments/reminders", json={"enabled": True, "days_before": 2, "time": "07:00"}).json()
    assert settings["chat"] == {"kind": "whatsapp", "label": "Whatsapp ••4567"} and settings["days_before"] == 2
    assert digest(at(TODAY, 7, 5))["sent"] is True
    # "Don't remind me" on the phone page.
    link = next(line.strip() for line in sent[-1][1].splitlines() if "/p/" in line)
    path = link.removeprefix("https://deck.example.com")
    assert "remind me about this one" in client.get(path).text
    assert client.post(f"{path}/no-reminders", follow_redirects=False).status_code == 303
    assert asyncio.run(store.get(a["id"]))["reminders"]["off"] is True


def test_chat_target_and_origins(env):
    client, store, _, sent = env
    _prepare(client, TODAY + timedelta(days=10))                             # from WhatsApp, not due yet
    mail = _prepare(client, TODAY, origin={})                                 # mail triage: no chat of its own
    web = _prepare(client, TODAY, origin={"kind": "web", "address": "s-1"}, payee_name="Vodovod",
                   amount="21,40", reference="1111-22")
    assert asyncio.run(store.get(web["id"]))["notify_origin"] == {}          # a web session isn't a chat
    assert digest(at(TODAY, 9, 30))["chat"] == "whatsapp"
    assert sent[-1][0] == WA and "due today" in sent[-1][1] and mail["id"]


def test_agent_lists_due_bills(env):
    client, *_ = env
    soon = _prepare(client, TODAY + timedelta(days=2))
    _prepare(client, TODAY + timedelta(days=20), payee_name="Later", amount="10,00", reference="2222-33")
    late = _prepare(client, TODAY - timedelta(days=3), payee_name="Late", amount="11,00", reference="3333-44")
    rows = client.get("/fd/payments/agent/list?status=due", headers=AGENT).json()["payments"]
    assert [r["id"] for r in rows] == [late["id"], soon["id"]]


def test_statements_are_read_first(env, monkeypatch):
    client, store, _, sent = env
    monkeypatch.setenv("FD_PAYMENTS_STATEMENTS", "1")
    bill = _prepare(client, TODAY)
    asyncio.run(store.save_bank_session(OWNER["id"], "enablebanking", "s-1", status="active",
                                        last_sync_at=at(TODAY, 9, 30) - 8 * 3600))
    reads = []

    async def read(sess, *, psu=None, now=None):
        reads.append(sess["session_id"])
        await store.update(bill["id"], status="settled")                     # the statement shows it paid
        return {"new": 1}

    monkeypatch.setattr(sr, "sync_session", read)
    assert digest(at(TODAY, 9, 30))["sent"] is False and reads == ["s-1"] and sent == []
