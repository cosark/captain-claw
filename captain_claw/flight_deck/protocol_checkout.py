"""Checkout protocols for purchases — ACP (OpenAI/Stripe) and AP2 (Google).

Payments Phase 3 (docs/payments-phase3-plan.md).

**ACP** (spec 2026-04-17, buyer side). The shop hosts a REST checkout:
Captain finds it at ``https://<shop>/.well-known/acp.json``, creates a checkout
session with the shop's own item ids, adds shipping and picks the cheapest
fulfillment option, and takes the **shop's** total. It then asks the user's
Link wallet for a *shared payment token* for exactly that total (the user
approves it in Link), and completes the checkout server-side with
``payment_data {handler_id, instrument {type: card, credential {type: spt,
token}}}``. No card number exists anywhere in Captain.

The shop's API key is the user's: ACP has no open agent registration — each
shop (or its platform) onboards an agent bilaterally — so the user adds a key
per shop on the Payments page.

**AP2** (v0.2) is Phase 3c.
"""

from __future__ import annotations

import time
import urllib.parse
import uuid
from typing import Any

import httpx
from fastapi import HTTPException

from captain_claw.logging import get_logger

log = get_logger(__name__)

PROTOCOLS = ("acp", "ap2")
ACP_VERSION = "2026-04-17"
ACP_PREF = "acp_merchants"
transport: httpx.AsyncBaseTransport | None = None        # tests swap in a MockTransport
_DISCOVERY: dict[str, tuple[float, dict]] = {}
_DISCOVERY_TTL = 3600


class ProtocolError(Exception):
    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _pur():
    from captain_claw.flight_deck import purchases_routes

    return purchases_routes


def enabled(kind: str) -> bool:
    if kind == "acp":
        from captain_claw.flight_deck import link_wallet

        return link_wallet.configured()        # ACP checkouts are paid with a Link shared payment token
    if kind == "ap2":
        from captain_claw.flight_deck import ap2_client

        return ap2_client.enabled()
    return False


def domain_of(value: str) -> str:
    value = (value or "").strip()
    if "://" not in value:
        value = f"https://{value}"
    return (urllib.parse.urlsplit(value).hostname or "").lower()


# ── ACP plumbing ──────────────────────────────────────────────────────

async def merchants(owner: str) -> dict[str, dict]:
    return await _pur().store().get_pref(owner, ACP_PREF) or {}


async def discover(domain: str) -> dict:
    cached = _DISCOVERY.get(domain)
    if cached and time.time() - cached[0] < _DISCOVERY_TTL:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False) as client:
            resp = await client.get(f"https://{domain}/.well-known/acp.json")
        doc = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        raise ProtocolError(502, f"Couldn't read {domain}'s ACP discovery ({type(exc).__name__})")
    if not isinstance(doc, dict):
        raise ProtocolError(resp.status_code, f"{domain} doesn't publish an ACP checkout (/.well-known/acp.json)")
    base = str(doc.get("api_base_url") or "")
    proto = doc.get("protocol") or {}
    if proto.get("name") != "acp" or not base.startswith("https://"):
        raise ProtocolError(502, f"{domain}'s ACP discovery document isn't usable")
    versions = set(proto.get("supported_versions") or [proto.get("version")])
    if ACP_VERSION not in versions:
        raise ProtocolError(502, f"{domain} speaks ACP {', '.join(sorted(v for v in versions if v))}, "
                                 f"not {ACP_VERSION}")
    _DISCOVERY[domain] = (time.time(), doc)
    return doc


