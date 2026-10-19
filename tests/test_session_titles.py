"""Topic titles: the short name a session gets when it is left behind.

``AgentSessionMixin.generate_session_title`` makes one small LLM call over
the opening turns (first user message as the fallback); the web ``/new``
titles the outgoing session in the background and broadcasts
``session_title`` so a client's Topics list can show it.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from captain_claw.agent_session_mixin import AgentSessionMixin
from captain_claw.session import Session
from captain_claw.web.slash_commands import handle_command


# ── sanitize / fallback ─────────────────────────────────────────────


@pytest.mark.parametrize("raw,clean", [
    ('"Rome day trip."', "Rome day trip"),
    ("Title: Trains and tickets!", "Trains and tickets"),
    ("<title>Plan za Rim</title>", "Plan za Rim"),
    # A model that thinks out loud: tags win; a think block is ignored; leaked
    # reasoning with no tagged answer is not a title.
    ("Thinking Process\nThe user asks about owls.\n<title>Owl facts</title>", "Owl facts"),
    ("<think>they want a label</think>\nOwl head rotation", "Owl head rotation"),
    ("<think>never closed, the budget ran out", ""),
    ("Thinking Process:\n1. Analyze the request:\n   Input: Opening turns of a conversation", ""),
    ("This conversation is about owls and how far they can turn their heads", ""),
    ("one two three four five six seven eight nine ten", ""),
    ("   ", ""),
])
def test_sanitize_session_title(raw, clean):
    assert AgentSessionMixin.sanitize_session_title(raw) == clean


def test_fallback_title_is_the_first_real_user_turn():
    s = Session(id="s", name="web-session")
    s.add_message("user", "/session switch x")
    s.add_message("user", "  Plan   a one-day trip to Rome, with museums and food and a sunset walk  ")
    s.add_message("assistant", "Sure")
    assert AgentSessionMixin.fallback_session_title(s) == "Plan a one-day trip to Rome, with museums and…"
    assert AgentSessionMixin.fallback_session_title(Session(id="e", name="x")) == ""


# ── generate_session_title ──────────────────────────────────────────


class _Agent(AgentSessionMixin):
    def __init__(self, reply: str | Exception):
        self.session = None
        self.reply = reply
        self.calls: list[dict] = []
        self.instructions = types.SimpleNamespace(
            load=lambda name: f"<{name}>",
            render=lambda name, **kw: f"<{name}:{kw.get('conversation_excerpt', '')}>",
        )

    def _format_compaction_messages(self, messages, max_total_chars=0, max_item_chars=0):
        return "\n".join(f"{m['role']}: {m['content']}" for m in messages)

    async def _complete_with_guards(self, messages, tools=None, interaction_label="", **kw):
        self.calls.append({"messages": messages, "label": interaction_label, **kw})
        if isinstance(self.reply, Exception):
            raise self.reply
        return types.SimpleNamespace(content=self.reply)


def _session() -> Session:
    s = Session(id="old", name="web-session")
    s.add_message("user", "Plan a one-day trip to Rome in one short sentence.")
    s.add_message("assistant", "Colosseum at dawn, then the Forum…")
    return s


async def test_generate_title_uses_one_small_call():
    a = _Agent(' "Rome day trip" ')
    assert await a.generate_session_title(_session()) == "Rome day trip"
    assert len(a.calls) == 1
    assert a.calls[0]["label"] == "session_title"
    assert a.calls[0]["max_tokens"] <= 160   # a title, not an essay
    assert "session_title_user_prompt.md" in a.calls[0]["messages"][1].content


async def test_generate_title_falls_back_to_the_first_user_turn():
    a = _Agent(RuntimeError("provider down"))
    assert await a.generate_session_title(_session()) == "Plan a one-day trip to Rome in one short…"
    assert await _Agent("x").generate_session_title(Session(id="e", name="x")) == ""


# ── /new titles the outgoing session in the background ───────────────


class _SM:
    def __init__(self, *sessions: Session) -> None:
        self.sessions = {s.id: s for s in sessions}
        self.saved: list[str] = []

    async def create_session(self, name=None, metadata=None):
        s = Session(id="fresh", name=name or "web-session")
        self.sessions[s.id] = s
        return s

    async def set_last_active_session(self, sid):
        return True

    async def save_session(self, session):
        self.saved.append(session.id)
        self.sessions[session.id] = session


def _server(session: Session, title: str):
    sm = _SM(session)
    titled: list[Session] = []

    async def generate_session_title(target):
        titled.append(target)
        return title

    agent = types.SimpleNamespace(
        session=session, session_manager=sm,
        archive_current_session_to_history=lambda: 0,
        refresh_session_runtime_flags=lambda: None,
        _empty_usage=lambda: {}, last_usage=None, last_context_window={},
        generate_session_title=generate_session_title,
    )
    s = types.SimpleNamespace(agent=agent, sent=[], broadcast=[], titled=titled, sm=sm)

    async def _send(ws, msg):
        s.sent.append(msg)

    def _broadcast(msg, exclude=None):
        s.broadcast.append(msg)

    def _session_info(a=None):
        a = a or s.agent
        if not a or not a.session:
            return {}
        return {"id": a.session.id, "name": a.session.name,
                "title": (a.session.metadata or {}).get("title", ""),
                "message_count": len(a.session.messages)}

    s._send = _send
    s._broadcast = _broadcast
    s._session_info = _session_info
    return s


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_new_titles_the_session_it_left_and_broadcasts_it():
    s = _server(_session(), "Rome day trip")

    await handle_command(s, types.SimpleNamespace(), "/new")
    await _settle()

    assert [t.id for t in s.titled] == ["old"]
    assert s.sm.sessions["old"].metadata["title"] == "Rome day trip"
    assert s.sm.sessions["old"].metadata["title_source"] == "auto"
    assert "old" in s.sm.saved
    assert {"type": "session_title", "id": "old", "title": "Rome day trip"} in s.broadcast
    # The command result names the NEW session, untitled.
    result = [m for m in s.sent if m.get("type") == "command_result"][-1]
    assert result["session"]["id"] == "fresh" and result["session"]["title"] == ""


async def test_new_on_an_empty_or_titled_session_titles_nothing():
    empty = Session(id="empty", name="web-session")
    s = _server(empty, "unused")
    await handle_command(s, types.SimpleNamespace(), "/new")
    await _settle()
    assert s.titled == []

    done = _session()
    done.metadata["title"] = "Already named"
    s = _server(done, "unused")
    await handle_command(s, types.SimpleNamespace(), "/new")
    await _settle()
    assert s.titled == []
    assert not [m for m in s.broadcast if m.get("type") == "session_title"]
