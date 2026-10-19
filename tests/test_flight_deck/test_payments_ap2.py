"""Payments Phase 3c — AP2 v0.2, Captain as the shopping agent, against a stub
merchant agent and credentials provider that follow the v0.2 sample sequence.

The owner — not the agent — approves the checkout; Flight Deck signs the
CheckoutMandate and PaymentMandate (SD-JWT, ES256) with the user's key; an
OTP challenge goes back to the owner. ``AP2_VERIFY_PYTHON`` (a Python with
Google's AP2 SDK installed) also checks the mandates with the SDK's own
``MandateClient().verify``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import textwrap

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import ap2_client, auth, payments_routes as pr, purchases_routes as pur
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore

pytest.importorskip("cryptography")

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
AGENT = {"X-Agent-Auth": "tok-A"}
MERCHANT = "http://localhost:8001/a2a/merchant_agent"
CP = "http://localhost:8002/a2a/credentials_provider"


class Stubs:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.public_pem = ""
        self.challenge = True

    @staticmethod
    def _task(state: str, *datas: dict, text: str = "", task_id: str = "t1") -> httpx.Response:
        parts = ([{"kind": "text", "text": text}] if text else []) + [{"kind": "data", "data": d} for d in datas]
        result = {"kind": "task", "id": task_id, "contextId": "ctx-1", "status": {"state": state, "message":
                  {"role": "agent", "parts": parts if state == "input-required" else []}},
                  "artifacts": [] if state == "input-required" else [{"artifactId": "a", "parts": parts}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-a2a-extensions"] == ap2_client.EXTENSION
        body = json.loads(request.content)
        assert body["method"] == "message/send"
        msg = body["params"]["message"]
        text = msg["parts"][0]["text"]
        data = {k: v for p in msg["parts"][1:] for k, v in p["data"].items()}
        self.calls.append({"url": str(request.url), "text": text, "data": data, "context": msg.get("contextId"),
                           "task": msg.get("taskId")})
        if str(request.url) == MERCHANT:
            if text.startswith("Find products"):
                return self._task("completed", {"ap2.cart": {"cart_id": "cart_1", "item_label": "Red shoes",
                                                             "amount": 120.0, "currency": "USD"}},
                                  {"ap2.cart": {"cart_id": "cart_2", "item_label": "Blue shoes",
                                                "amount": 80.0, "currency": "USD"}})
            if text.startswith("Create a checkout"):
                return self._task("completed", {"ap2.checkout": {
                    "cart_id": data["cart_id"], "checkout_jwt": "eyJhbGciOiJFUzI1NiJ9.e30.sig",
                    "checkout_hash": "hash_abc", "item_label": "Blue shoes", "amount_cents": 8000, "currency": "USD"}})
            if text.startswith("Initiate a payment"):
                cm = ap2_client.read_sd_jwt(data["ap2.mandates.CheckoutMandateSdJwt"], self.public_pem)
                pm = ap2_client.read_sd_jwt(data["ap2.mandates.PaymentMandateSdJwt"], self.public_pem)
                assert cm["checkout_hash"] == pm["transaction_id"] == "hash_abc"
                assert pm["payment_amount"] == {"amount": 8000, "currency": "USD"}
                if self.challenge:
                    return self._task("input-required", {"challenge": {"type": "otp", "display_text": "Enter the code"}},
                                      text="Please enter the code", task_id="pay-task")
                return self._task("completed", {"ap2.PaymentReceipt": {"payment_id": "pi_1", "status": "Success"}})
            if text.startswith("Here is the challenge response"):
                assert msg.get("taskId") == "pay-task"
                ok = data.get("challenge_response") == "123"
                return self._task("completed", {"ap2.PaymentReceipt": {"payment_id": "pi_2",
                                                                       "status": "Success" if ok else "Error"}})
        if str(request.url) == CP:
            if "shipping address" in text:
                return self._task("completed", {"contact_picker.ContactAddress": {"city": "Zagreb"}})
            if "payment methods" in text:
                return self._task("completed", {"payment_method_aliases": ["CARD-1"]})
            if "credential token" in text:
                return self._task("completed", {"token": "tok_1"})
            if "signed payment mandate" in text:
                return self._task("completed", {"ok": True})
        return httpx.Response(404)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    monkeypatch.setenv("FD_PAYMENTS", "1")
    monkeypatch.setenv("FD_AP2", "1")
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    stubs = Stubs()
    monkeypatch.setattr(ap2_client, "transport", httpx.MockTransport(stubs))
    monkeypatch.setattr(server, "_agent_caller_ok", lambda request: True)
    monkeypatch.setattr(server, "_find_agent_by_auth", lambda t: (t == "tok-A", OWNER["id"], "slug"))
    app = FastAPI()
    app.include_router(pur.router)
    app.include_router(pr.router)
    app.dependency_overrides[auth.get_current_user] = lambda: OWNER
    client = TestClient(app)
    settings = client.put("/fd/payments/ap2/settings", json={"credentials_provider_url": CP,
                                                              "user_email": "ana@example.com"}).json()
    stubs.public_pem = settings["public_key"]
    yield client, stubs, store
    asyncio.run(store.close())
    pr.set_store(None)
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


ORDER = {"source": "ap2", "merchant_name": "Shoe Store", "merchant_url": "https://shoes.example",
         "protocol_endpoint": MERCHANT, "description": "blue running shoes", "amount": "80", "currency": "USD"}


def test_an_ap2_purchase_with_an_otp(env):
    client, stubs, _ = env
    p = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT).json()["purchase"]
    assert p["status"] == "awaiting_approval" and p["amount_cents"] == 8000
    assert stubs.calls[0]["data"]["catalog_search"] == "blue running shoes"
    assert stubs.calls[1]["data"]["cart_id"] == "cart_2" and stubs.calls[1]["context"] == "ctx-1"
    assert not any("Initiate" in c["text"] for c in stubs.calls)            # nothing paid before approval
    assert client.post(f"/fd/payments/agent/purchases/{p['id']}/approve", headers=AGENT).status_code in (404, 405)
    p = client.post(f"/fd/payments/purchases/{p['id']}/approve").json()["purchase"]
    assert p["status"] == "needs_action" and p["next_action"]["display_message"] == "Enter the code"
    cp_texts = [c["text"] for c in stubs.calls if c["url"] == CP]
    assert cp_texts[0].startswith("Get the user's shipping address") and "signed payment mandate" in cp_texts[-1]
    done = client.post(f"/fd/payments/purchases/{p['id']}/challenge", json={"response": "123"}).json()["purchase"]
    assert done["status"] == "completed" and done["order_ref"] == "pi_2"


def test_a_wrong_otp_fails(env):
    client, stubs, _ = env
    pid = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT).json()["purchase"]["id"]
    client.post(f"/fd/payments/purchases/{pid}/approve")
    assert client.post(f"/fd/payments/purchases/{pid}/challenge", json={"response": "999"}).json()["purchase"]["status"] == "failed"


def test_off_without_the_flag(env, monkeypatch):
    client, *_ = env
    assert client.get("/fd/payments/purchases").json()["sources"] == ["ap2"]
    monkeypatch.delenv("FD_AP2")
    r = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT)
    assert r.status_code == 409 and "FD_AP2" in r.json()["detail"]
    assert client.get("/fd/payments/purchases").json()["sources"] == []


def test_sd_jwt_round_trip_hides_the_checkout_jwt_until_disclosed():
    private, public = ap2_client.generate_user_key()
    token = ap2_client.sd_jwt({"vct": "mandate.checkout.1", "checkout_jwt": "a.b.c", "checkout_hash": "h"},
                              private, "user-key-1", disclosable=("checkout_jwt",))
    issuer_jwt = token.split("~")[0]
    assert "a.b.c" not in issuer_jwt and token.endswith("~")
    assert ap2_client.read_sd_jwt(token, public) == {"vct": "mandate.checkout.1", "checkout_hash": "h",
                                                     "checkout_jwt": "a.b.c"}


@pytest.mark.skipif(not os.environ.get("AP2_VERIFY_PYTHON"), reason="set AP2_VERIFY_PYTHON to a Python with the AP2 SDK")
def test_googles_ap2_sdk_accepts_our_mandates(env):
    client, stubs, _ = env
    stubs.challenge = False
    pid = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT).json()["purchase"]["id"]
    p = client.post(f"/fd/payments/purchases/{pid}/approve").json()["purchase"]
    assert p["status"] == "completed" and p["order_ref"] == "pi_1"
    sent = next(c["data"] for c in stubs.calls if c["text"].startswith("Initiate a payment"))
    checkout, payment = sent["ap2.mandates.CheckoutMandateSdJwt"], sent["ap2.mandates.PaymentMandateSdJwt"]
    script = textwrap.dedent("""
        import json, sys
        from jwcrypto.jwk import JWK
        from ap2.sdk.mandate import MandateClient
        from ap2.sdk.generated.checkout_mandate import CheckoutMandate
        from ap2.sdk.generated.payment_mandate import PaymentMandate
        req = json.load(sys.stdin)
        key = JWK.from_pem(req["public"].encode())
        c = MandateClient().verify(req["checkout"], key, payload_type=CheckoutMandate)
        p = MandateClient().verify(req["payment"], key, payload_type=PaymentMandate)
        print(json.dumps({"checkout_hash": c.mandate_payload.checkout_hash, "checkout_jwt": c.mandate_payload.checkout_jwt,
                          "transaction_id": p.mandate_payload.transaction_id, "amount": p.mandate_payload.payment_amount.amount}))
    """)
    out = subprocess.run([os.environ["AP2_VERIFY_PYTHON"], "-I", "-c", script], capture_output=True, text=True,
                         input=json.dumps({"public": stubs.public_pem, "checkout": checkout, "payment": payment}), timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout) == {"checkout_hash": "hash_abc", "checkout_jwt": "eyJhbGciOiJFUzI1NiJ9.e30.sig",
                                      "transaction_id": "hash_abc", "amount": 8000}


def test_an_unreachable_merchant_agent_is_an_answer_not_a_crash(env, monkeypatch):
    client, *_ = env

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(ap2_client, "transport", httpx.MockTransport(down))
    r = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT)
    assert r.status_code == 502 and "Couldn't reach" in r.json()["detail"]
