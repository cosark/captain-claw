"""Phase 4b — bills settled from the bank statement, end to end against a fake
Enable Banking AIS: exact matches settle, near ones wait for the owner, a bill
paid before Captain prepared it is held back, "paid twice" and reversals warn
with a bell, and the agent sees a summary only — never the transactions."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from datetime import date, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import (
    auth, bank_link, bank_statements as bs, payments_routes as pr, revolut_business as rb, statement_matching as sm,
    statements_routes as sr,
)
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore

pytest.importorskip("cryptography")

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
AGENT = {"X-Agent-Auth": "tok-A"}
HEP = "HR1210010051863000160"
BANK = "Zagrebačka banka"
TODAY = date.today()


def eb_tx(entry, *, days_ago=0, amount="84.20", iban=HEP, ref="HR01 7269-68499637766-00019", status="BOOK"):
    day = (TODAY - timedelta(days=days_ago)).isoformat()
    tx = {"entry_reference": entry, "transaction_amount": {"amount": amount, "currency": "EUR"},
          "credit_debit_indicator": "DBIT", "status": status, "booking_date": day if status == "BOOK" else None,
          "value_date": day, "creditor": {"name": "HEP ELEKTRA D.O.O."}, "remittance_information": [ref] if ref else []}
    if iban:
        tx["creditor_account"] = {"iban": iban}
    return tx


class FakeAIS:
    def __init__(self) -> None:
        self.txs: list[dict] = []
        self.reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/aspsps":
            return httpx.Response(200, json={"aspsps": [{"name": BANK, "country": "HR",
                                                         "maximum_consent_validity": 15552000}]})
        if path == "/auth":
            body = json.loads(request.content)
            return httpx.Response(200, json={"url": f"https://bank.example/?state={body['state']}"})
        if path == "/sessions":
            return httpx.Response(200, json={"session_id": "sess-1", "aspsp": {"name": BANK, "country": "HR"},
                                             "access": {"valid_until": "2027-04-17T00:00:00+00:00"},
                                             "accounts": [{"uid": "u-1", "identification_hash": "h-1", "currency": "EUR",
                                                           "account_id": {"iban": "HR1723600001101234565"}}]})
        if path.endswith("/transactions"):
            self.reads += 1
            return httpx.Response(200, json={"transactions": self.txs})
        return httpx.Response(404, json={"message": "no"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    key, _ = rb.generate_key_and_certificate()
    (tmp_path / "eb.pem").write_text(key)
    for k, v in {"FD_PAYMENTS": "1", "FD_PAYMENTS_STATEMENTS": "1", "FD_ENABLEBANKING_APP_ID": "app-1",
                 "FD_ENABLEBANKING_KEY": str(tmp_path / "eb.pem"), "FD_PUBLIC_URL": "https://deck.example.com"}.items():
        monkeypatch.setenv(k, v)
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    bs._ASPSP_CACHE.clear()
    sr._STATES.clear()
    sr._LAST_MANUAL.clear()
    sm._LOCKS.clear()
    fake = FakeAIS()
    monkeypatch.setattr(bank_link, "transport", httpx.MockTransport(fake))
    monkeypatch.setattr(server, "_agent_caller_ok", lambda request: True)
    monkeypatch.setattr(server, "_find_agent_by_auth", lambda t: (t == "tok-A", OWNER["id"], "slug"))
    app = FastAPI()
    app.include_router(sr.router)
    app.include_router(sr.public_router)
    app.include_router(pr.router)
    app.include_router(pr.public_router)
    app.dependency_overrides[auth.get_current_user] = lambda: OWNER
    yield TestClient(app), fake, store, db
    asyncio.run(store.close())
    pr.set_store(None)
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


def _prepare(client, **fields) -> dict:
    body = {"payee_name": "HEP Elektra d.o.o.", "iban": HEP, "amount": "84,20", "model": "HR01",
            "reference": "7269-68499637766-00019", "description": "Struja", **fields}
    return client.post("/fd/payments/agent/prepare", json={"fields": body}, headers=AGENT).json()["intent"]


def _connect(client) -> None:
    url = client.post("/fd/payments/statements/connect", json={"name": BANK}).json()["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    assert client.get(f"/fd/payments/statements/callback?code=c&state={state}",
                      headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 200


def _sync(store) -> dict:
    return asyncio.run(sr.sync_session(asyncio.run(store.list_bank_sessions(OWNER["id"]))[0],
                                       psu={"Psu-Ip-Address": "203.0.113.7"}))


def test_an_exact_match_settles_and_the_agent_sees_only_the_summary(env):
    client, fake, store, _ = env
    bill = _prepare(client)
    assert bill["status"] == "ready" and bill["statement"]["booked_on"] == ""
    fake.txs = [eb_tx("e-1")]
    _connect(client)
    got = client.get(f"/fd/payments/agent/{bill['id']}", headers=AGENT)
    intent = got.json()["intent"]
    assert intent["status"] == "settled" and intent["paid_via"] == "statement"
    assert intent["statement"] == {"booked_on": TODAY.isoformat(), "account_label": "Zagrebačka ••4565",
                                   "match": "auto", "pending_since": "", "pending_account": "", "suggestions": 0}
    assert "HEP ELEKTRA D.O.O." not in got.text                               # the agent never sees the row
    events = client.get(f"/fd/payments/{bill['id']}").json()["events"]
    assert events[-1]["actor"] == "statement" and events[-1]["event"] == "settled"


def test_a_near_match_waits_for_the_owner(env):
    client, fake, store, _ = env
    bill = _prepare(client)
    fake.txs = [eb_tx("e-1", iban="")]                                         # this bank omits the payee IBAN
    _connect(client)
    agent = client.get(f"/fd/payments/agent/{bill['id']}", headers=AGENT).json()["intent"]
    assert agent["status"] == "ready" and agent["statement"]["suggestions"] == 1
    owner = client.get(f"/fd/payments/{bill['id']}").json()
    [s] = owner["statement_suggestions"]
    assert (s["reason"], s["amount_cents"], s["creditor_name"], s["day"]) == (
        "no_iban_on_statement", 8420, "HEP ELEKTRA D.O.O.", TODAY.isoformat())
    r = client.post(f"/fd/payments/{bill['id']}/statement-match", json={"fp": s["fp"], "accept": True}).json()
    assert r["intent"]["status"] == "settled" and r["intent"]["statement"]["match"] == "confirmed"
    assert r["statement_suggestions"] == []
    assert client.post(f"/fd/payments/{bill['id']}/statement-match",
                       json={"fp": s["fp"], "accept": True}).status_code == 409


def test_paid_before_captain_prepared_it_holds_the_bill_back(env):
    client, fake, store, _ = env
    fake.txs = [eb_tx("e-1", days_ago=10)]
    _connect(client)
    bill = _prepare(client)                                                   # the statement already shows it
    assert bill["status"] == "needs_review" and bill["statement"]["suggestions"] == 1
    assert any(w["code"] == "paid_before_prepared" and w["severity"] == "blocking" for w in bill["warnings"])
    assert client.post(f"/fd/payments/agent/{bill['id']}/handoff", headers=AGENT, json={}).status_code == 409
    fp = client.get(f"/fd/payments/{bill['id']}").json()["statement_suggestions"][0]["fp"]
    r = client.post(f"/fd/payments/{bill['id']}/statement-match", json={"fp": fp, "accept": False}).json()
    assert r["intent"]["status"] == "ready" and not any(w["code"] == "paid_before_prepared"
                                                        for w in r["intent"]["warnings"])


def test_paid_twice_and_reversed_warn_with_a_bell(env):
    client, fake, store, db = env
    bill = _prepare(client)
    fake.txs = [eb_tx("e-1", days_ago=0)]
    _connect(client)
    fake.txs = [eb_tx("e-1", days_ago=0), eb_tx("e-2", days_ago=-0)]
    fake.txs[1]["booking_date"] = fake.txs[1]["value_date"] = TODAY.isoformat()
    _sync(store)
    intent = client.get(f"/fd/payments/{bill['id']}").json()["intent"]
    assert intent["status"] == "settled" and any(w["code"] == "paid_twice" for w in intent["warnings"])
    fake.txs[0]["status"] = "RJCT"
    _sync(store)
    _sync(store)                                                              # the bell rings once
    intent = client.get(f"/fd/payments/{bill['id']}").json()["intent"]
    assert intent["status"] == "settled"                                      # never unsettled by itself
    assert any(w["code"] == "bank_reversed" and w["severity"] == "blocking" for w in intent["warnings"])
    titles = [n["title"] for n in asyncio.run(db.list_notifications(OWNER["id"]))]
    assert sum(t.startswith("Paid twice?") for t in titles) == 1 and sum(t.startswith("Payment reversed") for t in titles) == 1


def test_i_paid_but_not_on_the_statement(env):
    client, fake, store, _ = env
    bill = _prepare(client)
    _connect(client)
    asyncio.run(store.update(bill["id"], status="user_paid", paid_via="card", paid_at=time.time() - 12 * 86400))
    asyncio.run(sm.match_owner(OWNER["id"]))
    intent = client.get(f"/fd/payments/{bill['id']}").json()["intent"]
    [w] = [w for w in intent["warnings"] if w["code"] == "not_on_statement"]
    assert w["severity"] == "info" and "Zagrebačka ••4565" in w["message"]
    assert client.put("/fd/payments/statements/settings", json={"not_on_statement": False}).status_code == 200
    assert client.get("/fd/payments/statements").json()["not_on_statement"] is False
    intent = client.get(f"/fd/payments/{bill['id']}").json()["intent"]
    assert not any(w["code"] == "not_on_statement" for w in intent["warnings"])
    # Then the payment shows up: settled, and the user's own paid time is kept.
    paid_at = intent["paid_at"]
    fake.txs = [eb_tx("e-1", days_ago=1)]
    _sync(store)
    intent = client.get(f"/fd/payments/{bill['id']}").json()["intent"]
    assert intent["status"] == "settled" and intent["paid_at"] == paid_at and intent["paid_via"] == "card"


def test_i_paid_on_the_card_reads_the_statement_with_the_user_present(env, monkeypatch):
    client, fake, store, _ = env
    bill = _prepare(client)
    _connect(client)
    spawned = []                     # the read runs in the background (TestClient ends its loop per request)
    monkeypatch.setattr(sr, "_spawn", spawned.append)
    asyncio.run(store.save_bank_session(OWNER["id"], "enablebanking", "sess-1", last_sync_at=time.time() - 3600))
    reads = fake.reads
    fake.txs = [eb_tx("e-1")]
    r = client.post(f"/fd/payments/{bill['id']}/mark", json={"status": "user_paid"},
                    headers={"X-Forwarded-For": "203.0.113.7", "User-Agent": "Phone"})
    assert r.status_code == 200 and len(spawned) == 1
    asyncio.run(spawned[0])
    assert fake.reads == reads + 1
    assert client.get(f"/fd/payments/{bill['id']}").json()["intent"]["status"] == "settled"
    sess = asyncio.run(store.list_bank_sessions(OWNER["id"]))[0]
    assert sess["unattended_at"] == []                                       # it didn't use the unattended budget
