"""Progress check — when a turn runs out of steps, ask Jev whether the agent
is still getting somewhere before stopping it.

The main loop (``agent_orchestration_mixin``) gives each turn a step budget:
a soft limit it may extend, up to a hard ceiling. Its own rule extends only
when the last few steps made new tool calls AND a plan or a requirement says
work remains, so an ordinary request (no plan) stops at the soft limit even
while the agent is plainly working — probing an API, learning why a call
failed, switching to another approach — and the person gets "iteration
budget exhausted" with a half-finished thought.

At that point Jev (TypeSafe, see :mod:`captain_claw.jev_client`) reads the
request and the turn's recent steps and answers one yes/no question: is the
agent still making real progress? When it is confident, the budget grows by
one more block of steps (never past the hard ceiling) and Jev is asked again
when that runs out; otherwise the turn stops as before and ships what it has.

Only when Jev is usable (the owner's key in Flight Deck, or a standalone
key), never for a shared-agent member's turn (Jev is owner-only), and never
in the way: no key, a timeout or any Jev error means the usual stop. The
stagnation detector (six steps with nothing new) runs on every step either
way, so a "yes" can't keep a looping agent alive. Off with
``tools.jev.check_progress: false``.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from captain_claw import jev_client
from captain_claw.logging import get_logger

log = get_logger(__name__)

QUESTION_NAME = "progressing"

# p(yes) at or above this extends the budget. Lower than the garbled-reply
# check's 0.8 on purpose: a wrong "yes" costs one more bounded block of steps
# (the stagnation detector still watches), a wrong "no" stops an agent that
# was working.
THRESHOLD = 0.6

# How long the turn waits for Jev before giving up (the usual stop).
TIMEOUT_SECONDS = 15.0

# Jev reads the start of the request and the END of the turn's steps (the
# recent ones decide whether it is still moving), each step trimmed.
_REQUEST_CHARS = 1500
_STEPS_CHARS = 9000
_NARRATION_CHARS = 400
_ARGS_CHARS = 200
_RESULT_CHARS = 300
_NOTE_CHARS = 200

QUESTIONS: dict[str, dict[str, Any]] = {
    QUESTION_NAME: {
        "type": "noul",
        "instructions": (
            "An AI agent has used up its step budget while working on the person's "
            "request. Judging by its most recent steps, is it still making real "
            "progress toward finishing the request, so that more steps would likely "
            "pay off?"
        ),
        "yes_means": (
            "The recent steps keep moving the work forward: each one learns something "
            "new, rules out an approach for a stated reason and moves to a different "
            "one, narrows the problem, or produces part of the result; the agent has "
            "a concrete next step that differs from what already failed."
        ),
        "no_means": (
            "The agent is stuck or done: it repeats the same or nearly the same calls "
            "that keep failing or return nothing new, circles between the same few "
            "ideas, waits on something only the person can give (a login, a "
            "permission, missing information), has hit a wall it can't get past, or "
            "has already finished and is only re-checking."
        ),
    },
}


def enabled() -> bool:
    """``tools.jev.check_progress`` (default on)."""
    try:
        from captain_claw.config import get_config

        return bool(getattr(get_config().tools.jev, "check_progress", True))
    except Exception:
        return True


def _flat(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _args_text(args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(args)


def _step_lines(turn_messages: list[dict[str, Any]]) -> list[str]:
    """One line per step: the agent's narration, each tool call with its
    result, and notes the runtime added to the turn. The turn's opening
    message (the request) is skipped — the state carries it separately."""
    from captain_claw.msg_origin import is_monitor_only_tool_name

    lines: list[str] = []
    for i, msg in enumerate(turn_messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).strip().lower()
        if role == "assistant":
            if msg.get("origin") == "rejected":
                continue
            text = _flat(msg.get("content"), _NARRATION_CHARS)
            if text:
                lines.append(f"agent: {text}")
        elif role == "tool":
            name = str(msg.get("tool_name", "") or "tool").strip()
            if is_monitor_only_tool_name(name):
                continue
            result = str(msg.get("content", "") or "")
            failed = result.strip().lower().startswith("error:")
            args = _flat(_args_text(msg.get("tool_arguments")), _ARGS_CHARS)
            outcome = "FAILED" if failed else "ok"
            lines.append(
                f"{name}({args}) → {outcome}: {_flat(result, _RESULT_CHARS) or '(empty)'}"
            )
        elif role == "user" and i > 0:
            text = _flat(msg.get("content"), _NOTE_CHARS)
            if text:
                lines.append(f"[note to the agent: {text}]")
    return lines


def build_state(
    request: str,
    turn_messages: list[dict[str, Any]],
    *,
    steps_used: int,
    step_budget: int,
) -> dict[str, Any]:
    """What Jev reads: the person's request, how much of the budget is gone,
    and the turn's steps — the most recent ones when they don't all fit."""
    from captain_claw import msg_origin

    # A bridge's first message carries the surface rules block — not the
    # person's words.
    _block, said = msg_origin.split_surface_block(str(request or ""))
    lines = _step_lines(turn_messages)
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if kept and size + len(line) + 1 > _STEPS_CHARS:
            break
        kept.append(line)
        size += len(line) + 1
    kept.reverse()
    cut = len(lines) - len(kept)
    steps = "\n".join(kept)
    if cut:
        steps = f"[… {cut} earlier steps cut …]\n{steps}"
    return {
        "request": said.strip()[:_REQUEST_CHARS],
        "budget": f"{steps_used} of {step_budget} steps used",
        "recent_steps": steps or "(no steps recorded)",
    }


async def _ask(state: dict[str, Any]) -> dict[str, Any]:
    if jev_client.under_flight_deck():
        return await jev_client.ask_via_flight_deck(state, QUESTIONS, timeout=TIMEOUT_SECONDS)
    return await jev_client.standalone_api().ask(state, QUESTIONS)


async def ready() -> bool:
    """Whether the check can run for this turn: on, not a shared-agent
    member's turn (Jev is owner-only) and a usable key. Never raises."""
    if not enabled():
        return False
    try:
        if jev_client._member_call():
            return False
        return bool(await jev_client.refresh_connected())
    except Exception as exc:
        log.debug("Jev progress check unavailable", error=str(exc) or type(exc).__name__)
        return False


async def progress_probability(
    request: str,
    turn_messages: list[dict[str, Any]],
    *,
    steps_used: int,
    step_budget: int,
) -> float | None:
    """Jev's p(the agent is still making real progress), or None when the
    check can't run — off, no key, a shared-agent member's turn, a timeout or
    an error. Never raises."""
    if not await ready():
        return None
    try:
        state = build_state(
            request, turn_messages, steps_used=steps_used, step_budget=step_budget,
        )
        resp = await asyncio.wait_for(_ask(state), TIMEOUT_SECONDS + 3)
    except Exception as exc:  # TimeoutError, JevError, JevInputError, anything
        log.debug("Jev progress check skipped", error=str(exc) or type(exc).__name__)
        return None
    p = jev_client.answer_values(resp).get(QUESTION_NAME)
    if not isinstance(p, (int, float)):
        return None
    log.info("Jev progress check", p_progressing=round(float(p), 3),
             steps_used=steps_used, step_budget=step_budget)
    return float(p)
