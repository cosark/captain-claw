"""Report this agent's LLM tokens to Flight Deck, for the plan's daily allowance.

Flight Deck keeps each user's daily token allowance
(``flight_deck/usage_quota.py``), but only the agent sees every token it spends:
a Telegram turn, a cron job or the post-turn maintenance calls never pass
through Flight Deck. So every recorded LLM call is reported here.

Fire-and-forget: a report that fails is dropped and never becomes a turn's
error. During a shared-agent member's turn the report carries the turn's grant,
so Flight Deck bills that member; once the grant has closed (work after the
turn), the owner is billed. Calls on the person's own key (BYOK) cost the deck
nothing and are not reported.
"""

from __future__ import annotations

import asyncio
import os
import weakref
from typing import Any

from captain_claw.logging import get_logger

log = get_logger(__name__)

USAGE_PATH = "/fd/quota/agent/usage"

_CLIENT: tuple[Any, Any] | None = None  # (weakref to its loop, FDClient)
_PENDING: set[asyncio.Task] = set()


def _enabled() -> bool:
    """Only an agent Flight Deck spawned for a user reports."""
    from captain_claw.fd_client import flight_deck_base, flight_deck_slug

    return bool(flight_deck_base() and flight_deck_slug()
                and (os.environ.get("FD_OWNER_ID") or "").strip())


def _client() -> Any:
    from captain_claw.fd_client import FDClient

    global _CLIENT
    loop = asyncio.get_running_loop()
    held = _CLIENT
    if held is None or held[0]() is not loop:
        held = _CLIENT = (weakref.ref(loop), FDClient(timeout=5.0))
    return held[1]


def _member_grant() -> tuple[dict[str, str], dict[str, str]]:
    """The member turn's grant headers and marker, or empty for the owner."""
    try:
        from captain_claw import speaker

        return speaker.grant_headers(), speaker.grant_params()
    except Exception:  # a member context without its grant: bill the owner
        return {}, {}


async def _post(tokens: int, headers: dict[str, str], params: dict[str, str]) -> None:
    try:
        resp = await _client().post(USAGE_PATH, json={"tokens": tokens},
                                    headers=headers, params=params)
        if resp.status_code == 403 and headers:
            # The member's turn ended before the report arrived.
            resp = await _client().post(USAGE_PATH, json={"tokens": tokens})
        if resp.status_code != 200:
            log.debug("Token usage report refused", status=resp.status_code)
    except Exception as exc:
        log.debug("Token usage report failed", error=type(exc).__name__)


def report_tokens(tokens: int, *, byok: bool = False) -> None:
    """Tell Flight Deck this agent just spent *tokens* (non-blocking)."""
    try:
        tokens = int(tokens or 0)
        if tokens <= 0 or byok or not _enabled():
            return
        headers, params = _member_grant()
        task = asyncio.get_running_loop().create_task(_post(tokens, headers, params))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as exc:
        log.debug("Token usage report skipped", error=type(exc).__name__)
