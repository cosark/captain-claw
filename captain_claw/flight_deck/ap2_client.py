"""AP2 (Agent Payments Protocol, v0.2) — Captain as the shopping agent.

Payments Phase 3c (docs/payments-phase3-plan.md). Follows the v0.2
human-present cards flow of github.com/google-agentic-commerce/AP2 over A2A
JSON-RPC (``message/send``, header ``X-A2A-Extensions``):

1. merchant: "Find products …" ``{catalog_search}`` → carts (``ap2.cart``)
2. merchant: "Create a checkout for the selected cart." ``{cart_id}`` →
   ``ap2.checkout {checkout_jwt, checkout_hash, amount_cents, currency}``
   — the purchase now waits for the owner's approval in Flight Deck
3. on approval: credentials provider (the user's wallet) — shipping address,
   payment methods, a token for the chosen method; Flight Deck signs the
   **CheckoutMandate** and **PaymentMandate** (SD-JWT, ES256) with the user's
   P-256 key it keeps; the PaymentMandate goes to the credentials provider
4. merchant: "Initiate a payment" with both mandates → a receipt
   (``ap2.PaymentReceipt``), or a challenge (``input-required``, e.g. an OTP)
   the owner answers in Flight Deck.

Off unless ``FD_AP2=1``: there are no public AP2 merchants yet, only Google's
samples. The mandate format mirrors the AP2 SDK's ``MandateClient.create``
(root SD-JWT, claims under ``delegate_payload``, ``checkout_jwt``
selectively disclosable); a test checks it with the real SDK.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from typing import Any

import httpx
from fastapi import HTTPException

from captain_claw.logging import get_logger

log = get_logger(__name__)

EXTENSION = "https://github.com/google-agentic-commerce/ap2/v1"
PROVIDER = "ap2"
PREF = "ap2"
MANDATE_TTL = 3600
transport: httpx.AsyncBaseTransport | None = None        # tests swap in a MockTransport


class Ap2Error(Exception):
    pass


def enabled() -> bool:
    return os.environ.get("FD_AP2", "").strip().lower() in ("1", "true", "yes", "on")


def shopping_agent_id() -> str:
    return os.environ.get("FD_AP2_SHOPPING_AGENT_ID", "").strip() or "captain-claw"


def _pur():
    from captain_claw.flight_deck import purchases_routes

    return purchases_routes


# ── SD-JWT mandates (RFC 9901), as the AP2 SDK issues them ────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _disclosure(*parts: Any) -> tuple[str, str]:
    """(disclosure, its sha-256 digest)."""
    encoded = _b64(json.dumps([_b64(secrets.token_bytes(16)), *parts], separators=(",", ":")).encode())
    return encoded, _b64(hashlib.sha256(encoded.encode("ascii")).digest())


def sd_jwt(payload: dict, private_key_pem: str, kid: str, disclosable: tuple[str, ...] = ()) -> str:
    """A root SD-JWT with ``payload`` as the one (disclosed) ``delegate_payload``
    element; the fields in *disclosable* are selectively disclosable."""
    import jwt

    disclosures: list[str] = []
    element = {k: v for k, v in payload.items() if k not in disclosable}
    digests = []
    for name in disclosable:
        if name in payload:
            disc, digest = _disclosure(name, payload[name])
            disclosures.append(disc)
            digests.append(digest)
    if digests:
        element["_sd"] = sorted(digests)
    element_disc, element_digest = _disclosure(element)
    disclosures.insert(0, element_disc)
    claims = {"delegate_payload": [{"...": element_digest}], "_sd_alg": "sha-256"}
    token = jwt.encode(claims, private_key_pem, algorithm="ES256", headers={"kid": kid})
    return token + "~" + "~".join(disclosures) + "~"


def read_sd_jwt(token: str, public_key_pem: str) -> dict:
    """Verify and resolve a root SD-JWT made by :func:`sd_jwt` (our own check)."""
    import jwt

    parts = token.split("~")
    claims = jwt.decode(parts[0], public_key_pem, algorithms=["ES256"])
    by_digest = {_b64(hashlib.sha256(d.encode("ascii")).digest()): json.loads(base64.urlsafe_b64decode(d + "=" * (-len(d) % 4)))
                 for d in parts[1:] if d}
    element = by_digest[claims["delegate_payload"][0]["..."]][1]
    for digest in element.pop("_sd", []):
        _salt, name, value = by_digest[digest]
        element[name] = value
    return element


def generate_user_key() -> tuple[str, str]:
    """(private key PEM, public key PEM) — P-256 for ES256 mandates."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


# ── A2A JSON-RPC ──────────────────────────────────────────────────────

