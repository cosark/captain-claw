"""Sessions tool — the user's conversation topics, and moving between them.

A chat is a series of topics (agent sessions). Flight Deck's web chat has a
Topics menu for them; on a voice device or WhatsApp there is no menu, so the
user asks the agent instead: "go back to the Rome trip", "what topics do we
have", "start a new topic". The tool lists them, and records a switch or a
new topic to be carried out once the current turn is over (the web server's
``apply_pending_session_change``) — never in the middle of a turn, which
would split the turn's messages across two sessions.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from captain_claw.logging import get_logger
from captain_claw.tools.registry import Tool, ToolResult

log = get_logger(__name__)

_STOPWORDS = frozenset(
    "the a an to of about on in at for topic topics conversation session chat one back go "
    "switch that this our my your with and or we were talking earlier again please".split()
)
_LIST_LIMIT = 15


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[^\W_]+", str(text or "").lower()) if w not in _STOPWORDS}


def _first_user_turn(session: Any) -> str:
    for msg in (getattr(session, "messages", None) or []):
        if msg.get("role") != "user":
            continue
        text = " ".join(str(msg.get("content") or "").split())
        if text and not text.startswith("/"):
            return text
    return ""


def session_title(session: Any) -> str:
    """What the user calls the topic: the agent's generated title, else the
    first user turn (trimmed), else the session name."""
    meta = getattr(session, "metadata", None) or {}
    title = str(meta.get("title") or "").strip()
    if title:
        return title
    first = _first_user_turn(session)
    if first:
        return first if len(first) <= 48 else first[:47].rstrip() + "…"
    return str(getattr(session, "name", "") or "untitled")


def _when(iso: str) -> str:
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    now = datetime.now(d.tzinfo) if d.tzinfo else datetime.now()
    return d.strftime("%H:%M") if d.date() == now.date() else d.strftime("%Y-%m-%d")


def _turns(session: Any) -> int:
    return sum(1 for m in (getattr(session, "messages", None) or []) if m.get("role") in ("user", "assistant"))


def _unavailable(agent: Any) -> str | None:
    """Why this agent can't move between topics: a shared agent's member has
    private, capped sessions; a Telegram user has a dedicated session and the
    session list is the owner's."""
    try:
        from captain_claw.speaker import is_speaker_agent

        if is_speaker_agent(agent):
            return "Switching topics isn't available on a shared agent."
    except Exception:
        pass
    if str(getattr(agent, "_channel", "") or "") == "telegram":
        return "Switching topics isn't available on Telegram — /new starts a fresh one."
    return None


def resolve_topic(sessions: list[Any], selector: str, current_id: str = "") -> Any | None:
    """The session the user means: an exact id, ``#N`` / ``N`` from the list,
    else the best title match by shared words (most recent wins ties)."""
    key = str(selector or "").strip()
    if not key:
        return None
    for s in sessions:
        if s.id == key:
            return s
    m = re.fullmatch(r"#?\s*(\d{1,3})", key)
    if m:
        n = int(m.group(1))
        return sessions[n - 1] if 1 <= n <= len(sessions) else None
    want = _tokens(key)
    if not want:
        return None
    best, best_score = None, 0
    for s in sessions:  # newest first: a tie keeps the earlier (newer) hit
        have = _tokens(session_title(s)) | _tokens(getattr(s, "name", "")) | _tokens(_first_user_turn(s))
        score = len(want & have)
        if score > best_score:
            best, best_score = s, score
    # One shared word out of several ("the Vienna trip" vs "Rome day trip")
    # is not a match; every word, or at least two, is.
    if best_score >= 2 or best_score == len(want):
        return best
    return None


class SessionsTool(Tool):
    """List the user's conversation topics, switch to one, or start a new one."""

    name = "sessions"
    description = (
        "The user's conversation TOPICS with you (sessions), and the way to move "
        "between them when the user ASKS for it — 'go back to the Rome trip', "
        "'what were we talking about before?', 'start a new topic'. Actions: "
        "'list' (recent topics: number, title, turns, when, which is current), "
        "'switch' (to one by number, title words or id), 'new' (a fresh topic, "
        "optional name). A switch or new topic takes effect AFTER this reply: tell "
        "the user briefly and don't start answering within the other topic yet. "
        "Never switch or start a topic on your own initiative."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "switch", "new"],
                "description": "list = recent topics; switch = move to 'topic'; new = start a fresh topic.",
            },
            "topic": {
                "type": "string",
                "description": "For switch: the topic's number from the list (#2), words from its title, or its id.",
            },
            "name": {"type": "string", "description": "For new: an optional name for the topic."},
        },
        "required": ["action"],
    }

    async def execute(
        self,
        action: str,
        topic: str | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        agent = kwargs.get("_agent")
        if agent is None or getattr(agent, "session_manager", None) is None:
            return ToolResult(success=False, error="No conversation to work with here.")
        why = _unavailable(agent)
        if why:
            return ToolResult(success=False, error=why)
        from captain_claw.speaker import list_owner_sessions

        sm = agent.session_manager
        current = getattr(agent, "session", None)
        current_id = str(getattr(current, "id", "") or "")
        action = str(action or "").strip().lower()

        if action == "list":
            sessions = await list_owner_sessions(sm, limit=_LIST_LIMIT)
            if not sessions:
                return ToolResult(success=True, content="No topics yet.")
            lines = ["Topics, newest first:"]
            for i, s in enumerate(sessions, 1):
                mark = " (current)" if s.id == current_id else ""
                when = _when(getattr(s, "updated_at", ""))
                lines.append(
                    f"#{i} {session_title(s)} — {_turns(s)} turns"
                    + (f", {when}" if when else "") + mark + f" (id {s.id[:8]})"
                )
            lines.append("Switch with the number, words from the title, or the id.")
            return ToolResult(success=True, content="\n".join(lines))

        if action == "switch":
            if not str(topic or "").strip():
                return ToolResult(success=False, error="Say which topic: its number, words from its title, or its id.")
            sessions = await list_owner_sessions(sm, limit=50)
            target = resolve_topic(sessions, str(topic), current_id)
            if target is None:
                # An exact id outside the recent list still resolves.
                loaded = await sm.load_session(str(topic).strip())
                target = loaded if loaded is not None and not _is_member(loaded) else None
            if target is None:
                return ToolResult(success=False, error=f"No topic matches {topic!r}. Use 'list' and pick a number.")
            if target.id == current_id:
                return ToolResult(success=True, content=f"Already on this topic: {session_title(target)}.")
            agent._pending_session_change = {"action": "switch", "id": target.id, "title": session_title(target)}
            return ToolResult(
                success=True,
                content=(
                    f"Will switch to the topic \"{session_title(target)}\" ({_turns(target)} turns) as soon as "
                    "this reply is done. Tell the user you're switching, in one short sentence; the next "
                    "message will be in that topic."
                ),
            )

        if action == "new":
            agent._pending_session_change = {"action": "new", "name": str(name or "").strip()}
            return ToolResult(
                success=True,
                content=(
                    "Will start a new topic as soon as this reply is done. Tell the user, in one short "
                    "sentence; their next message opens it."
                ),
            )

        return ToolResult(success=False, error=f"Unknown action {action!r}; use list, switch or new.")


def _is_member(session: Any) -> bool:
    try:
        from captain_claw.speaker import is_member_session

        return is_member_session(session)
    except Exception:
        return False
