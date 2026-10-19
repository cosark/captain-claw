"""Stripe Link Agent Wallet — one-time cards for Captain's purchases
(Payments Phase 3, docs/payments-phase3-plan.md).

The same rail Grok Bot uses: the agent asks to spend a specific amount at a
specific seller (a *spend request*), the user approves it in Link (app or
web), and Link returns a one-time credential for that purchase — a virtual card,
a Link Pay Token for Stripe checkouts, or a shared payment token. The user's
real card is never exposed.

Deck setup (admin): a Link OAuth client Stripe issued to this deck
(``FD_LINK_CLIENT_ID``, ``FD_LINK_CLIENT_SECRET``, ``FD_LINK_PUBLISHABLE_KEY``),
with ``<deck>/fd/payments/link/callback`` among its redirect URIs. Customers must
be in the US or Canada (Stripe's rule, 2026). ``FD_LINK_TEST=1`` makes every
spend request a test one (test cards, nothing charged).

API per docs.stripe.com/agentic-commerce/agents/link-agent-wallet and the MIT
link-cli source: REST at ``https://api.link.com`` with the user's bearer token;
OAuth at ``https://login.link.com`` (authorization code + PKCE; the token call
carries the publishable key as bearer and the client secret in the form;
refresh tokens rotate).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from typing import Any, Awaitable, Callable

import httpx

from captain_claw.logging import get_logger

log = get_logger(__name__)

API = "https://api.link.com"
AUTH = "https://login.link.com"
SCOPE = "payment_methods.agentic userinfo:read"
MIN_CONTEXT = 100

# Link spend-request status → purchase ledger status.
STATUS_MAP = {
    "created": "awaiting_approval", "pending_approval": "awaiting_approval",
    "requires_action": "needs_action", "approved": "approved", "submitted": "ordered",
    "succeeded": "completed", "failed": "failed", "denied": "denied", "expired": "expired",
    "canceled": "cancelled",
}

transport: httpx.AsyncBaseTransport | None = None      # tests swap in a MockTransport
_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


class LinkError(Exception):
    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def configured() -> bool:
    return bool(_env("FD_LINK_CLIENT_ID") and _env("FD_LINK_CLIENT_SECRET") and _env("FD_LINK_PUBLISHABLE_KEY"))


def test_mode() -> bool:
    return _env("FD_LINK_TEST").lower() in ("1", "true", "yes", "on")


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(48)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(*, redirect_uri: str, state: str, code_challenge: str) -> str:
    query = {"key": _env("FD_LINK_PUBLISHABLE_KEY"), "client_id": _env("FD_LINK_CLIENT_ID"),
             "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE, "state": state,
             "code_challenge": code_challenge, "code_challenge_method": "S256"}
    return f"{AUTH}/auth?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}"


def _message(resp: httpx.Response) -> tuple[str, str]:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return resp.text[:300] or f"HTTP {resp.status_code}", ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or body)[:300], str(error.get("code") or "")
    if isinstance(error, str):
        return str(body.get("error_description") or error)[:300], error
    return str((body or {}).get("message") or body)[:300], ""


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        raise LinkError(502, "Link's answer isn't JSON")


async def _token_call(form: dict) -> dict:
    form = {**form, "client_id": _env("FD_LINK_CLIENT_ID"), "client_secret": _env("FD_LINK_CLIENT_SECRET")}
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            resp = await client.post(f"{AUTH}/auth/token", data=form,
                                     headers={"Authorization": f"Bearer {_env('FD_LINK_PUBLISHABLE_KEY')}"})
    except httpx.HTTPError as exc:
        raise LinkError(502, f"Couldn't reach Link ({type(exc).__name__})")
    if resp.status_code != 200:
        message, code = _message(resp)
        raise LinkError(resp.status_code, message, code)
    data = _json(resp)
    return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token", ""),
            "access_expires_at": time.time() + float(data.get("expires_in") or 3600),
            "scope": data.get("scope", "")}


async def exchange_code(*, code: str, redirect_uri: str, code_verifier: str) -> dict:
    return await _token_call({"grant_type": "authorization_code", "code": code,
                              "redirect_uri": redirect_uri, "code_verifier": code_verifier})


async def revoke(refresh_token: str) -> None:
    form = {"client_id": _env("FD_LINK_CLIENT_ID"), "client_secret": _env("FD_LINK_CLIENT_SECRET"),
            "token": refresh_token, "token_type_hint": "refresh_token"}
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            await client.post(f"{AUTH}/auth/revoke", data=form,
                              headers={"Authorization": f"Bearer {_env('FD_LINK_PUBLISHABLE_KEY')}"})
    except Exception as exc:  # disconnecting locally must not depend on Link answering
        log.info("Link revoke failed", error=str(exc))


class LinkClient:
    """Calls for one owner's Link connection; *save* persists rotated tokens."""

    def __init__(self, conn: dict, save: Callable[[dict], Awaitable[None]]) -> None:
        self.conn = conn
        self._save = save

    async def _token(self, force: bool = False) -> str:
        lock = _REFRESH_LOCKS.setdefault(self.conn.get("owner_id", ""), asyncio.Lock())
        async with lock:
            fresh = float(self.conn.get("access_expires_at") or 0) > time.time() + 60
            if self.conn.get("access_token") and fresh and not force:
                return self.conn["access_token"]
            if not self.conn.get("refresh_token"):
                raise LinkError(401, "Link isn't connected")
            tokens = await _token_call({"grant_type": "refresh_token", "refresh_token": self.conn["refresh_token"]})
            tokens = {k: v for k, v in tokens.items() if k != "scope" and (v or k != "refresh_token")}
            self.conn.update(tokens)
            await self._save(tokens)      # refresh tokens rotate: keep the new one at once
            return self.conn["access_token"]

    async def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> Any:
        for attempt in (0, 1):
            token = await self._token(force=attempt == 1)
            try:
                async with httpx.AsyncClient(timeout=30, transport=transport) as client:
                    resp = await client.request(method, f"{API}{path}", json=json, params=params,
                                                headers={"Authorization": f"Bearer {token}",
                                                         "User-Agent": "captain-claw-payments"})
            except httpx.HTTPError as exc:
                raise LinkError(502, f"Couldn't reach Link ({type(exc).__name__})")
            if resp.status_code == 401 and attempt == 0:
                continue
            if resp.status_code >= 400:
                message, code = _message(resp)
                raise LinkError(resp.status_code, message, code)
            return _json(resp) if resp.content else None
        raise LinkError(401, "Link refused the token")

    async def user_info(self) -> dict:
        return await self.request("GET", "/userinfo") or {}

    async def create_spend_request(self, *, amount_cents: int, currency: str, merchant_name: str, merchant_url: str,
                                   context: str, line_items: list[dict], idempotency_key: str,
                                   credential_type: str = "card", merchant_account_id: str = "",
                                   network_id: str = "") -> dict:
        body: dict[str, Any] = {
            "amount": int(amount_cents), "currency": currency.lower(), "context": context,
            "credential_type": credential_type, "request_approval": True, "idempotency_key": idempotency_key,
            "line_items": line_items,
            "totals": [{"type": "total", "display_text": "Total", "amount": int(amount_cents)}],
        }
        if credential_type == "link_pay_token":
            body["merchant_account_id"] = merchant_account_id
        elif credential_type == "shared_payment_token":
            body["network_id"] = network_id        # Link resolves the seller itself
        else:
            body.update(merchant_name=merchant_name, merchant_url=merchant_url)
        if test_mode() and credential_type != "link_pay_token":
            body["test"] = True
        return await self.request("POST", "/spend_requests", json=body)

    async def get_spend_request(self, spend_id: str, *, include: str = "") -> dict:
        params = {"include": include} if include else None
        return await self.request("GET", f"/spend_requests/{urllib.parse.quote(spend_id)}", params=params) or {}

    async def cancel_spend_request(self, spend_id: str) -> None:
        try:
            await self.request("POST", f"/spend_requests/{urllib.parse.quote(spend_id)}/cancel")
        except LinkError as exc:
            if exc.status not in (400, 404, 409):
                raise

    async def report(self, *, domain: str, outcome: str, spend_id: str, step: str = "") -> None:
        body = {"domain": domain, "outcome": outcome, "spend_request_id": spend_id}
        if step:
            body["step"] = step[:500]
        try:
            await self.request("POST", "/agent_observations", json=body)
        except LinkError as exc:   # reporting is optional
            log.info("Link outcome report failed", error=exc.message)


def context_text(merchant_name: str, merchant_url: str, description: str, items: list[dict]) -> str:
    """What the user reads when approving (Link wants at least 100 characters)."""
    names = ", ".join(str(i.get("name") or "") for i in items if i.get("name"))[:300]
    text = (f"Captain is buying {names or description or 'an order'} from {merchant_name} ({merchant_url}) "
            f"because you asked it to. {description}").strip()
    if len(text) < MIN_CONTEXT:
        text += " Approve only if the item, seller and amount are what you expect."
    return text[:2000]
