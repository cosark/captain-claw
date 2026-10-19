"""Payments Phase 1 — Revolut Business payment drafts, against a fake Revolut.

Pins the promises: the token is asked for READ,WRITE only (never PAY), the
client assertion is signed with the user's key, a payee name Revolut's VoP
doesn't confirm needs the owner, a draft (never a payment) is created, and the
draft's state flows back — COMPLETED is the first "settled" Captain can show.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import auth, payments_routes as pr, revolut_business as rb
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore

pytest.importorskip("cryptography")

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
HR = "HR1210010051863000160"
AGENT = {"X-Agent-Auth": "tok-A"}
ORIGIN = {"Origin": "https://deck.example.com"}


class FakeRevolut:
    """Just enough of the Business API, checking the bearer and the assertion."""

    def __init__(self) -> None:
        self.certificate = ""
        self.client_id = "client-123456"
        self.access = "access-1"
        self.refresh = "refresh-1"
        self.calls: list[tuple[str, str, object]] = []
        self.vop: dict[str, str] = {}             # company_name → result_code
        self.counterparties: list[dict] = []
        self.drafts: dict[str, dict] = {}
        self.draft_state = "CREATED"
        self.fail_next_401 = False

    def _assertion_ok(self, token: str) -> bool:
        from cryptography import x509

        public = x509.load_pem_x509_certificate(self.certificate.encode()).public_key()
        claims = jwt.decode(token, public, algorithms=["RS256"], audience="https://revolut.com")
        return claims["sub"] == self.client_id and claims["iss"] == "deck.example.com"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/1.0", "")
        if path == "/auth/token":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.calls.append(("POST", path, form["grant_type"]))
            if not self._assertion_ok(form["client_assertion"]):
                return httpx.Response(400, json={"error": "invalid_client"})
            if form["grant_type"] == "authorization_code":
                if form["code"] != "good-code":
                    return httpx.Response(400, json={"error_description": "Invalid code"})
                return httpx.Response(200, json={"access_token": self.access, "token_type": "bearer",
                                                 "expires_in": 2399, "refresh_token": self.refresh})
            self.access = f"access-{int(self.access.split('-')[1]) + 1}"
            return httpx.Response(200, json={"access_token": self.access, "expires_in": 2399})
        if request.headers.get("authorization") != f"Bearer {self.access}" or self.fail_next_401:
            self.fail_next_401 = False
            return httpx.Response(401, json={"message": "The request should be authorized."})
        body = None if not request.content else __import__("json").loads(request.content)
        self.calls.append((request.method, path, body))
        if path == "/accounts":
            return httpx.Response(200, json=[
                {"id": "acc-gbp", "name": "GBP", "currency": "GBP", "state": "active", "balance": 1},
                {"id": "acc-eur", "name": "Main EUR", "currency": "EUR", "state": "active", "balance": 500}])
        if path == "/account-name-validation":
            name = body.get("company_name", "")
            code = self.vop.get(name, "matched")
            return httpx.Response(200, json={"result_code": code, "company_name": "HEP ELEKTRA D.O.O."})
        if path == "/counterparties":
            iban = request.url.params.get("iban")
            return httpx.Response(200, json=[c for c in self.counterparties if c["accounts"][0]["iban"] == iban])
        if path == "/counterparty":
            cp = {"id": f"cp-{len(self.counterparties) + 1}", "name": body["company_name"],
                  "accounts": [{"id": f"cpa-{len(self.counterparties) + 1}", "iban": body["iban"]}]}
            self.counterparties.append(cp)
            return httpx.Response(200, json=cp)
        if path == "/payment-drafts" and request.method == "POST":
            draft_id = f"draft-{len(self.drafts) + 1}"
            self.drafts[draft_id] = body
            return httpx.Response(201, json={"id": draft_id})
        if path.startswith("/payment-drafts/"):
            draft_id = path.rsplit("/", 1)[1]
            if draft_id not in self.drafts:
                return httpx.Response(404, json={"message": "Not found"})
            if request.method == "DELETE":
                del self.drafts[draft_id]
                return httpx.Response(204)
            return httpx.Response(200, json={"payments": [{"id": "p1", "state": self.draft_state,
                                                           "reason": "insufficient funds" if self.draft_state == "DECLINED" else ""}]})
        return httpx.Response(404, json={"message": f"no route {path}"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    monkeypatch.setenv("FD_PAYMENTS", "1")
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    pr._CONNECT_STATES.clear()
    fake = FakeRevolut()
    monkeypatch.setattr(rb, "transport", httpx.MockTransport(fake))
    monkeypatch.setattr(server, "_agent_caller_ok", lambda request: True)
    monkeypatch.setattr(server, "_find_agent_by_auth", lambda t: (t == "tok-A", OWNER["id"], "slug"))
    app = FastAPI()
    app.include_router(pr.router)
    app.include_router(pr.public_router)
    app.dependency_overrides[auth.get_current_user] = lambda: OWNER
    yield TestClient(app), fake, store
    asyncio.run(store.close())
    pr.set_store(None)
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


def _connect(client: TestClient, fake: FakeRevolut) -> dict:
    key = client.post("/fd/payments/revolut/key", json={"environment": "sandbox"}, headers=ORIGIN).json()
    fake.certificate = key["certificate"]
    assert key["redirect_uri"] == "https://deck.example.com/fd/payments/revolut/callback"
    client.put("/fd/payments/revolut/client", json={"client_id": fake.client_id, "environment": "sandbox"})
    url = client.post("/fd/payments/revolut/connect").json()["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    page = client.get(f"/fd/payments/revolut/callback?code=good-code&state={state}")
    assert "connected" in page.text
    return client.get("/fd/payments/revolut/status").json()


def _ready_payment(client: TestClient, **fields) -> dict:
    body = {"payee_name": "HEP Elektra d.o.o.", "iban": HR, "amount": "84,20", "model": "HR01",
            "reference": "7269-68499637766-00019", **fields}
    intent = client.post("/fd/payments/agent/prepare", json={"fields": body}, headers=AGENT).json()["intent"]
    assert intent["status"] == "ready", intent
    return intent


def test_consent_asks_for_read_write_never_pay(env):
    client, fake, _ = env
    client.post("/fd/payments/revolut/key", json={"environment": "sandbox"}, headers=ORIGIN)
    client.put("/fd/payments/revolut/client", json={"client_id": fake.client_id, "environment": "sandbox"})
    url = client.post("/fd/payments/revolut/connect").json()["url"]
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    assert parts.netloc == "sandbox-business.revolut.com" and parts.path == "/app-confirm"
    assert query["scope"] == ["READ,WRITE"] and "PAY" not in query["scope"][0].split(",")
    assert query["redirect_uri"] == ["https://deck.example.com/fd/payments/revolut/callback"]


def test_connect_signs_with_the_users_key_and_hides_secrets(env):
    client, fake, store = env
    status = _connect(client, fake)
    assert status["connected"] and status["account_id"] == "acc-eur" and status["scope"] == "READ,WRITE"
    assert "private_key" not in status and "refresh_token" not in status and "access_token" not in status
    conn = asyncio.run(store.get_connection(OWNER["id"], "revolut"))
    assert conn["refresh_token"] == "refresh-1" and conn["environment"] == "sandbox"
    assert ("POST", "/auth/token", "authorization_code") in fake.calls


def test_bad_code_and_unknown_state(env):
    client, fake, _ = env
    client.post("/fd/payments/revolut/key", json={}, headers=ORIGIN)
    fake.certificate = client.get("/fd/payments/revolut/status").json()["certificate"]
    client.put("/fd/payments/revolut/client", json={"client_id": fake.client_id})
    url = client.post("/fd/payments/revolut/connect").json()["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    assert "Invalid code" in client.get(f"/fd/payments/revolut/callback?code=bad&state={state}").text
    # A state nobody issued: the page hands the code over for pasting instead.
    assert "Copy" in client.get("/fd/payments/revolut/callback?code=good-code&state=forged").text
    assert not client.get("/fd/payments/revolut/status").json()["connected"]


def test_handoff_to_revolut_makes_a_draft_never_a_payment(env):
    client, fake, _ = env
    _connect(client, fake)
    assert client.get("/fd/payments/agent/status", headers=AGENT).json()["rails"] == ["barcode", "revolut"]
    pid = _ready_payment(client)["id"]
    out = client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "revolut"}, headers=AGENT).json()
    intent = out["intent"]
    assert intent["status"] == "handed_off" and intent["handoff_rail"] == "revolut"
    assert intent["revolut"]["draft_id"] == "draft-1" and intent["revolut"]["vop"]["result_code"] == "matched"
    draft = fake.drafts["draft-1"]["payments"][0]
    assert draft == {"account_id": "acc-eur", "receiver": {"counterparty_id": "cp-1", "account_id": "cpa-1"},
                     "amount": 84.2, "currency": "EUR", "reference": "HR01 7269-68499637766-00019"}
    assert all(path != "/pay" and path != "/transfer" for _, path, _ in fake.calls)
    assert "reference_as_text" in [w["code"] for w in intent["warnings"]]
    # Same payment again: the same draft, no second one.
    again = client.post(f"/fd/payments/{pid}/revolut").json()
    assert again["existing"] and len(fake.drafts) == 1
    # A second payment to the same IBAN reuses the counterparty.
    pid2 = _ready_payment(client, amount="12,00", reference="7269-1")["id"]
    client.post(f"/fd/payments/agent/{pid2}/handoff", json={"rail": "revolut"}, headers=AGENT)
    assert len(fake.counterparties) == 1


def test_a_name_vop_doesnt_confirm_needs_the_owner(env):
    client, fake, _ = env
    _connect(client, fake)
    fake.vop["HEP Elektra d.o.o."] = "close_match"
    pid = _ready_payment(client)["id"]
    r = client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "revolut"}, headers=AGENT)
    assert r.status_code == 409 and not fake.drafts
    intent = client.get(f"/fd/payments/{pid}").json()["intent"]
    assert intent["status"] == "needs_review"
    vop = next(w for w in intent["warnings"] if w["code"] == "vop_close_match")
    assert vop["severity"] == "blocking" and "HEP ELEKTRA D.O.O." in vop["message"]
    # The agent can't get past it; the owner can.
    assert client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "revolut"},
                       headers=AGENT).status_code == 409
    assert client.post(f"/fd/payments/{pid}/ack", json={"codes": ["vop_close_match"]}).json()["intent"]["status"] == "ready"
    vop_calls = sum(1 for _, path, _ in fake.calls if path == "/account-name-validation")
    assert client.post(f"/fd/payments/{pid}/revolut").status_code == 200
    assert sum(1 for _, path, _ in fake.calls if path == "/account-name-validation") == vop_calls
    assert fake.drafts


def test_the_drafts_state_flows_back(env):
    client, fake, _ = env
    _connect(client, fake)
    pid = _ready_payment(client)["id"]
    client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "revolut"}, headers=AGENT)
    fake.draft_state = "PENDING"
    assert client.get(f"/fd/payments/{pid}").json()["intent"]["revolut"]["state"] == "PENDING"
    fake.draft_state = "COMPLETED"
    intent = client.get(f"/fd/payments/agent/{pid}", headers=AGENT).json()["intent"]
    assert intent["status"] == "settled" and intent["paid_via"] == "revolut"


def test_declined_and_deleted_drafts(env):
    client, fake, _ = env
    _connect(client, fake)
    pid = _ready_payment(client)["id"]
    client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "revolut"}, headers=AGENT)
    fake.draft_state = "DECLINED"
    intent = client.get("/fd/payments?status=").json()["payments"][0]
    assert intent["status"] == "failed"
    assert any(w["code"] == "revolut_failed" and "insufficient funds" in w["message"] for w in intent["warnings"])
    pid2 = _ready_payment(client, amount="5,00", reference="7269-2")["id"]
    client.post(f"/fd/payments/agent/{pid2}/handoff", json={"rail": "revolut"}, headers=AGENT)
    fake.drafts.clear()                                   # deleted in the app
    assert client.get(f"/fd/payments/{pid2}").json()["intent"]["status"] == "cancelled"


def test_skipping_in_captain_deletes_the_unsent_draft(env):
    client, fake, _ = env
    _connect(client, fake)
    pid = _ready_payment(client)["id"]
    client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "revolut"}, headers=AGENT)
    assert fake.drafts
    client.post(f"/fd/payments/{pid}/mark", json={"status": "skipped"})
    assert not fake.drafts


def test_tokens_refresh_on_expiry_and_on_401(env):
    client, fake, store = env
    _connect(client, fake)
    asyncio.run(store.save_connection(OWNER["id"], "revolut", access_expires_at=time.time() - 1))
    assert client.get("/fd/payments/revolut/accounts").status_code == 200
    assert ("POST", "/auth/token", "refresh_token") in fake.calls and fake.access == "access-2"
    fake.fail_next_401 = True
    assert client.get("/fd/payments/revolut/accounts").json()["accounts"][0]["id"] == "acc-eur"
    assert fake.access == "access-3"
    assert asyncio.run(store.get_connection(OWNER["id"], "revolut"))["access_token"] == "access-3"


def test_not_connected_and_disconnect(env):
    client, fake, _ = env
    pid = _ready_payment(client)["id"]
    r = client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "revolut"}, headers=AGENT)
    assert r.status_code == 409 and "isn't connected" in r.json()["detail"]
    _connect(client, fake)
    assert client.delete("/fd/payments/revolut/connection").json()["connected"] is False
    assert client.get("/fd/payments/agent/status", headers=AGENT).json()["rails"] == ["barcode"]


def test_client_assertion_claims():
    key, cert = rb.generate_key_and_certificate()
    conn = {"client_id": "cid-1", "redirect_uri": "https://deck.example.com:8443/fd/payments/revolut/callback",
            "private_key": key}
    from cryptography import x509

    claims = jwt.decode(rb.client_assertion(conn), x509.load_pem_x509_certificate(cert.encode()).public_key(),
                        algorithms=["RS256"], audience="https://revolut.com")
    assert claims["iss"] == "deck.example.com" and claims["sub"] == "cid-1"
    assert isinstance(claims["exp"], int) and 0 < claims["exp"] - time.time() <= 300
    assert jwt.get_unverified_header(rb.client_assertion(conn)) == {"alg": "RS256", "typ": "JWT"}
