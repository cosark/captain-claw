"""Payments Phase 3a — purchases with a one-time card (Link Agent Wallet, a
Revolut card per purchase), against fakes.

Pins: nothing exists to pay with until the user approved this seller and
amount (in Link, or the owner in Flight Deck — never the agent); the card
leaves Flight Deck once; the ledger moves on the provider's word; a purchase
that won't happen takes its card / spend request with it.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import (
    auth, link_wallet, payments_routes as pr, purchases_routes as pur, revolut_business as rb,
)
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
AGENT = {"X-Agent-Auth": "tok-A"}
SHOP = {"source": "link", "merchant_name": "Stripe Press", "merchant_url": "https://press.stripe.com/working",
        "description": "The book the user asked for", "items": [{"name": "Working in Public", "unit_amount": "35.00"}],
        "amount": "35.00", "currency": "USD"}


class FakeLink:
    def __init__(self) -> None:
        self.requests: dict[str, dict] = {}
        self.bodies: list[dict] = []
        self.observations: list[dict] = []
        self.cancelled: list[str] = []
        self.token_forms: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "login.link.com":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.token_forms.append(form)
            assert request.headers["authorization"] == "Bearer pk_test_123"
            if path == "/auth/token":
                return httpx.Response(200, json={"access_token": "liwltoken_1", "refresh_token": "liwlrefresh_2",
                                                 "expires_in": 3600, "scope": "payment_methods.agentic userinfo:read"})
            return httpx.Response(200, json={})
        assert request.headers["authorization"] == "Bearer liwltoken_1"
        if path == "/spend_requests" and request.method == "POST":
            body = json.loads(request.content)
            self.bodies.append(body)
            sid = f"lsrq_{len(self.requests) + 1}"
            self.requests[sid] = {"id": sid, "status": "pending_approval", "credential_type": body["credential_type"],
                                  "approval_url": f"https://app.link.com/activity/approve/{sid}"}
            return httpx.Response(200, json=self.requests[sid])
        if path.startswith("/spend_requests/"):
            parts = path.split("/")
            sid = parts[2]
            if parts[-1] == "cancel":
                self.cancelled.append(sid)
                self.requests[sid]["status"] = "canceled"
                return httpx.Response(200, json=self.requests[sid])
            out = dict(self.requests[sid])
            if request.url.params.get("include") == "card":
                out["card"] = {"number": "4000009990001984", "cvc": "100", "exp_month": 6, "exp_year": 2029,
                               "billing_address": {"name": "Jenny Rosen"}}
            return httpx.Response(200, json=out)
        if path == "/agent_observations":
            self.observations.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "recorded"})
        if path == "/userinfo":
            return httpx.Response(200, json={"email": "jenny@example.com", "name": "Jenny Rosen"})
        return httpx.Response(404, json={"error": "no route"})


class FakeRevolut:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.terminated: list[str] = []
        self.transactions: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/1.0", "")
        if path == "/team-members":
            return httpx.Response(200, json=[{"id": "tm-owner", "role_id": "owner"}])
        if path == "/cards" and request.method == "POST":
            body = json.loads(request.content)
            cid = f"card-{len(self.cards) + 1}"
            self.cards[cid] = body
            return httpx.Response(200, json={"id": cid, "last_digits": "7788", "state": "active",
                                             "expiry": "10/2027", "virtual": True})
        if path.endswith("/sensitive-details"):
            return httpx.Response(200, json={"pan": "4111111111117788", "cvv": "321", "expiry": "10/2027"})
        if path.startswith("/cards/") and request.method == "DELETE":
            self.terminated.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if path == "/transactions":
            return httpx.Response(200, json=self.transactions)
        return httpx.Response(404, json={"message": "no route"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    monkeypatch.setenv("FD_PAYMENTS", "1")
    monkeypatch.setenv("FD_PUBLIC_URL", "https://deck.example.com")
    for k, v in {"FD_LINK_CLIENT_ID": "cid_1", "FD_LINK_CLIENT_SECRET": "sec_1",
                 "FD_LINK_PUBLISHABLE_KEY": "pk_test_123", "FD_LINK_TEST": "1"}.items():
        monkeypatch.setenv(k, v)
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    link, rev = FakeLink(), FakeRevolut()
    monkeypatch.setattr(link_wallet, "transport", httpx.MockTransport(link))
    monkeypatch.setattr(rb, "transport", httpx.MockTransport(rev))
    monkeypatch.setattr(server, "_agent_caller_ok", lambda request: True)
    monkeypatch.setattr(server, "_find_agent_by_auth", lambda t: (t == "tok-A", OWNER["id"], "slug"))
    app = FastAPI()
    app.include_router(pur.router)
    app.include_router(pr.router)
    app.include_router(pr.public_router)
    app.dependency_overrides[auth.get_current_user] = lambda: OWNER
    yield TestClient(app), link, rev, store
    asyncio.run(store.close())
    pr.set_store(None)
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


def _connect_link(client) -> None:
    url = client.post("/fd/payments/link/connect").json()["url"]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://login.link.com/auth?") and q["code_challenge_method"] == ["S256"]
    assert q["key"] == ["pk_test_123"] and "payment_methods.agentic" in q["scope"][0]
    assert q["redirect_uri"] == ["https://deck.example.com/fd/payments/link/callback"]
    page = client.get(f"/fd/payments/link/callback?code=c1&state={q['state'][0]}")
    assert "Link connected" in page.text


def _connect_revolut(store) -> None:
    asyncio.run(store.save_connection(OWNER["id"], "revolut", environment="sandbox", client_id="c",
                                      redirect_uri="https://deck.example.com/x", private_key="k",
                                      refresh_token="r", access_token="a", access_expires_at=time.time() + 3000,
                                      account_id="acc-eur", account_label="Main EUR"))
    asyncio.run(store.set_pref(OWNER["id"], "revolut_cards", True))


def test_link_connect_uses_pkce_and_the_decks_own_client(env):
    client, link, _, store = env
    _connect_link(client)
    form = link.token_forms[0]
    assert form["grant_type"] == "authorization_code" and form["client_secret"] == "sec_1"
    assert len(form["code_verifier"]) >= 43
    conn = asyncio.run(store.get_connection(OWNER["id"], "link"))
    assert conn["refresh_token"] == "liwlrefresh_2"
    assert client.get("/fd/payments/link/status").json()["user"]["email"] == "jenny@example.com"
    assert "Link not connected" in client.get("/fd/payments/link/callback?code=c&state=forged").text


def test_a_link_purchase_end_to_end(env):
    client, link, _, store = env
    _connect_link(client)
    p = client.post("/fd/payments/agent/purchases", json=SHOP, headers=AGENT).json()["purchase"]
    assert p["status"] == "awaiting_approval" and p["approval_url"].endswith("lsrq_1")
    body = link.bodies[0]
    assert body["amount"] == 3500 and body["currency"] == "usd" and body["merchant_url"] == SHOP["merchant_url"]
    assert len(body["context"]) >= 100 and body["idempotency_key"] == p["id"] and body["test"] is True
    pid = p["id"]
    # Nothing to pay with before the user approves in Link.
    assert client.post(f"/fd/payments/agent/purchases/{pid}/credential", headers=AGENT).status_code == 409
    link.requests["lsrq_1"]["status"] = "approved"
    assert client.get(f"/fd/payments/agent/purchases/{pid}", headers=AGENT).json()["purchase"]["status"] == "approved"
    cred = client.post(f"/fd/payments/agent/purchases/{pid}/credential", headers=AGENT).json()
    assert cred["kind"] == "card" and cred["card"]["number"].endswith("1984")
    # Once only.
    assert client.post(f"/fd/payments/agent/purchases/{pid}/credential", headers=AGENT).status_code == 409
    out = client.post(f"/fd/payments/agent/purchases/{pid}/result", headers=AGENT, json={
        "outcome": "confirmed", "excerpt": "Thank you! Order number SP-2049. Card 4242 4242 4242 4242",
        "url": "https://press.stripe.com/thanks"}).json()["purchase"]
    assert out["status"] == "ordered" and out["order_ref"] == "SP-2049"
    assert "4242 4242" not in json.dumps(out)                       # scrubbed before it's stored
    assert link.observations[0]["outcome"] == "success" and link.observations[0]["domain"] == "press.stripe.com"
    link.requests["lsrq_1"]["status"] = "succeeded"
    assert client.get(f"/fd/payments/purchases/{pid}").json()["purchase"]["status"] == "completed"


def test_card_data_and_bad_urls_are_refused(env):
    client, *_ = env
    _connect_link(client)
    assert client.post("/fd/payments/agent/purchases", headers=AGENT,
                       json={**SHOP, "description": "use 4111 1111 1111 1111"}).status_code == 422
    assert client.post("/fd/payments/agent/purchases", headers=AGENT,
                       json={**SHOP, "merchant_url": "http://shop.example"}).status_code == 422


def test_a_revolut_card_per_purchase(env):
    client, _, rev, store = env
    _connect_revolut(store)
    p = client.post("/fd/payments/agent/purchases", headers=AGENT, json={
        **SHOP, "source": "revolut_card", "amount": "42,50", "currency": "EUR",
        "merchant_name": "Knjižara", "merchant_url": "https://knjizara.example/kosarica"}).json()["purchase"]
    pid = p["id"]
    assert p["status"] == "awaiting_approval" and p["approval_url"] == f"/payments/purchases/{pid}"
    # The agent can't approve its own purchase.
    assert client.post(f"/fd/payments/agent/purchases/{pid}/approve", headers=AGENT).status_code in (404, 405)
    approved = client.post(f"/fd/payments/purchases/{pid}/approve").json()["purchase"]
    assert approved["status"] == "approved" and approved["card_last4"] == "7788"
    card = rev.cards["card-1"]
    assert card["spending_limits"] == {"single": {"amount": 42.5, "currency": "EUR"},
                                       "all_time": {"amount": 42.5, "currency": "EUR"}}
    assert card["contact_ids"] == ["tm-owner"] and card["accounts"] == ["acc-eur"]
    assert card["spending_period"]["end_date_action"] == "terminate"
    assert card["references"] == [{"name": "captain_purchase", "value": pid}]
    cred = client.post(f"/fd/payments/agent/purchases/{pid}/credential", headers=AGENT).json()
    assert cred["card"] == {"number": "4111111111117788", "cvc": "321", "exp_month": 10, "exp_year": 2027, "name": ""}
    client.post(f"/fd/payments/agent/purchases/{pid}/result", headers=AGENT, json={"outcome": "confirmed",
                                                                                  "excerpt": "Hvala na narudžbi"})
    rev.transactions.append({"id": "t1", "state": "completed", "card": {"id": "card-1"},
                             "merchant": {"name": "KNJIZARA"}})
    done = client.get(f"/fd/payments/purchases/{pid}").json()["purchase"]
    assert done["status"] == "completed" and rev.terminated == ["card-1"]


def test_a_card_that_never_reached_the_shop_can_be_tried_again_then_stops(env):
    client, _, rev, store = env
    _connect_revolut(store)
    pid = client.post("/fd/payments/agent/purchases", headers=AGENT, json={
        **SHOP, "source": "revolut_card", "currency": "EUR"}).json()["purchase"]["id"]
    client.post(f"/fd/payments/purchases/{pid}/approve")
    for attempt in range(3):
        assert client.post(f"/fd/payments/agent/purchases/{pid}/credential", headers=AGENT).status_code == 200
        out = client.post(f"/fd/payments/agent/purchases/{pid}/result", headers=AGENT,
                          json={"outcome": "no_button"}).json()["purchase"]
    assert out["status"] == "failed" and rev.terminated == ["card-1"]


def test_deny_cancels_the_spend_request(env):
    client, link, _, _ = env
    _connect_link(client)
    pid = client.post("/fd/payments/agent/purchases", json=SHOP, headers=AGENT).json()["purchase"]["id"]
    assert client.post(f"/fd/payments/purchases/{pid}/deny").json()["purchase"]["status"] == "denied"
    assert link.cancelled == ["lsrq_1"]


def test_not_connected_and_listing(env):
    client, *_ = env
    r = client.post("/fd/payments/agent/purchases", json=SHOP, headers=AGENT)
    assert r.status_code == 409 and "Link isn't connected" in r.json()["detail"]
    listing = client.get("/fd/payments/purchases").json()
    assert listing["purchases"] == [] and listing["sources"] == []


def test_link_unreachable_is_an_answer_not_a_crash(env, monkeypatch):
    client, *_ = env
    _connect_link(client)

    def down(request):
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setattr(link_wallet, "transport", httpx.MockTransport(down))
    r = client.post("/fd/payments/agent/purchases", json=SHOP, headers=AGENT)
    assert r.status_code == 502 and "Couldn't reach Link" in r.json()["detail"]
    assert client.get("/fd/payments/purchases").status_code == 200
