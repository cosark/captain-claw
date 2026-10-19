"""The ``sessions`` tool: the user's topics, and moving between them by asking.

The tool lists sessions and records a switch / new topic on the agent; the
web server carries it out once the turn is over
(``apply_pending_session_change``), announcing it like ``/session switch``
and ``/new`` do so timelines and bridges follow.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from captain_claw.session import Session
from captain_claw.tools.sessions import SessionsTool, resolve_topic, session_title
from captain_claw.web.slash_commands import apply_pending_session_change


def _session(sid: str, name: str, *turns: str, title: str = "", updated: str = "") -> Session:
    s = Session(id=sid, name=name)
    for i, t in enumerate(turns):
        s.add_message("user" if i % 2 == 0 else "assistant", t)
    if title:
        s.metadata["title"] = title
    if updated:
        s.updated_at = updated
    return s


class _SM:
    def __init__(self, *sessions: Session) -> None:
        self.sessions = {s.id: s for s in sessions}
        self.last_active: str | None = None

    async def list_sessions(self, limit=10):
        rows = sorted(self.sessions.values(), key=lambda s: s.updated_at, reverse=True)
        return rows[:limit]

    async def load_session(self, sid):
        return self.sessions.get(sid)

    async def load_session_by_name(self, name):
        return next((s for s in self.sessions.values() if s.name == name), None)

    async def create_session(self, name=None, metadata=None):
        s = Session(id="fresh", name=name or "web-session")
        self.sessions[s.id] = s
        return s

    async def set_last_active_session(self, sid):
        self.last_active = sid
        return True

    async def save_session(self, session):
        self.sessions[session.id] = session


ROME = _session("rome1234", "web-session", "Plan a one-day trip to Rome", "Colosseum at dawn…",
                title="Rome day trip", updated="2026-10-19T13:58:00")
PINE = _session("pine5678", "web-session", "Remember the secret word: pineapple", "Pineapple!",
                updated="2026-10-19T13:56:00")
CUR = _session("cur90000", "web-session", "Which planet has the most moons?", "Saturn.",
               updated="2026-10-19T14:25:00")


def _agent(sm: _SM, current: Session):
    return types.SimpleNamespace(
        session=current, session_manager=sm,
        archive_current_session_to_history=lambda: 0,
        refresh_session_runtime_flags=lambda: None,
        _sync_runtime_flags_from_session=lambda: None,
        _empty_usage=lambda: {}, last_usage=None, last_context_window={},
    )


async def _run(tool: SessionsTool, agent, **kw):
    return await tool.execute(_agent=agent, **kw)


# ── titles and resolution ───────────────────────────────────────────


def test_title_prefers_the_generated_one_then_the_first_turn():
    assert session_title(ROME) == "Rome day trip"
    assert session_title(PINE) == "Remember the secret word: pineapple"
    assert session_title(Session(id="e", name="web-session")) == "web-session"


@pytest.mark.parametrize("selector,expected", [
    ("rome1234", "rome1234"),            # exact id
    ("#2", "rome1234"),                  # list number (newest first: cur, rome, pine)
    ("3", "pine5678"),
    ("the rome trip", "rome1234"),       # title words
    ("pineapple secret word", "pine5678"),
    ("go back to the moons one", "cur90000"),
    ("the vienna trip", None),           # nothing shared beyond stopwords
    ("#9", None),
])
def test_resolve_topic(selector, expected):
    sessions = [CUR, ROME, PINE]
    got = resolve_topic(sessions, selector, current_id="cur90000")
    assert (got.id if got else None) == expected


# ── the tool ────────────────────────────────────────────────────────


async def test_list_names_topics_newest_first_and_marks_the_current_one():
    sm = _SM(ROME, PINE, CUR)
    r = await _run(SessionsTool(), _agent(sm, CUR), action="list")
    assert r.success
    lines = r.content.splitlines()
    assert lines[1].startswith("#1 Which planet has the most moons? — 2 turns") and "(current)" in lines[1]
    assert lines[2].startswith("#2 Rome day trip — 2 turns")
    assert lines[3].startswith("#3 Remember the secret word: pineapple")


async def test_switch_records_the_change_for_after_the_turn():
    sm = _SM(ROME, PINE, CUR)
    agent = _agent(sm, CUR)
    r = await _run(SessionsTool(), agent, action="switch", topic="rome trip")
    assert r.success
    assert "Rome day trip" in r.content and "after" in r.content.lower() or "as soon as" in r.content
    assert agent._pending_session_change == {"action": "switch", "id": "rome1234", "title": "Rome day trip"}
    # Nothing moved yet: the turn is still running.
    assert agent.session is CUR


async def test_switch_to_the_current_topic_or_nothing_matching():
    sm = _SM(ROME, PINE, CUR)
    agent = _agent(sm, CUR)
    same = await _run(SessionsTool(), agent, action="switch", topic="#1")
    assert same.success and "Already on this topic" in same.content
    assert not hasattr(agent, "_pending_session_change")
    none = await _run(SessionsTool(), agent, action="switch", topic="vienna")
    assert not none.success and "No topic matches" in none.error


async def test_new_records_a_fresh_topic():
    sm = _SM(CUR)
    agent = _agent(sm, CUR)
    r = await _run(SessionsTool(), agent, action="new", name="Basil")
    assert r.success
    assert agent._pending_session_change == {"action": "new", "name": "Basil"}


async def test_unavailable_on_telegram_and_for_a_shared_agent_member():
    sm = _SM(CUR)
    tg = _agent(sm, CUR)
    tg._channel = "telegram"
    r = await _run(SessionsTool(), tg, action="list")
    assert not r.success and "Telegram" in r.error
    member = _agent(sm, CUR)
    member._speaker_scoped = True
    r = await _run(SessionsTool(), member, action="switch", topic="#1")
    assert not r.success and "shared agent" in r.error


# ── carried out after the turn ──────────────────────────────────────


def _server(agent):
    s = types.SimpleNamespace(agent=agent, broadcast=[])
    s._broadcast = lambda msg, exclude=None: s.broadcast.append(msg)

    def _session_info(a=None):
        a = a or s.agent
        if not a or not a.session:
            return {}
        return {"id": a.session.id, "name": a.session.name,
                "title": (a.session.metadata or {}).get("title", ""),
                "message_count": len(a.session.messages)}

    s._session_info = _session_info
    return s


async def test_a_pending_switch_is_applied_and_announced():
    sm = _SM(ROME, PINE, CUR)
    agent = _agent(sm, CUR)
    agent._pending_session_change = {"action": "switch", "id": "rome1234", "title": "Rome day trip"}
    s = _server(agent)

    assert await apply_pending_session_change(s, agent) is True

    assert agent.session is ROME
    assert sm.last_active == "rome1234"
    assert agent._pending_session_change is None
    types_ = [m["type"] for m in s.broadcast]
    assert types_ == ["session_info", "session_switched", "command_result"]
    assert s.broadcast[0]["id"] == "rome1234" and s.broadcast[0]["message_count"] == 2
    result = s.broadcast[2]
    assert result["command"].startswith("/session switch ") and result["content"].startswith("Switched to session")
    assert result["session"]["id"] == "rome1234" and result["session"]["title"] == "Rome day trip"


async def test_a_pending_new_topic_is_applied_titled_and_announced():
    sm = _SM(CUR)
    agent = _agent(sm, CUR)
    titled = []

    async def generate_session_title(target):
        titled.append(target.id)
        return "Saturn's moons"

    agent.generate_session_title = generate_session_title
    agent._pending_session_change = {"action": "new", "name": ""}
    s = _server(agent)

    assert await apply_pending_session_change(s, agent) is True
    for _ in range(5):
        await asyncio.sleep(0)

    assert agent.session.id == "fresh"
    assert [m["type"] for m in s.broadcast][:3] == ["session_info", "session_switched", "command_result"]
    assert s.broadcast[2]["command"] == "/new" and s.broadcast[2]["content"].startswith("New session created")
    assert titled == ["cur90000"]
    assert sm.sessions["cur90000"].metadata["title"] == "Saturn's moons"
    assert {"type": "session_title", "id": "cur90000", "title": "Saturn's moons"} in s.broadcast


async def test_nothing_pending_changes_nothing():
    sm = _SM(CUR)
    agent = _agent(sm, CUR)
    s = _server(agent)
    assert await apply_pending_session_change(s, agent) is False
    assert s.broadcast == [] and agent.session is CUR


async def test_a_vanished_or_member_target_is_refused_with_a_notice():
    sm = _SM(CUR)
    agent = _agent(sm, CUR)
    agent._pending_session_change = {"action": "switch", "id": "gone0000", "title": "x"}
    s = _server(agent)
    assert await apply_pending_session_change(s, agent) is False
    assert agent.session is CUR
    assert [m["type"] for m in s.broadcast] == ["command_result"]
    assert "not found" in s.broadcast[0]["content"]