async def send(url: str, text: str, data: dict[str, Any], *, context_id: str = "", task_id: str = "") -> dict:
    """``message/send`` one AP2 message; returns the task."""
    message: dict[str, Any] = {"kind": "message", "messageId": uuid.uuid4().hex, "role": "agent",
                               "parts": [{"kind": "text", "text": text},
                                         *({"kind": "data", "data": {k: v}} for k, v in data.items() if v not in (None, ""))]}
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send", "params": {"message": message}}
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as client:
            resp = await client.post(url, json=body, headers={"X-A2A-Extensions": EXTENSION})
        out = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        raise Ap2Error(f"Couldn't reach {url} ({type(exc).__name__})")
    if not isinstance(out, dict):
        raise Ap2Error(f"{url} answered HTTP {resp.status_code}")
    if out.get("error"):
        raise Ap2Error(str((out["error"] or {}).get("message") or out["error"]))
    task = out.get("result") or {}
    if (task.get("status") or {}).get("state") == "failed":
        raise Ap2Error(_text(task) or "the agent reported a failure")
    return task


def _datas(task: dict) -> list[dict]:
    """Every DataPart's data in a task (artifacts first, then the status message)."""
    out: list[dict] = []
    for artifact in task.get("artifacts") or []:
        out += [p.get("data") or {} for p in artifact.get("parts") or [] if p.get("kind") == "data"]
    out += [p.get("data") or {} for p in ((task.get("status") or {}).get("message") or {}).get("parts") or []
            if p.get("kind") == "data"]
    return out


def _first(task: dict, key: str) -> Any:
    return next((d[key] for d in _datas(task) if key in d), None)


def _all(task: dict, key: str) -> list[Any]:
    return [d[key] for d in _datas(task) if key in d]


def _text(task: dict) -> str:
    parts = ((task.get("status") or {}).get("message") or {}).get("parts") or []
    return " ".join(p.get("text", "") for p in parts if p.get("kind") == "text").strip()


# ── purchase hooks ───────────────────────────────────────────────────

async def _settings(owner: str) -> dict:
    return await _pur().store().get_pref(owner, PREF) or {}


async def user_key(owner: str) -> dict:
    """The owner's AP2 signing key (made on first use); ``certificate`` holds the public PEM."""
    store = _pur().store()
    conn = await store.get_connection(owner, PROVIDER)
    if not conn or not conn.get("private_key"):
        private, public = generate_user_key()
        conn = await store.save_connection(owner, PROVIDER, private_key=private, certificate=public,
                                           client_id=f"captain-{secrets.token_hex(4)}")
    return conn


async def start(row: dict, endpoint: str) -> dict:
    """Find the cart and open the checkout; the purchase then waits for the owner."""
    if not enabled():
        raise HTTPException(409, "AP2 checkout isn't switched on for this deck (FD_AP2).")
    if not endpoint.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise HTTPException(422, "protocol_endpoint must be the merchant agent's A2A URL")
    risk = {"source": "captain-claw", "purchase": row["id"]}
    try:
        found = await send(endpoint, "Find products that match the user's request.",
                           {"catalog_search": row["description"] or row["merchant_name"], "risk_data": json.dumps(risk),
                            "shopping_agent_id": shopping_agent_id()})
        context_id = found.get("contextId", "")
        carts = _all(found, "ap2.cart")
        if not carts:
            raise HTTPException(409, "The merchant agent found nothing for that request.")
        want = row["amount_cents"]
        cart = min(carts, key=lambda c: (abs(int(round(float(c.get("amount") or 0) * 100)) - want), float(c.get("amount") or 0)))
        checkout_task = await send(endpoint, "Create a checkout for the selected cart.",
                                   {"cart_id": cart.get("cart_id"), "shopping_agent_id": shopping_agent_id()},
                                   context_id=context_id)
    except Ap2Error as exc:
        raise HTTPException(502, f"The merchant agent refused it: {exc}")
    checkout = _first(checkout_task, "ap2.checkout") or {}
    if not checkout.get("checkout_jwt") or not checkout.get("checkout_hash"):
        raise HTTPException(502, "The merchant agent didn't return a checkout.")
    amount = int(checkout.get("amount_cents") or round(float(checkout.get("amount") or 0) * 100))
    return {**row, "amount_cents": amount, "currency": str(checkout.get("currency") or row["currency"]).upper(),
            "items": row.get("items") or [{"name": checkout.get("item_label") or cart.get("item_label") or "item",
                                           "quantity": 1}],
            "approval_url": f"/payments/purchases/{row['id']}",
            "provider_state": {"ap2": {"endpoint": endpoint, "context_id": context_id, "cart_id": cart.get("cart_id"),
                                       "checkout_jwt": checkout["checkout_jwt"],
                                       "checkout_hash": checkout["checkout_hash"],
                                       "item_label": checkout.get("item_label", "")}}}


