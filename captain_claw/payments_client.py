"""Agent side of payments: whether the `payments` tool is offered, and its calls
to Flight Deck (docs/payments-phase0-plan.md).

Payments live in Flight Deck only (the ledger, the checks, the barcodes); an
agent without a Flight Deck never sees the tool. Owner-only: a shared agent's
member never reaches it.
"""

from __future__ import annotations

import time

import httpx

from captain_claw.logging import get_logger

log = get_logger(__name__)

NOT_AVAILABLE = ("Payments aren't turned on for this Flight Deck (FD_PAYMENTS), or it is missing "
                 "the payments libraries.")
MEMBER_REFUSED = ("Payments aren't available during a shared-agent member's turn — only the "
                  "agent's owner can prepare payments.")
NO_FLIGHT_DECK = "Payments need Flight Deck: this agent wasn't started by one."

_USABLE: tuple[bool, float] = (False, 0.0)
_CACHE_MAX_AGE = 120.0
_REFRESH_MIN_GAP = 20.0
_LAST_CHECK_AT = 0.0


def _fd_base() -> str:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._flight_deck_base()


def _fd_headers() -> dict[str, str]:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    # Owner-only: never the member grant — /fd/payments/* is not grant-aware.
    return GoogleOAuthManager._flight_deck_headers(as_speaker=False)


def under_flight_deck() -> bool:
    return bool(_fd_base())


def member_call() -> bool:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._member_call()


def _mark(usable: bool) -> None:
    global _USABLE
    _USABLE = (bool(usable), time.time())


def payments_usable() -> bool:
    """Offer the tool? (sync, for the tool registry; fails closed)"""
    usable, at = _USABLE
    return usable and at > 0 and time.time() - at <= _CACHE_MAX_AGE


async def refresh(force: bool = False) -> bool:
    """Ask Flight Deck whether payments are on (while on, at most every ~20 s)."""
    global _LAST_CHECK_AT
    if not under_flight_deck() or member_call():
        return False
    usable, _at = _USABLE
    if not force and usable and time.time() - _LAST_CHECK_AT < _REFRESH_MIN_GAP:
        _mark(True)
        return True
    _LAST_CHECK_AT = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_fd_base()}/fd/payments/agent/status", headers=_fd_headers())
    except Exception as exc:
        log.debug("Flight Deck payments status check failed: %s", exc)
        return payments_usable()
    if resp.status_code != 200:
        _mark(False)
        return False
    data = resp.json()
    _mark(bool(data.get("enabled")) and bool(data.get("deps_ok")))
    return payments_usable()


async def call(method: str, path: str, body: dict | None = None, *, params: dict | None = None,
               timeout: float = 90.0) -> tuple[dict, str]:
    """(response JSON, "") or ({}, error text Flight Deck gave)."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, f"{_fd_base()}{path}", json=body, params=params,
                                    headers=_fd_headers())
    if resp.status_code == 200:
        return resp.json(), ""
    try:
        detail = str(resp.json().get("detail") or "")
    except Exception:  # noqa: BLE001
        detail = resp.text[:300]
    if resp.status_code == 404 and not detail:
        detail = NOT_AVAILABLE
    return {}, detail or f"Flight Deck answered {resp.status_code}"
