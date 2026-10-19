"""Payments Phase 4a — read-only bank statements, against a fake Enable Banking
AIS and a fake Revolut Business.

Pins: only outgoing payments are stored (incoming ones never are); an account
is read at most four times a day without the user, and a read with the user
present carries their PSU headers; a renewal keeps the history without
duplicating it; disconnecting forgets everything read; the agent sees nothing.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import (
    auth, bank_link, bank_statements as bs, payments_jobs, payments_routes as pr,
    revolut_business as rb, statements_routes as sr,
)
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore

pytest.importorskip("cryptography")

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
AGENT = {"X-Agent-Auth": "tok-A"}
APP_ID = "app-0000-1111"
BANK = "Erste&Steiermärkische Bank"
HEP_IBAN = "HR1210010051863000160"


def _tx(entry, amount, indicator="DBIT", status="BOOK", **extra):
    return {"entry_reference": entry, "transaction_amount": {"amount": amount, "currency": "EUR"},
            "credit_debit_indicator": indicator, "status": status, "booking_date": "2026-10-10",
            "value_date": "2026-10-10", **extra}


class FakeAIS:
    def __init__(self) -> None:
        self.auth_bodies: list[dict] = []
        self.reads: list[dict] = []           # {uid, params, headers}
        self.deleted: list[str] = []
        self.sessions = 0
        self.error: tuple[int, dict] | None = None
        self.pages = {
            None: {"transactions": [
                _tx("e-1", "84.20", creditor={"name": "HEP ELEKTRA D.O.O."},
                    creditor_account={"iban": "HR12 1001 0051 8630 0016 0"},
                    remittance_information=["HR01 7269-68499637766-00019 Struja rujan"]),
                _tx("e-2", "2500.00", "CRDT", debtor={"name": "Employer d.o.o."},
                    remittance_information=["Plaća za rujan"])],
                "continuation_key": "k2"},
            "k2": {"transactions": [_tx("e-3", "-18.75", status="PDNG", booking_date=None,
                                        creditor={"name": "Čistoća"}, reference_number="HR00 123")]},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/aspsps":
            assert request.url.params["service"] == "AIS"
            return httpx.Response(200, json={"aspsps": [
                {"name": BANK, "country": "HR", "logo": "", "beta": False, "maximum_consent_validity": 15552000,
                 "required_psu_headers": ["Psu-Ip-Address"]}]})
        if path == "/auth":
            body = json.loads(request.content)
            self.auth_bodies.append(body)
            return httpx.Response(200, json={"url": f"https://bank.example/auth?state={body['state']}",
                                             "authorization_id": "a-1"})
        if path == "/sessions" and request.method == "POST":
            self.sessions += 1
            n = self.sessions
            return httpx.Response(200, json={
                "session_id": f"sess-{n}", "aspsp": {"name": BANK, "country": "HR"}, "psu_type": "personal",
                "access": {"valid_until": "2027-04-17T00:00:00+00:00"},
                "accounts": [
                    {"uid": f"u-{n}", "identification_hash": "hash-current", "currency": "EUR",
                     "cash_account_type": "CACC", "account_id": {"iban": "HR1723600001101234565"}},
                    {"uid": f"s-{n}", "identification_hash": "hash-savings", "currency": "EUR",
                     "cash_account_type": "SVGS", "account_id": {"iban": "HR1723600003201234567"}}]})
        if path.startswith("/sessions/") and request.method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"message": "OK"})
        if path.endswith("/transactions"):
            uid = path.split("/")[2]
            self.reads.append({"uid": uid, "params": dict(request.url.params),
                               "headers": {k.lower(): v for k, v in request.headers.items()}})
            if self.error:
                return httpx.Response(self.error[0], json=self.error[1])
            if uid.startswith("s-"):                                        # the savings account: no payments
                return httpx.Response(200, json={"transactions": []})
            return httpx.Response(200, json=self.pages[request.url.params.get("continuation_key")])
        return httpx.Response(404, json={"message": f"no route {path}"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    key, _cert = rb.generate_key_and_certificate()
    (tmp_path / "eb.pem").write_text(key)
    for k, v in {"FD_PAYMENTS": "1", "FD_PAYMENTS_STATEMENTS": "1", "FD_ENABLEBANKING_APP_ID": APP_ID,
                 "FD_ENABLEBANKING_KEY": str(tmp_path / "eb.pem"), "FD_PUBLIC_URL": "https://deck.example.com"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FD_ENABLEBANKING_STATEMENTS_REDIRECT_URL", raising=False)
    monkeypatch.delenv("FD_PAYMENTS_SYNC_TIMES", raising=False)
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    bs._ASPSP_CACHE.clear()
    sr._STATES.clear()
    sr._LAST_MANUAL.clear()
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


def _connect(client, fake) -> str:
    banks = client.get("/fd/payments/statements/aspsps?country=HR").json()["banks"]
    assert banks[0]["name"] == BANK and banks[0]["consent_days"] == 180
    url = client.post("/fd/payments/statements/connect", json={"name": BANK}).json()["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    body = fake.auth_bodies[-1]
    assert body["redirect_url"] == "https://deck.example.com/fd/payments/statements/callback"
    assert body["aspsp"] == {"name": BANK, "country": "HR"} and body["psu_type"] == "personal"
    assert datetime.fromisoformat(body["access"]["valid_until"]).timestamp() <= time.time() + 180 * 86400 + 5
    page = client.get(f"/fd/payments/statements/callback?code=c-1&state={state}",
                      headers={"X-Forwarded-For": "203.0.113.7", "User-Agent": "Phone"})
    assert page.status_code == 200, page.text
    return page.text


def test_connect_reads_only_outgoing_payments(env):
    client, fake, store, _ = env
    assert client.get("/fd/payments/statements").json()["sessions"] == []
    page = _connect(client, fake)
    assert "Captain read 2 outgoing payments" in page
    rows = asyncio.run(store.bank_transactions(OWNER["id"]))
    assert sorted(r["amount_cents"] for r in rows) == [1875, 8420]            # the salary never stored
    assert not any("Plaća" in r["remittance"] or "Employer" in r["creditor_name"] for r in rows)
    hep = next(r for r in rows if r["amount_cents"] == 8420)
    assert hep["creditor_iban"] == HEP_IBAN and hep["status"] == "BOOK"
    assert "0172696849963776600019" in hep["reference_digits"].split(" ")[0]
    assert next(r for r in rows if r["amount_cents"] == 1875)["status"] == "PDNG"
    # Only the current account was read; the first read went 13 months back with the user present.
    assert {r["uid"] for r in fake.reads} == {"u-1"}
    first = fake.reads[0]
    assert first["params"]["strategy"] == "longest"
    assert first["params"]["date_from"] == (date.today() - timedelta(days=395)).isoformat()
    assert first["headers"]["psu-ip-address"] == "203.0.113.7" and first["headers"]["psu-user-agent"] == "Phone"
    sess = client.get("/fd/payments/statements").json()["sessions"][0]
    assert sess["bank"] == BANK and sess["status"] == "active" and sess["unattended_left"] == 4
    assert [(a["label"], a["watched"], a["outgoing"]) for a in sess["accounts"]] == [
        ("Erste ••4565", True, 2), ("Erste ••4567", False, 0)]


def test_unattended_reads_keep_the_psd2_daily_limit(env, monkeypatch):
    client, fake, store, _ = env
    _connect(client, fake)
    sess = asyncio.run(store.list_bank_sessions(OWNER["id"]))[0]
    before = len(fake.reads)
    for _ in range(5):
        result = asyncio.run(sr.sync_session(sess))
    assert "four times today" in result["error"]
    unattended = fake.reads[before:]
    assert len({r["params"]["date_from"] for r in unattended}) == 1 and len(unattended) == 4 * 2  # 2 pages each
    assert not any("psu-ip-address" in r["headers"] for r in unattended)
    assert unattended[0]["params"]["date_from"] == (date.today() - timedelta(days=5)).isoformat()
    assert "strategy" not in unattended[0]["params"]
    # The user pressing "Check now" doesn't use the budget.
    r = client.post("/fd/payments/statements/sync", headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200 and r.json()["results"][0]["error"] == ""
    assert client.post("/fd/payments/statements/sync").status_code == 429     # once a minute


def test_background_sync_runs_once_per_slot(env, monkeypatch):
    client, fake, store, _ = env
    _connect(client, fake)
    monkeypatch.setenv("FD_PAYMENTS_SYNC_TIMES", "07:30,16:30")
    monkeypatch.setattr(sr, "_has_work", lambda owner: asyncio.sleep(0, result=True))
    later = datetime.now() + timedelta(days=1)
    slot = datetime(later.year, later.month, later.day, 8, 0).timestamp()
    assert asyncio.run(sr.background_sync(slot)) == 1
    assert asyncio.run(sr.background_sync(slot + 60)) == 0                   # same slot: already read
    assert asyncio.run(sr.background_sync(slot + 9 * 3600)) == 1              # 17:00: the next slot
    # A failed read uses up its slot too: a broken bank doesn't eat the day's four reads in 20 minutes.
    fake.error = (500, {"message": "ASPSP error"})
    next_day = slot + 24 * 3600
    assert asyncio.run(sr.background_sync(next_day)) == 1
    assert asyncio.run(sr.background_sync(next_day + 300)) == 0
    fake.error = None
    # Nothing to confirm: at most one read a day.
    monkeypatch.setattr(sr, "_has_work", lambda owner: asyncio.sleep(0, result=False))
    assert asyncio.run(sr.background_sync(next_day + 9 * 3600)) == 0


def test_renewal_keeps_history_and_disconnect_forgets_it(env):
    client, fake, store, _ = env
    _connect(client, fake)
    client.put("/fd/payments/statements/accounts/s-1", json={"watched": True})
    page = _connect(client, fake)                                            # the same bank again: sess-2
    assert "Captain read 0 outgoing payments" in page                        # same entries, no duplicates
    assert fake.deleted == ["sess-1"]
    sessions = client.get("/fd/payments/statements").json()["sessions"]
    assert [s["session_id"] for s in sessions] == ["sess-2"]
    accounts = {a["label"]: a for a in sessions[0]["accounts"]}
    assert accounts["Erste ••4567"]["watched"] and accounts["Erste ••4565"]["outgoing"] == 2
    renewed = [r for r in fake.reads if r["uid"] == "u-2"]                  # the current account, new uid
    assert renewed[0]["params"]["date_from"] == (date.today() - timedelta(days=5)).isoformat()
    assert [r for r in fake.reads if r["uid"] == "s-2"][0]["params"]["strategy"] == "longest"   # newly watched
    assert client.delete("/fd/payments/statements/enablebanking/sess-2").status_code == 200
    assert fake.deleted == ["sess-1", "sess-2"]
    assert asyncio.run(store.bank_transactions(OWNER["id"])) == []
    assert client.get("/fd/payments/statements").json()["sessions"] == []


def test_ended_consent_rate_limits_and_renewal_notices(env):
    client, fake, store, db = env
    _connect(client, fake)
    sess = asyncio.run(store.list_bank_sessions(OWNER["id"]))[0]
    fake.error = (429, {"error": "ASPSP_RATE_LIMIT_EXCEEDED", "message": "ASPSP Rate limit exceeded"})
    assert "slow down" in asyncio.run(sr.sync_session(sess))["error"]
    assert asyncio.run(store.list_bank_sessions(OWNER["id"]))[0]["status"] == "active"
    fake.error = (401, {"error": "EXPIRED_SESSION", "message": "Session is expired"})
    asyncio.run(sr.sync_session(sess))
    assert asyncio.run(store.list_bank_sessions(OWNER["id"]))[0]["status"] == "needs_renewal"
    # A week before consent ends: one bell, not one per tick.
    asyncio.run(store.save_bank_session(OWNER["id"], "enablebanking", "sess-1", status="active",
                                        valid_until=time.time() + 3 * 86400, notified=""))
    fake.error = None
    asyncio.run(sr.background_sync())
    asyncio.run(sr.background_sync())
    notes = asyncio.run(db.list_notifications(OWNER["id"]))
    assert len([n for n in notes if "ends on" in n["title"]]) == 1
    asyncio.run(store.save_bank_session(OWNER["id"], "enablebanking", "sess-1", valid_until=time.time() - 1))
    asyncio.run(sr.background_sync())
    assert asyncio.run(store.list_bank_sessions(OWNER["id"]))[0]["status"] == "needs_renewal"
    assert any("has ended" in n["title"] for n in asyncio.run(db.list_notifications(OWNER["id"])))


class FakeRevolutStatement:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/1.0", "")
        self.paths.append(path)
        assert request.headers["authorization"] == "Bearer access-1"
        if path == "/accounts":
            return httpx.Response(200, json=[{"id": "acc-eur", "name": "Main", "currency": "EUR", "state": "active"},
                                             {"id": "acc-usd", "name": "USD", "currency": "USD", "state": "active"}])
        if path == "/transactions":
            assert request.url.params["type"] == "transfer" and request.url.params["account"] == "acc-eur"
            return httpx.Response(200, json=[
                {"id": "t-1", "type": "transfer", "state": "completed", "created_at": "2026-10-09T08:00:00Z",
                 "completed_at": "2026-10-09T08:00:05Z", "reference": "HR01 7269-68499637766-00019",
                 "legs": [{"leg_id": "l-1", "account_id": "acc-eur", "amount": -84.2, "currency": "EUR",
                           "description": "To HEP ELEKTRA", "counterparty": {"id": "cp-1", "account_id": "cpa-1",
                                                                             "account_type": "external"}}]},
                {"id": "t-2", "type": "transfer", "state": "completed", "created_at": "2026-10-08T08:00:00Z",
                 "completed_at": "2026-10-08T08:00:05Z", "reference": "Invoice 7",
                 "legs": [{"leg_id": "l-2", "account_id": "acc-eur", "amount": 1200, "currency": "EUR",
                           "description": "From Client Ltd"}]}])
        if path == "/counterparty/cp-1":
            return httpx.Response(200, json={"id": "cp-1", "name": "HEP", "accounts": [
                {"id": "cpa-1", "iban": "HR12 1001 0051 8630 0016 0"}]})
        return httpx.Response(404, json={"message": "no"})


def test_revolut_statement(env, monkeypatch):
    client, _, store, _ = env
    fake = FakeRevolutStatement()
    monkeypatch.setattr(rb, "transport", httpx.MockTransport(fake))
    assert client.put("/fd/payments/statements/revolut", json={"enabled": True}).status_code == 409
    asyncio.run(store.save_connection(OWNER["id"], "revolut", refresh_token="r-1", access_token="access-1",
                                      access_expires_at=time.time() + 3600, environment="sandbox"))
    r = client.put("/fd/payments/statements/revolut", json={"enabled": True}).json()
    assert r["message"] == "Captain read 1 outgoing payments from the last months"
    [row] = asyncio.run(store.bank_transactions(OWNER["id"]))
    assert (row["creditor_name"], row["creditor_iban"], row["amount_cents"], row["status"]) == (
        "HEP ELEKTRA", HEP_IBAN, 8420, "BOOK")
    sess = client.get("/fd/payments/statements").json()["sessions"][0]
    assert sess["bank"] == "Revolut Business" and sess["unattended_left"] is None
    assert [(a["label"], a["watched"]) for a in sess["accounts"]] == [("Revolut Main (EUR)", True),
                                                                      ("Revolut USD (USD)", False)]
    assert client.put("/fd/payments/statements/revolut", json={"enabled": False}).json() == {"enabled": False}
    assert asyncio.run(store.bank_transactions(OWNER["id"])) == []


def test_off_without_the_flag_and_never_shown_to_the_agent(env, monkeypatch):
    client, fake, store, _ = env
    _connect(client, fake)
    for path in ("/fd/payments/agent/list", "/fd/payments/agent/status"):
        text = client.get(path, headers=AGENT).text
        assert "HEP ELEKTRA" not in text and "Erste" not in text
    monkeypatch.delenv("FD_PAYMENTS_STATEMENTS")
    assert client.get("/fd/payments/statements").json() == {
        "enabled": False, "configured": True, "revolut_connected": False, "sync_times": ["07:30", "16:30"],
        "redirect_url": "", "sessions": []}
    assert client.post("/fd/payments/statements/connect", json={"name": BANK}).status_code == 404
    assert client.get("/fd/payments/statements/callback?code=x&state=y").status_code == 404
    assert asyncio.run(payments_jobs.tick())["synced"] == 0


def test_psu_headers_all_or_none():
    from starlette.requests import Request

    req = Request({"type": "http", "headers": [(b"user-agent", b"Phone"), (b"x-forwarded-for", b"203.0.113.7")],
                   "client": ("10.0.0.1", 1), "method": "GET", "path": "/"})
    assert sr.psu_headers(req, ["Psu-Ip-Address"]) == {"Psu-Ip-Address": "203.0.113.7", "Psu-User-Agent": "Phone"}
    assert sr.psu_headers(req, ["Psu-Ip-Address", "Psu-Geo-Location"]) is None


def test_retention_purges_only_unmatched_old_rows(env):
    _, _, store, _ = env
    old = (date.today() - timedelta(days=401)).isoformat()
    rows = [{"account_key": "k", "fp": f"fp-{i}", "status": "BOOK", "booking_date": d, "amount_cents": 100,
             "currency": "EUR"} for i, d in enumerate([old, old, date.today().isoformat()])]
    asyncio.run(store.upsert_bank_transactions(OWNER["id"], rows))

    async def link_one():
        db = await store._ensure_db()
        await db.execute("UPDATE bank_transactions SET intent_id = 'pay_x' WHERE fp = 'fp-1'")
        await db.commit()

    asyncio.run(link_one())
    assert asyncio.run(sr.purge()) == 1
    assert sorted(r["fp"] for r in asyncio.run(store.bank_transactions(OWNER["id"]))) == ["fp-1", "fp-2"]
