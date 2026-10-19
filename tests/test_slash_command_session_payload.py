"""Every web ``command_result`` names the lane's session AFTER the command.

A client that keeps one continuous transcript per lane (Flight Deck's chat
timeline) marks a topic boundary from it: ``/new`` and ``/session switch``
move the context to another session, ``/clear`` resets the same one.
"""

from __future__ import annotations

import types

import pytest

from captain_claw.session import Session
from captain_claw.web.slash_commands import handle_command


class _SM:
    def __init__(self, *sessions: Session) -> None:
        self.sessions = {s.id: s for s in sessions}
        self.last_active: str | None = None

    async def create_session(self, name=None, metadata=None):
        s = Session(id="fresh", name=name or "web-session")
        self.sessions[s.id] = s
        return s

    async def set_last_active_session(self, sid):
        self.last_active = sid
        return True

    async def load_session(self, sid):
        return self.sessions.get(sid)

    async def load_session_by_name(self, name):
        for s in self.sessions.values():
            if s.name == name:
                return s
        return None

    async def list_sessions(self, limit=10):
        return list(self.sessions.values())[:limit]

    async def save_session(self, session):
        self.sessions[session.id] = session


def _agent(session: Session, sm: _SM):
    return types.SimpleNamespace(
        session=session, session_manager=sm,
        archive_current_session_to_history=lambda: 0,
        refresh_session_runtime_flags=lambda: None,
        _sync_runtime_flags_from_session=lambda: None,
        _empty_usage=lambda: {},
        last_usage=None, last_context_window={},
    )


def _server(agent):
    sent: list[dict] = []
    s = types.SimpleNamespace(agent=agent, sent=sent)

    async def _send(ws, msg):
        sent.append(msg)

    def _broadcast(msg, exclude=None):
        return None

    def _session_info(a=None):
        a = a or s.agent
        if not a or not a.session:
            return {}
        return {"id": a.session.id, "name": a.session.name,
                "message_count": len(a.session.messages)}

    s._send = _send
    s._broadcast = _broadcast
    s._session_info = _session_info
    return s


def _result(server) -> dict:
    results = [m for m in server.sent if m.get("type") == "command_result"]
    assert results, "no command_result was sent"
    return results[-1]


@pytest.fixture
def ws():
    return types.SimpleNamespace()


async def test_new_names_the_session_it_switched_to(ws):
    old = Session(id="old", name="web-session")
    old.add_message("user", "hello")
    sm = _SM(old)
    s = _server(_agent(old, sm))

    await handle_command(s, ws, "/new")

    r = _result(s)
    assert r["session"] == {"id": "fresh", "name": "web-session", "message_count": 0}
    assert sm.last_active == "fresh"


async def test_session_switch_names_the_resumed_session(ws):
    old = Session(id="old", name="trip")
    old.add_message("user", "plan the trip")
    old.add_message("assistant", "sure")
    cur = Session(id="cur", name="web-session")
    sm = _SM(old, cur)
    s = _server(_agent(cur, sm))

    await handle_command(s, ws, "/session switch old")

    r = _result(s)
    assert "Switched" in r["content"]
    assert r["session"] == {"id": "old", "name": "trip", "message_count": 2}


async def test_clear_keeps_the_same_session(ws):
    cur = Session(id="cur", name="web-session")
    cur.add_message("user", "hello")
    s = _server(_agent(cur, _SM(cur)))

    await handle_command(s, ws, "/clear")

    r = _result(s)
    assert r["session"]["id"] == "cur"
    assert r["session"]["message_count"] == 0


async def test_no_session_means_an_empty_summary(ws):
    s = _server(_agent(None, _SM()))  # type: ignore[arg-type]

    await handle_command(s, ws, "/help")

    assert _result(s)["session"] == {}
