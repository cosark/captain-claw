"""Payments Phase 3b — ACP (spec 2026-04-17) buyer side, against a stub shop.

The stub answers with the spec's own examples (tests/fixtures/acp) and
validates every request Captain sends against the official JSON Schema — so
these tests check conformance, not just our own idea of the protocol. Payment
is a Link shared payment token the user approved; no card exists in Captain.
"""

from __future__ import annotations

import asyncio
import copy
import json
import urllib.parse
from pathlib import Path

import httpx
import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import (
    auth, link_wallet, payments_routes as pr, protocol_checkout as pc, purchases_routes as pur,
)
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "acp"
SCHEMA = json.loads((FIX / "schema.agentic_checkout.json").read_text())
EXAMPLES = json.loads((FIX / "examples.agentic_checkout.json").read_text())
OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
AGENT = {"X-Agent-Auth": "tok-A"}
API = "https://api.shop.example/acp"


def _validate(defn: str, body: dict) -> None:
    schema = {"$schema": SCHEMA.get("$schema"), "$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{defn}"}
    jsonschema.validators.validator_for(schema)(schema).validate(body)


class StubShop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.decline = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, url, body, dict(request.headers)))
        if url == "https://shop.example/.well-known/acp.json":
            return httpx.Response(200, json={"protocol": {"name": "acp", "version": "2026-04-17",
                                                          "supported_versions": ["2026-01-30", "2026-04-17"]},
                                             "api_base_url": API, "transports": ["rest"],
                                             "capabilities": {"services": ["checkout"]}})
        assert request.headers["authorization"] == "Bearer shop_key_1"
        assert request.headers["api-version"] == "2026-04-17"
        if request.method == "POST":
            assert request.headers.get("idempotency-key")
        path = url.removeprefix(API)
        if path == "/checkout_sessions":
            _validate("CheckoutSessionCreateRequest", body)
            session = copy.deepcopy(EXAMPLES["create_checkout_session_response"])
            session.pop("selected_fulfillment_options", None)
            session["status"] = "not_ready_for_payment"
            return httpx.Response(201, json=session)
        if path.endswith("/complete"):
            _validate("CheckoutSessionCompleteRequest", body)
            if self.decline:
                return httpx.Response(200, json=copy.deepcopy(EXAMPLES["checkout_session_with_payment_declined"]))
            return httpx.Response(200, json=copy.deepcopy(EXAMPLES["complete_checkout_session_response"]))
        if path.endswith("/cancel"):
            return httpx.Response(200, json=copy.deepcopy(EXAMPLES["cancel_checkout_session_response"]))
        if path.startswith("/checkout_sessions/"):
            _validate("CheckoutSessionUpdateRequest", body)
            return httpx.Response(200, json=copy.deepcopy(EXAMPLES["create_checkout_session_response"]))
        return httpx.Response(404, json={"type": "invalid_request", "code": "not_found", "message": "no"})


class FakeLink:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.status = "pending_approval"
        self.cancelled = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.link.com":
            return httpx.Response(200, json={"access_token": "liwltoken_1", "refresh_token": "r2", "expires_in": 3600})
        path = request.url.path
        if path == "/spend_requests":
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "lsrq_1", "status": "pending_approval",
                                             "approval_url": "https://app.link.com/activity/approve/lsrq_1"})
        if path.endswith("/cancel"):
            self.cancelled = True
            return httpx.Response(200, json={"id": "lsrq_1", "status": "canceled"})
        out = {"id": "lsrq_1", "status": self.status}
        if self.status == "approved":
            out["shared_payment_token"] = {"id": "spt_123", "valid_until": "2026-10-20T00:00:00Z"}
        return httpx.Response(200, json=out)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    monkeypatch.setenv("FD_PAYMENTS", "1")
    for k, v in {"FD_LINK_CLIENT_ID": "cid", "FD_LINK_CLIENT_SECRET": "sec", "FD_LINK_PUBLISHABLE_KEY": "pk"}.items():
        monkeypatch.setenv(k, v)
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    pc._DISCOVERY.clear()
    shop, link = StubShop(), FakeLink()
    monkeypatch.setattr(pc, "transport", httpx.MockTransport(shop))
    monkeypatch.setattr(link_wallet, "transport", httpx.MockTransport(link))
    monkeypatch.setattr(server, "_agent_caller_ok", lambda request: True)
    monkeypatch.setattr(server, "_find_agent_by_auth", lambda t: (t == "tok-A", OWNER["id"], "slug"))
    asyncio.run(store.save_connection(OWNER["id"], "link", refresh_token="r", access_token="liwltoken_1",
                                      access_expires_at=9e12))
    app = FastAPI()
    app.include_router(pur.router)
    app.include_router(pr.router)
    app.dependency_overrides[auth.get_current_user] = lambda: OWNER
    yield TestClient(app), shop, link, store
    asyncio.run(store.close())
    pr.set_store(None)
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


