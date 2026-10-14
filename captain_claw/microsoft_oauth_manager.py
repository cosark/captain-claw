"""Agent-side access to the owner's Microsoft connection.

Flight Deck only: the deck runs the Microsoft sign-in and keeps every user's
refresh token (``flight_deck.microsoft_oauth_routes``). An agent asks
``{FD}/fd/microsoft/access_token`` with the same credentials it uses for
Google (``X-Agent-Secret`` + its ``X-Agent-Auth`` token — see
:meth:`GoogleOAuthManager._flight_deck_headers`), and the deck answers with a
short-lived Graph token for the agent's OWNER. There is no standalone mode: an
agent outside Flight Deck has no Microsoft tools (the registry hides them).

A shared-agent member's turn never gets a token: members can't use the
owner's mailbox, and member Microsoft connections aren't built yet. The
request is refused here, before any network call (Flight Deck would refuse it
too — ``/fd/microsoft/*`` is not grant-aware).
"""

from __future__ import annotations

import time

import httpx

from captain_claw.google_oauth_manager import GoogleOAuthManager
from captain_claw.logging import get_logger
from captain_claw.microsoft_oauth import MicrosoftOAuthTokens

log = get_logger(__name__)

NOT_CONNECTED = (
    "Microsoft account is not connected. Connect your Microsoft account in "
    "Flight Deck → Connections → Microsoft."
)
NO_FLIGHT_DECK = (
    "Microsoft tools need Flight Deck: this agent isn't running under a Flight "
    "Deck, which holds the Microsoft connection (Connections → Microsoft)."
)
MEMBER_REFUSED = (
    "Microsoft isn't available during a shared-agent member's turn — only the "
    "agent's owner can use their Microsoft account through it."
)

# The owner's connection state, readable synchronously by the tool registry
# (which can't await). Refreshed by every token fetch / is_connected(); stale
# entries read as "not connected" so the tools hide until freshly checked.
_CONNECTED: tuple[bool, float] = (False, 0.0)
_CACHE_MAX_AGE = 120.0

# The owner's last access token (tools build a manager per call, so the cache
# lives here). Reused for at most _TOKEN_REUSE_S, so a disconnect in Flight
# Deck stops this agent within a minute (Microsoft can't revoke the token
# itself). Never used for a member's call.
_TOKENS: MicrosoftOAuthTokens | None = None
_TOKENS_AT: float = 0.0
_TOKEN_REUSE_S = 60.0


def _mark(connected: bool) -> None:
    global _CONNECTED
    _CONNECTED = (bool(connected), time.time())


def is_microsoft_connected_cached(max_age: float | None = None) -> bool:
    connected, at = _CONNECTED
    age = time.time() - at
    return connected and at > 0 and age <= (_CACHE_MAX_AGE if max_age is None else max_age)


def _forget() -> None:
    global _TOKENS
    _TOKENS = None


class MicrosoftRefused(RuntimeError):
    """Flight Deck (or this agent, for a member's turn) refused the token —
    a reason the user must see, not "connect Microsoft"."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(
            f"Flight Deck refused this agent's Microsoft request (HTTP {status}): "
            f"{detail or 'no reason given'}"
            if status else detail
        )


class MicrosoftOAuthManager:
    """Fetches the owner's Microsoft Graph token from Flight Deck."""

    @staticmethod
    def _base() -> str:
        return GoogleOAuthManager._flight_deck_base()

    def is_flight_deck_client(self) -> bool:
        return bool(self._base())

    @staticmethod
    def _member_call() -> bool:
        return GoogleOAuthManager._member_call()

    async def get_tokens(self) -> MicrosoftOAuthTokens | None:
        """The owner's tokens — None when Microsoft isn't connected (or Flight
        Deck can't be reached). Raises :class:`MicrosoftRefused` for a member's
        turn or a 401/403 from Flight Deck, keeping its reason."""
        global _TOKENS, _TOKENS_AT
        if self._member_call():
            raise MicrosoftRefused(0, MEMBER_REFUSED)
        base = self._base()
        if not base:
            _mark(False)
            return None
        if (_TOKENS is not None and not _TOKENS.is_expired()
                and time.time() - _TOKENS_AT < _TOKEN_REUSE_S):
            _mark(True)
            return _TOKENS
        try:
            headers = GoogleOAuthManager._flight_deck_headers(as_speaker=False)
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base}/fd/microsoft/access_token", headers=headers)
        except Exception as exc:
            log.warning("Flight Deck Microsoft access_token fetch failed: %s", exc)
            return None  # unreachable: leave the connection flag as it was
        if resp.status_code == 404:
            _forget()
            _mark(False)
            return None
        if resp.status_code in (401, 403):
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail") or "")[:500]
            except Exception:
                pass
            _forget()
            _mark(False)
            log.warning("Flight Deck refused Microsoft access_token", status=resp.status_code)
            raise MicrosoftRefused(resp.status_code, detail)
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Flight Deck Microsoft access_token fetch failed: %s", exc)
            return None
        tokens = MicrosoftOAuthTokens(
            access_token=str(data.get("access_token") or ""),
            refresh_token="",  # Flight Deck keeps it
            token_type=str(data.get("token_type") or "Bearer"),
            expires_at=float(data.get("expires_at") or 0.0) or (time.time() + 3300),
            scope=str(data.get("scope") or ""),
        )
        if not tokens.access_token:
            _forget()
            _mark(False)
            return None
        _TOKENS, _TOKENS_AT = tokens, time.time()
        _mark(True)
        return tokens

    async def access_token(self) -> MicrosoftOAuthTokens:
        """:meth:`get_tokens`, or a RuntimeError the tools return as-is."""
        tokens = await self.get_tokens()
        if tokens is None:
            raise RuntimeError(NOT_CONNECTED if self.is_flight_deck_client() else NO_FLIGHT_DECK)
        return tokens

    async def mail_signature(self) -> str | None:
        """The owner's agentic signature (Connections → Microsoft), which
        microsoft_mail puts at the end of every draft — from Flight Deck's
        ``/fd/microsoft/agent_status`` (``mail_signature``; absent = none).
        "" outside Flight Deck; None when Flight Deck couldn't say."""
        base = self._base()
        if not base:
            return ""
        try:
            headers = GoogleOAuthManager._flight_deck_headers(as_speaker=False)
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base}/fd/microsoft/agent_status", headers=headers)
            data = resp.json() if resp.status_code == 200 else None
        except Exception as exc:
            log.info("Flight Deck Microsoft agent_status failed: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("mail_signature")
        return value if isinstance(value, str) else ""

    async def is_connected(self) -> bool:
        """Refresh the cached flag (once per agent turn). A member's turn
        reports not connected and leaves the owner's flag alone."""
        if self._member_call():
            return False
        try:
            tokens = await self.get_tokens()
        except MicrosoftRefused:
            return False
        if tokens is None and self.is_flight_deck_client():
            # Unreachable keeps the previous flag; a 404 already marked False.
            return is_microsoft_connected_cached()
        return bool(tokens)