async def approve(row: dict, owner: str) -> dict:
    """The owner approved this checkout: get a payment method from the user's
    credentials provider, sign both mandates, and initiate the payment."""
    store = _pur().store()
    settings = await _settings(owner)
    cp = settings.get("credentials_provider_url", "")
    email = settings.get("user_email", "")
    if not cp or not email:
        raise HTTPException(409, "Set your AP2 credentials provider and wallet email on the Payments page.")
    ps = dict(row.get("provider_state") or {})
    ap2 = dict(ps.get("ap2") or {})
    ctx = ap2.get("context_id", "")
    conn = await user_key(owner)
    try:
        await send(cp, "Get the user's shipping address.", {"user_email": email}, context_id=ctx)
        methods = await send(cp, "Get a filtered list of the user's payment methods.",
                             {"user_email": email,
                              "payment_request.PaymentMethodData": {"supported_methods": "CARD", "data": {}}},
                             context_id=ctx)
        aliases = _first(methods, "payment_method_aliases") or []
        if not aliases:
            raise HTTPException(409, "Your AP2 wallet has no card for this merchant.")
        alias = aliases[0]
        await send(cp, "Get a payment credential token for the user's payment method.",
                   {"payment_method_alias": alias, "user_email": email}, context_id=ctx)
        now = int(time.time())
        checkout_mandate = sd_jwt({"vct": "mandate.checkout.1", "checkout_jwt": ap2["checkout_jwt"],
                                   "checkout_hash": ap2["checkout_hash"], "iat": now, "exp": now + MANDATE_TTL},
                                  conn["private_key"], conn["client_id"], disclosable=("checkout_jwt",))
        payment_mandate = sd_jwt({"vct": "mandate.payment.1", "transaction_id": ap2["checkout_hash"],
                                  "payee": {"id": row["merchant_url"], "name": row["merchant_name"],
                                            "website": row["merchant_url"]},
                                  "payment_amount": {"amount": int(row["amount_cents"]), "currency": row["currency"]},
                                  "payment_instrument": {"id": str(alias), "type": "card"},
                                  "iat": now, "exp": now + MANDATE_TTL},
                                 conn["private_key"], conn["client_id"])
        risk = json.dumps({"source": "captain-claw", "purchase": row["id"]})
        await send(cp, "This is the signed payment mandate",
                   {"ap2.mandates.PaymentMandateSdJwt": payment_mandate, "risk_data": risk}, context_id=ctx)
        task = await send(ap2["endpoint"], "Initiate a payment",
                          {"ap2.mandates.PaymentMandateSdJwt": payment_mandate,
                           "ap2.mandates.CheckoutMandateSdJwt": checkout_mandate, "risk_data": risk,
                           "shopping_agent_id": shopping_agent_id()}, context_id=ctx)
    except Ap2Error as exc:
        raise HTTPException(502, f"AP2 payment failed: {exc}")
    ap2.update(payment_method_alias=str(alias), mandates_signed_at=time.time())
    ps["ap2"] = ap2
    return await _after_payment(row, ps, task)


async def _after_payment(row: dict, ps: dict, task: dict) -> dict:
    store = _pur().store()
    state = (task.get("status") or {}).get("state")
    receipt = _first(task, "ap2.PaymentReceipt")
    ap2 = dict(ps.get("ap2") or {})
    if receipt:
        ok = str(receipt.get("payment_status") or receipt.get("status") or "").lower() in ("success", "succeeded")
        ap2["receipt"] = {k: receipt.get(k) for k in ("payment_id", "psp_confirmation_id", "status", "payment_status")}
        ps["ap2"] = ap2
        return await store.update_purchase(row["id"], status="completed" if ok else "failed", provider_state=ps,
                                           order_ref=str(receipt.get("payment_id") or "")[:120],
                                           completed_at=time.time() if ok else None)
    if state == "input-required":
        challenge = _first(task, "challenge") or {}
        ap2["task_id"] = task.get("id", "")
        ps["ap2"] = ap2
        ps["next_action"] = {"type": challenge.get("type", "challenge"),
                             "display_message": challenge.get("display_text") or _text(task)}
        return await store.update_purchase(row["id"], status="needs_action", provider_state=ps)
    ps["ap2"] = ap2
    return await store.update_purchase(row["id"], status="paying", provider_state=ps)


async def answer_challenge(row: dict, response: str) -> dict:
    """The owner's answer to the payment's challenge (e.g. the OTP)."""
    ps = dict(row.get("provider_state") or {})
    ap2 = ps.get("ap2") or {}
    try:
        task = await send(ap2["endpoint"], "Here is the challenge response.", {"challenge_response": response},
                          context_id=ap2.get("context_id", ""), task_id=ap2.get("task_id", ""))
    except Ap2Error as exc:
        raise HTTPException(502, f"AP2: {exc}")
    ps.pop("next_action", None)
    return await _after_payment(row, ps, task)


async def sync(row: dict) -> dict:
    return row            # AP2 answers synchronously: approve / answer_challenge move the purchase