ORDER = {"source": "acp", "merchant_name": "Test Shop", "merchant_url": "https://shop.example/p/123",
         "protocol_endpoint": "shop.example", "amount": "3.00", "currency": "USD",
         "items": [{"name": "Socks", "id": "item_123"}],
         "shipping": {"name": "John Doe", "line_one": "1234 Chat Road", "city": "San Francisco", "state": "CA",
                      "country": "US", "postal_code": "94131", "email": "johndoe@example.com"}}


def _add_shop(client) -> None:
    r = client.put("/fd/payments/acp/merchants", json={"domain": "shop.example", "api_key": "shop_key_1"})
    assert r.status_code == 200 and r.json()["merchants"][0]["domain"] == "shop.example"
    assert "shop_key_1" not in r.text


def test_an_acp_purchase_end_to_end(env):
    client, shop, link, _ = env
    _add_shop(client)
    p = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT).json()["purchase"]
    assert p["status"] == "awaiting_approval" and p["approval_url"].endswith("lsrq_1")
    total = next(t["amount"] for t in EXAMPLES["create_checkout_session_response"]["totals"] if t["type"] == "total")
    assert p["amount_cents"] == total and any(w["code"] == "merchant_total" for w in p["warnings"])
    sr = link.bodies[0]
    assert sr["credential_type"] == "shared_payment_token" and sr["network_id"] == "acct_1234567890"
    assert sr["amount"] == total and "merchant_name" not in sr
    create = next(c for c in shop.calls if c[1] == f"{API}/checkout_sessions")
    assert create[2]["line_items"] == [{"id": "item_123"}] and create[2]["fulfillment_details"]["address"]["city"] == "San Francisco"
    # Nothing to pay with until Link says approved; then Flight Deck completes server-side.
    assert client.post(f"/fd/payments/agent/purchases/{p['id']}/credential", headers=AGENT).status_code == 409
    link.status = "approved"
    done = client.get(f"/fd/payments/agent/purchases/{p['id']}", headers=AGENT).json()["purchase"]
    assert done["status"] == "completed" and done["order_ref"] == "#1001"
    complete = next(c for c in shop.calls if c[1].endswith("/complete"))
    assert complete[2]["payment_data"] == {"handler_id": "card_tokenized",
                                           "instrument": {"type": "card", "credential": {"type": "spt", "token": "spt_123"}}}
    assert complete[3]["idempotency-key"] == f"{p['id']}-complete"


def test_a_declined_payment(env):
    client, shop, link, _ = env
    _add_shop(client)
    shop.decline = True
    pid = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT).json()["purchase"]["id"]
    link.status = "approved"
    p = client.get(f"/fd/payments/purchases/{pid}").json()["purchase"]
    assert p["status"] == "failed" and any(w["code"] == "acp_payment_declined" for w in p["warnings"])


def test_denied_in_link_cancels_the_checkout(env):
    client, shop, link, _ = env
    _add_shop(client)
    pid = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT).json()["purchase"]["id"]
    link.status = "denied"
    assert client.get(f"/fd/payments/purchases/{pid}").json()["purchase"]["status"] == "denied"
    assert any(c[1].endswith("/cancel") for c in shop.calls)


def test_needs_the_shops_key_and_item_ids(env):
    client, *_ = env
    assert client.get("/fd/payments/purchases").json()["sources"] == ["link"]
    r = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT)
    assert r.status_code == 409 and "ACP key" in r.json()["detail"]
    _add_shop(client)
    assert client.get("/fd/payments/purchases").json()["sources"] == ["link", "acp"]
    r = client.post("/fd/payments/agent/purchases", headers=AGENT,
                    json={**ORDER, "items": [{"name": "Socks"}]})
    assert r.status_code == 422 and "item id" in r.json()["detail"]


def test_the_owner_cant_approve_an_acp_purchase_outside_link(env):
    client, *_ = env
    _add_shop(client)
    pid = client.post("/fd/payments/agent/purchases", json=ORDER, headers=AGENT).json()["purchase"]["id"]
    r = client.post(f"/fd/payments/purchases/{pid}/approve")
    assert r.status_code == 409 and "Link" in r.json()["detail"]


def test_an_unreachable_shop_is_an_answer_not_a_crash(env, monkeypatch):
    client, *_ = env

    def down(request):
        raise httpx.ConnectError("nodename nor servname provided", request=request)

    monkeypatch.setattr(pc, "transport", httpx.MockTransport(down))
    r = client.put("/fd/payments/acp/merchants", json={"domain": "nowhere.invalid", "api_key": "k"})
    assert r.status_code == 422 and "Couldn't read nowhere.invalid" in r.json()["detail"]
    monkeypatch.setattr(pc, "transport", httpx.MockTransport(lambda req: httpx.Response(200, text="<html>")))
    r = client.put("/fd/payments/acp/merchants", json={"domain": "html.example", "api_key": "k"})
    assert r.status_code == 422