async def acp_call(method: str, base: str, path: str, key: str, *, json: Any = None,
                   idempotency_key: str = "") -> dict:
    headers = {"Authorization": f"Bearer {key}", "API-Version": ACP_VERSION, "Request-Id": uuid.uuid4().hex}
    if method == "POST":
        headers["Idempotency-Key"] = idempotency_key or uuid.uuid4().hex
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            resp = await client.request(method, f"{base.rstrip('/')}{path}", json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise ProtocolError(502, f"Couldn't reach the shop ({type(exc).__name__})")
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = None
    if resp.status_code >= 400:
        body = body if isinstance(body, dict) else {}
        raise ProtocolError(resp.status_code, str(body.get("message") or resp.text[:200]), str(body.get("code") or ""))
    if not isinstance(body, dict):
        raise ProtocolError(502, "The shop's answer isn't JSON")
    return body


def _errors(session: dict) -> list[dict]:
    return [m for m in session.get("messages") or [] if m.get("type") == "error"]


def _total(session: dict) -> int:
    return next((int(t.get("amount") or 0) for t in session.get("totals") or [] if t.get("type") == "total"), 0)


def _stripe_card_handler(session: dict) -> dict | None:
    handlers = ((session.get("capabilities") or {}).get("payment") or {}).get("handlers") or []
    return next((h for h in handlers if h.get("psp") == "stripe" and "tokenized.card" in str(h.get("name"))), None)


def _address(shipping: dict) -> dict | None:
    need = ("name", "line_one", "city", "state", "country", "postal_code")
    if not shipping or not all(shipping.get(k) for k in need):
        return None
    addr = {k: str(shipping[k])[:200] for k in (*need, "line_two") if shipping.get(k)}
    out: dict[str, Any] = {"name": addr["name"], "address": addr}
    for key in ("email", "phone_number"):
        if shipping.get(key):
            out[key] = str(shipping[key])[:200]
    return out


# ── purchase hooks ───────────────────────────────────────────────────

async def start(row: dict, endpoint: str) -> dict:
    """Open the protocol checkout for a new purchase (row not yet stored)."""
    if row["source"] == "ap2":
        from captain_claw.flight_deck import ap2_client

        return await ap2_client.start(row, endpoint)
    return await _acp_start(row, endpoint)


async def _acp_start(row: dict, endpoint: str) -> dict:
    from captain_claw.flight_deck import link_wallet
    from captain_claw.payments import checks

    owner = row["owner_id"]
    domain = domain_of(endpoint or row["merchant_url"])
    creds = (await merchants(owner)).get(domain)
    if not creds:
        raise HTTPException(409, f"Captain has no ACP key for {domain}. Add the shop's ACP key on the Payments page.")
    conn = await _pur().link_conn(owner)
    if not conn:
        raise HTTPException(409, "ACP checkouts are paid with a Link shared payment token — connect Link first.")
    items = row.get("items") or []
    if not items or not all(i.get("id") for i in items):
        raise HTTPException(422, "ACP needs the shop's own item id for every item (items[].id).")
    line_items = [{"id": str(i["id"])} for i in items for _ in range(max(1, int(i.get("quantity") or 1)))]
    body: dict[str, Any] = {"currency": row["currency"].lower(), "line_items": line_items,
                            "capabilities": {"interventions": {"supported": []}}}
    fulfillment = _address((row.get("provider_state") or {}).get("shipping") or {})
    if fulfillment:
        body["fulfillment_details"] = fulfillment
    try:
        doc = await discover(domain)
        base = doc["api_base_url"]
        session = await acp_call("POST", base, "/checkout_sessions", creds["api_key"], json=body,
                                 idempotency_key=f"{row['id']}-create")
        options = session.get("fulfillment_options") or []
        if options and not session.get("selected_fulfillment_options"):
            cheapest = min(options, key=lambda o: next((t.get("amount", 0) for t in o.get("totals") or []
                                                        if t.get("type") == "total"), 0))
            session = await acp_call("POST", base, f"/checkout_sessions/{session['id']}", creds["api_key"], json={
                "selected_fulfillment_options": [{"type": cheapest["type"], "option_id": cheapest["id"],
                                                  "item_ids": [li["id"] for li in session.get("line_items") or []]}]},
                idempotency_key=f"{row['id']}-fulfillment")
    except ProtocolError as exc:
        raise HTTPException(502, f"{domain}'s checkout refused it: {exc.message}")
    problems = _errors(session)
    if problems:
        raise HTTPException(409, f"{domain}: " + " ".join(str(m.get("content")) for m in problems))
    if session.get("status") != "ready_for_payment":
        raise HTTPException(409, f"{domain}'s checkout isn't ready for payment ({session.get('status')}) — "
                                 "it may need a shipping address (shipping).")
    handler = _stripe_card_handler(session)
    if not handler:
        raise HTTPException(409, f"{domain} doesn't take a Stripe shared payment token through ACP.")
    total = _total(session)
    currency = str(session.get("currency") or row["currency"]).upper()
    config = handler.get("config") or {}
    network_id = str(config.get("network_id") or config.get("network_business_profile") or config.get("merchant_id") or "")
    warnings = list(row.get("warnings") or [])
    if total != row["amount_cents"]:
        warnings.append({"code": "merchant_total", "severity": "info",
                         "message": f"The shop's checkout total is {checks.format_amount(total, currency)} "
                                    f"(asked for {checks.format_amount(row['amount_cents'], row['currency'])}), "
                                    "with shipping and tax — that's what you approve."})
    try:
        sr = await _pur().link_client(conn).create_spend_request(
            amount_cents=total, currency=currency, merchant_name=row["merchant_name"], merchant_url=row["merchant_url"],
            context=link_wallet.context_text(row["merchant_name"], row["merchant_url"], row["description"], items),
            line_items=[{"name": li.get("name") or (li.get("item") or {}).get("id", "item"),
                         "quantity": li.get("quantity", 1)} for li in session.get("line_items") or []],
            idempotency_key=row["id"], credential_type="shared_payment_token", network_id=network_id)
    except link_wallet.LinkError as exc:
        await _safe_cancel(base, session["id"], creds["api_key"], row["id"])
        raise HTTPException(502, f"Link refused the spend request: {exc.message}")
    return {**row, "amount_cents": total, "currency": currency, "warnings": warnings,
            "approval_url": sr.get("approval_url", ""),
            "provider_state": {"acp": {"domain": domain, "api_base_url": base, "session_id": session["id"],
                                       "handler_id": handler["id"], "network_id": network_id,
                                       "status": session.get("status")},
                               "spend_request_id": sr.get("id"), "credential_type": "shared_payment_token",
                               "link_status": sr.get("status"), "test": link_wallet.test_mode()}}


async def _safe_cancel(base: str, session_id: str, key: str, purchase_id: str) -> None:
    try:
        await acp_call("POST", base, f"/checkout_sessions/{session_id}/cancel", key, json={},
                       idempotency_key=f"{purchase_id}-cancel")
    except ProtocolError as exc:
        if exc.status != 405:   # already completed / cancelled
            log.info("ACP cancel failed", error=exc.message)


async def sync(row: dict) -> dict:
    """ACP: once the user approves the shared payment token in Link, complete the
    checkout (server-side); then follow the order."""
    if row["source"] == "ap2":
        from captain_claw.flight_deck import ap2_client

        return await ap2_client.sync(row)
    from captain_claw.flight_deck import link_wallet

    if row["status"] not in ("awaiting_approval", "needs_action"):
        return row
    pur = _pur()
    store = pur.store()
    ps = dict(row.get("provider_state") or {})
    acp = dict(ps.get("acp") or {})
    conn = await pur.link_conn(row["owner_id"])
    creds = (await merchants(row["owner_id"])).get(acp.get("domain", ""))
    if not conn or not creds:
        return row
    sr = await pur.link_client(conn).get_spend_request(ps["spend_request_id"])
    ps["link_status"] = sr.get("status")
    target = link_wallet.STATUS_MAP.get(str(sr.get("status") or ""), "")
    if target in ("denied", "expired", "cancelled", "failed"):
        await _safe_cancel(acp["api_base_url"], acp["session_id"], creds["api_key"], row["id"])
        return await store.update_purchase(row["id"], status=target, provider_state=ps)
    if target != "approved":
        return await store.update_purchase(row["id"], provider_state=ps)
    token = sr.get("shared_payment_token")
    token = token.get("id") if isinstance(token, dict) else token
    if not token:
        return await store.update_purchase(row["id"], provider_state=ps)
    payload = {"payment_data": {"handler_id": acp["handler_id"],
                                "instrument": {"type": "card", "credential": {"type": "spt", "token": token}}}}
    try:
        session = await acp_call("POST", acp["api_base_url"], f"/checkout_sessions/{acp['session_id']}/complete",
                                 creds["api_key"], json=payload, idempotency_key=f"{row['id']}-complete")
    except ProtocolError as exc:
        if exc.code == "requires_3ds":
            return await store.update_purchase(row["id"], status="needs_action", provider_state=ps)
        await store.add_event(row["id"], row["owner_id"], "acp", "complete_failed", {"error": exc.message})
        return await store.update_purchase(row["id"], status="failed", provider_state=ps, warnings=[
            *(row.get("warnings") or []), {"code": "acp_failed", "severity": "info",
                                           "message": f"The shop refused the payment: {exc.message}"}])
    acp["status"] = session.get("status")
    ps["acp"] = acp
    problems = _errors(session)
    order = session.get("order") or {}
    if session.get("status") == "completed" and order:
        ref = str(order.get("order_number") or (order.get("confirmation") or {}).get("confirmation_number")
                  or order.get("id") or "")
        await store.add_event(row["id"], row["owner_id"], "acp", "completed", {"order": order.get("id")})
        return await store.update_purchase(row["id"], status="completed", provider_state=ps, order_ref=ref[:120],
                                           completed_at=time.time(),
                                           result={"outcome": "confirmed", "url": order.get("permalink_url", "")})
    if problems:
        return await store.update_purchase(row["id"], status="failed", provider_state=ps, warnings=[
            *(row.get("warnings") or []),
            *({"code": f"acp_{m.get('code')}", "severity": "info", "message": str(m.get("content"))} for m in problems)])
    return await store.update_purchase(row["id"], provider_state=ps)


async def cancel(row: dict) -> None:
    if row["source"] == "ap2":
        return
    acp = (row.get("provider_state") or {}).get("acp") or {}
    creds = (await merchants(row["owner_id"])).get(acp.get("domain", ""))
    if acp.get("session_id") and creds:
        await _safe_cancel(acp["api_base_url"], acp["session_id"], creds["api_key"], row["id"])


async def approve(row: dict, owner: str) -> dict:
    if row["source"] == "ap2":
        from captain_claw.flight_deck import ap2_client

        return await ap2_client.approve(row, owner)
    raise HTTPException(409, "Approve ACP purchases in Link (the approval link).")
