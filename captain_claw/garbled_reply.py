"""Garbled-reply check — when a chat reply came out as garbage, tell the
person to start a fresh session (``/new``).

A session that has grown too long, or a weak model that lost the thread,
can answer with output nobody can use: the same line looping, token soup,
raw tool-call JSON or control tokens, text that stops mid-word. On a phone
(WhatsApp, Telegram, the glasses HUD) the person can't see why, and every
further message in that session tends to come back the same way.

After a person's turn, Jev (TypeSafe, see :mod:`captain_claw.jev_client`)
is asked one yes/no question about the reply. When it is confident the
reply is garbage, the surface gets one extra line, :data:`HINT`, which
recommends ``/new``. The hint is a notice: it is sent next to the reply and
never written into the session, so the model never reads it back.

Only when Jev is usable (the owner's key in Flight Deck, or a standalone
key), only for a person's turn, and never in the way: no key, a timeout or
any Jev error means no hint — the reply itself has already been delivered.
Off with ``tools.jev.check_replies: false``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from captain_claw import jev_client
from captain_claw.logging import get_logger

log = get_logger(__name__)

QUESTION_NAME = "garbled"

# p(yes) at or above this counts as garbage. High on purpose: a false alarm
# tells the person to throw away a working conversation.
THRESHOLD = 0.8

# How long the turn waits for Jev before giving up (no hint).
TIMEOUT_SECONDS = 12.0

# Jev reads the end of a long reply (where a loop or a cut-off shows) and the
# start of the person's message (what it was an answer to).
_REPLY_CHARS = 6000
_MESSAGE_CHARS = 1500

HINT = (
    "⚠️ That reply came out garbled — this session may be overloaded. "
    "Send /new to start a fresh session (the old one is kept in history)."
)

QUESTIONS: dict[str, dict[str, Any]] = {
    QUESTION_NAME: {
        "type": "noul",
        "instructions": (
            "Is the assistant's reply garbage — output the person cannot use as an "
            "answer at all?"
        ),
        "yes_means": (
            "Most of the reply is broken output: the same words, lines or paragraphs "
            "repeating in a loop; random characters or token soup; raw markup, JSON, "
            "tool-call syntax or model control tokens (like <|im_end|> or <think>) "
            "where an answer should be; text that breaks off mid-sentence; or "
            "sentences that make no sense or jump between languages for no reason."
        ),
        "no_means": (
            "The reply is readable — even if it is short, terse, wrong, unhelpful, "
            "off-topic, a refusal or an honest error message, or contains code or "
            "data the person asked for."
        ),
    },
}


def enabled() -> bool:
    """``tools.jev.check_replies`` (default on)."""
    try:
        from captain_claw.config import get_config

        return bool(getattr(get_config().tools.jev, "check_replies", True))
    except Exception:
        return True


def build_state(reply: str, message: str = "") -> dict[str, str]:
    """What Jev reads: the person's message and the reply (both trimmed)."""
    from captain_claw import msg_origin

    # A bridge's first message carries the surface rules block — not the
    # person's words.
    _block, said = msg_origin.split_surface_block(str(message or ""))
    reply = str(reply or "").strip()
    if len(reply) > _REPLY_CHARS:
        reply = "[…start of the reply cut…]\n" + reply[-_REPLY_CHARS:]
    state = {"reply": reply}
    said = said.strip()[:_MESSAGE_CHARS]
    if said:
        state = {"person_message": said, **state}
    return state


async def _ask(state: dict[str, str]) -> dict[str, Any]:
    if jev_client.under_flight_deck():
        return await jev_client.ask_via_flight_deck(state, QUESTIONS, timeout=TIMEOUT_SECONDS)
    return await jev_client.standalone_api().ask(state, QUESTIONS)


async def is_garbled(reply: str, message: str = "") -> bool:
    """Whether Jev is confident *reply* (the answer to *message*) is garbage.

    False whenever the check can't run — off, no key, an empty reply, a
    shared-agent member's turn (Jev is owner-only), a timeout or an error.
    Never raises.
    """
    if not str(reply or "").strip() or not enabled():
        return False
    try:
        if jev_client._member_call():
            return False
        if not await jev_client.refresh_connected():
            return False
        resp = await asyncio.wait_for(_ask(build_state(reply, message)), TIMEOUT_SECONDS + 3)
    except Exception as exc:  # TimeoutError, JevError, JevInputError, anything
        log.debug("Garbled-reply check skipped", error=str(exc) or type(exc).__name__)
        return False
    p = jev_client.answer_values(resp).get(QUESTION_NAME)
    if not isinstance(p, (int, float)):
        return False
    garbled = p >= THRESHOLD
    if garbled:
        log.info("Reply looks garbled; recommending /new", p_garbled=round(float(p), 3),
                 reply_len=len(str(reply)))
    return garbled


def hint_frame() -> dict[str, Any]:
    """The web-socket frame carrying :data:`HINT`: an assistant line marked
    ``notice`` (bridges send it as plain text, never as audio)."""
    from datetime import UTC, datetime

    return {
        "type": "chat_message",
        "role": "assistant",
        "content": HINT,
        "timestamp": datetime.now(UTC).isoformat(),
        "notice": True,
    }
