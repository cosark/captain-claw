"""Payments Phase 2 — pay-by-bank links through Enable Banking, against a fake.

Pins: every call carries an RS256 app token (kid = application id, the right
iss/aud, at most 24 h); the payment request carries the payee, the amount as a
string and the Croatian reference as text; the user gets one link to their own
bank; the bank's status — not anything Captain says — moves the ledger.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import auth, bank_link, payments_routes as pr, revolut_business as rb
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore

pytest.importorskip("cryptography")

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
HR = "HR1210010051863000160"
AGENT = {"X-Agent-Auth": "tok-A"}
APP_ID = "app-0000-1111"


class FakeEnableBanking:
    def __init__(self, certificate: str) -> None:
        self.certificate = certificate
        self.payments: dict[str, dict] = {}
        self.bodies: list[dict] = []
        self.status = "RCVD"
        self.final = False

    def _token_ok(self, request: httpx.Request) -> bool:
        from cryptography import x509

        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        header = jwt.get_unverified_header(token)
        public = x509.load_pem_x509_certificate(self.certificate.encode()).public_key()
        claims = jwt.decode(token, public, algorithms=["RS256"], audience="api.enablebanking.com")
        return (header.get("kid") == APP_ID and claims["iss"] == "enablebanking.com"
                and claims["exp"] - claims["iat"] <= 86400)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self._token_ok(request):
            return httpx.Response(401, json={"message": "Invalid JWT"})
        path = request.url.path
        if path == "/application":
            return httpx.Response(200, json={"name": "Captain", "environment": "SANDBOX"})
        if path == "/aspsps":
            assert request.url.params["service"] == "PIS"
            return httpx.Response(200, json={"aspsps": [
                {"name": "Zagrebačka banka", "country": "HR", "logo": "https://x/zaba.png", "beta": False,
                 "payments": [{"payment_type": "SEPA", "currencies": ["EUR"],
                               "remittance_information_lines": [{"max_length": 30}]}]},
                {"name": "AIS-only bank", "country": "HR", "payments": []}]})
        if path == "/payments" and request.method == "POST":
            body = json.loads(request.content)
            self.bodies.append(body)
            pid = f"pay-eb-{len(self.payments) + 1}"
            self.payments[pid] = body
            return httpx.Response(200, json={"payment_id": pid, "status": "RCVD", "psu_id_hash": "h",
                                             "url": f"https://bank.example/auth?payment={pid}"})
        if path.startswith("/payments/"):
            pid = path.rsplit("/", 1)[1]
            if pid not in self.payments:
                return httpx.Response(404, json={"message": "Payment not found", "error": "PAYMENT_NOT_FOUND"})
            return httpx.Response(200, json={
                "payment_id": pid, "status": self.status, "final_status": self.final,
                "status_reason_information": {"status_reason_code": "AM04",
                                              "status_reason_description": "Insufficient funds"}
                if self.status == "RJCT" else {}})
        return httpx.Response(404, json={"message": f"no route {path}"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    key, cert = rb.generate_key_and_certificate()
    key_path = tmp_path / "eb.pem"
    key_path.write_text(key)
    monkeypatch.setenv("FD_PAYMENTS", "1")
    monkeypatch.setenv("FD_ENABLEBANKING_APP_ID", APP_ID)
    monkeypatch.setenv("FD_ENABLEBANKING_KEY", str(key_path))
    monkeypatch.setenv("FD_PUBLIC_URL", "https://deck.example.com")
    monkeypatch.delenv("FD_ENABLEBANKING_REDIRECT_URL", raising=False)
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    bank_link._ASPSP_CACHE.clear()
    bank_link._APP_CACHE = (0.0, {})
    fake = FakeEnableBanking(cert)
    monkeypatch.setattr(bank_link, "transport", httpx.MockTransport(fake))
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


def _ready(client, **fields):
    body = {"payee_name": "HEP Elektra d.o.o.", "iban": HR, "amount": "84,20", "model": "HR01",
            "reference": "7269-68499637766-00019", "description": "Struja rujan", **fields}
    intent = client.post("/fd/payments/agent/prepare", json={"fields": body}, headers=AGENT).json()["intent"]
    assert intent["status"] == "ready", intent
    return intent


def _choose_bank(client):
    banks = client.get("/fd/payments/bank/aspsps?country=HR").json()["banks"]
    assert [b["name"] for b in banks] == ["Zagrebačka banka"]        # an AIS-only bank isn't offered
    assert client.put("/fd/payments/bank/choice", json={"name": "Zagrebačka banka"}).status_code == 200


def test_setup_status(env):
    client, *_ = env
    status = client.get("/fd/payments/bank/status").json()
    assert status["configured"] and status["environment"] == "sandbox"
    assert status["redirect_url"] == "https://deck.example.com/fd/payments/bank/callback"
    assert status["bank"] is None
    assert "bank" not in client.get("/fd/payments/agent/status", headers=AGENT).json()["rails"]
    _choose_bank(client)
    assert "bank" in client.get("/fd/payments/agent/status", headers=AGENT).json()["rails"]


def test_the_user_gets_one_link_to_their_bank(env):
    client, fake, _ = env
    _choose_bank(client)
    pid = _ready(client)["id"]
    out = client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "bank"}, headers=AGENT).json()
    assert out["url"] == "https://bank.example/auth?payment=pay-eb-1" and out["bank_name"] == "Zagrebačka banka"
    intent = out["intent"]
    assert intent["status"] == "handed_off" and intent["handoff_rail"] == "bank"
    assert intent["bank_link"]["url"] == out["url"] and "approve" in intent["bank_link"]["label"]
    body = fake.bodies[0]
    assert body["payment_type"] == "SEPA" and body["psu_type"] == "personal" and body["language"] == "hr"
    assert body["aspsp"] == {"name": "Zagrebačka banka", "country": "HR"}
    assert body["redirect_url"] == "https://deck.example.com/fd/payments/bank/callback"
    tx = body["payment_request"]["credit_transfer_transaction"][0]
    assert tx["instructed_amount"] == {"amount": "84.20", "currency": "EUR"}
    assert tx["beneficiary"] == {"creditor": {"name": "HEP Elektra d.o.o."},
                                 "creditor_account": {"scheme_name": "IBAN", "identification": HR}}
    assert tx["remittance_information"] == ["HR01 7269-68499637766-00019"]   # cut to the bank's 30
    assert "reference_number" not in tx


def test_an_rf_reference_goes_structured(env):
    client, fake, _ = env
    _choose_bank(client)
    pid = _ready(client, model="", reference="RF18539007547034", description="Invoice 7")["id"]
    client.post(f"/fd/payments/{pid}/bank")
    tx = fake.bodies[0]["payment_request"]["credit_transfer_transaction"][0]
    assert tx["reference_number"] == "RF18539007547034" and tx["remittance_information"] == ["Invoice 7"]


def test_the_banks_word_moves_the_ledger(env):
    client, fake, _ = env
    _choose_bank(client)
    pid = _ready(client)["id"]
    client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "bank"}, headers=AGENT)
    state = asyncio.run(env[2].get(pid))["rail_nonce"]
    fake.status = "ACTC"                                          # accepted, not final yet
    page = client.get(f"/fd/payments/bank/callback?state={state}")
    assert "Approved at your bank" in page.text
    assert client.get(f"/fd/payments/{pid}").json()["intent"]["status"] == "handed_off"
    fake.status, fake.final = "ACSP", True                        # final accepted
    intent = client.get(f"/fd/payments/agent/{pid}", headers=AGENT).json()["intent"]
    assert intent["status"] == "settled" and intent["paid_via"] == "bank"
    assert intent["bank_link"]["url"] == ""                            # no link once it's done


def test_rejected_and_not_approved(env):
    client, fake, store = env
    _choose_bank(client)
    pid = _ready(client)["id"]
    client.post(f"/fd/payments/{pid}/bank")
    state = asyncio.run(store.get(pid))["rail_nonce"]
    page = client.get(f"/fd/payments/bank/callback?state={state}&error=access_denied&error_description=User+cancelled")
    assert "Not paid" in page.text and "User cancelled" in page.text
    assert client.get(f"/fd/payments/{pid}").json()["intent"]["status"] == "handed_off"   # can hand over again
    again = client.post(f"/fd/payments/{pid}/bank").json()
    assert again["url"].endswith("pay-eb-2")
    fake.status, fake.final = "RJCT", True
    intent = client.get(f"/fd/payments/{pid}").json()["intent"]
    assert intent["status"] == "failed"
    assert any(w["code"] == "bank_rejected" and "Insufficient funds" in w["message"] for w in intent["warnings"])


def test_gates(env):
    client, _, _ = env
    pid = _ready(client)["id"]
    r = client.post(f"/fd/payments/agent/{pid}/handoff", json={"rail": "bank"}, headers=AGENT)
    assert r.status_code == 409 and "Choose your bank" in r.json()["detail"]
    _choose_bank(client)
    blocked = client.post("/fd/payments/agent/prepare", headers=AGENT, json={"fields": {
        "payee_name": "X", "iban": HR, "amount": "999999,00"}}).json()["intent"]
    assert blocked["status"] == "needs_review"
    assert client.post(f"/fd/payments/agent/{blocked['id']}/handoff", json={"rail": "bank"},
                       headers=AGENT).status_code == 409
    assert client.get("/fd/payments/bank/callback?state=nope").status_code == 404


def test_app_token_shape(env):
    token = bank_link.auth_token(lifetime=999999)
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False}, audience="api.enablebanking.com")
    assert header["kid"] == APP_ID and header["alg"] == "RS256"
    assert claims["iss"] == "enablebanking.com" and claims["exp"] - claims["iat"] == 86400


def test_status_mapping():
    assert bank_link.ledger_status("ACSC", False) == "settled"
    assert bank_link.ledger_status("ACTC", False) == ""
    assert bank_link.ledger_status("ACTC", True) == "settled"
    assert bank_link.ledger_status("RJCT", True) == "failed"
    assert bank_link.ledger_status("CNCL", True) == "cancelled"
    assert bank_link.ledger_status("CNCL", False) == ""
    assert bank_link.ledger_status("PDNG", False) == ""
