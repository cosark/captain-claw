"""/new on Telegram starts a fresh session for that Telegram user.

It used to answer "Not available on Telegram". Each user still has ONE
session at a time — /new replaces it (the old one is frozen into history and
the mapping persists), but listing and switching stay off.
"""

from __future__ import annotations

import types

import pytest

from captain_claw.session import Session
from captain_claw.web import telegram as tg

USER = "4242"


class _SessionManager:
    def __init__(self):
        self.created: list[Session] = []

    async def create_session(self, name: str = "", **kw):
        s = Session(id=f"sess-{len(self.created) + 1}", name=name)
        self.created.append(s)
        return s


def _agent(session: Session):
    a = types.SimpleNamespace(
        _user_id=USER, session=session, session_manager=_SessionManager(),
        archived=[], refreshed=0, last_usage={"x": 1}, last_context_window={"y": 2},
    )
    a.archive_current_session_to_history = lambda: a.archived.append(a.session.id) or 1

    def _refresh():
        a.refreshed += 1

    a.refresh_session_runtime_flags = _refresh
    a._empty_usage = lambda: {}
    return a


@pytest.fixture
def env(monkeypatch):
    server = types.SimpleNamespace(
        _telegram_user_sessions={USER: "old"},
        _telegram_user_locks={},
        _approved_telegram_users={USER: {"username": "stevica"}},
    )
    saved: list = []

    async def _save(s):
        saved.append(dict(s._telegram_user_sessions))

    monkeypatch.setattr(tg, "_tg_save_user_sessions", _save)
    old = Session(id="old", name="tg-stevica")
    old.add_message("user", "hello")
    return types.SimpleNamespace(server=server, agent=_agent(old), saved=saved)


async def test_new_starts_a_fresh_session_and_remembers_it(env):
    out = await tg.execute_telegram_command(env.server, "/new", env.agent)
    assert out.startswith("New session started: tg-stevica")
    assert env.agent.archived == ["old"]                       # the old one kept in history
    assert env.agent.session.id == "sess-1" and env.agent.session.messages == []
    assert env.server._telegram_user_sessions[USER] == "sess-1"
    assert env.saved == [{USER: "sess-1"}]                     # survives a restart
    assert env.agent.refreshed == 1
    assert env.agent.last_usage == {} and env.agent.last_context_window == {}


async def test_new_and_session_new_take_a_name(env):
    await tg.execute_telegram_command(env.server, "/new trip plan", env.agent)
    assert env.agent.session.name == "trip plan"
    await tg.execute_telegram_command(env.server, "/session new budget", env.agent)
    assert env.agent.session.name == "budget"
    assert env.server._telegram_user_sessions[USER] == "sess-2"


async def test_new_waits_for_a_running_reply(env):
    lock = tg._tg_get_user_lock(env.server, USER)
    async with lock:
        out = await tg.execute_telegram_command(env.server, "/new", env.agent)
    assert "Still answering" in out
    assert env.agent.session.id == "old" and env.saved == []


@pytest.mark.parametrize("raw", ["/sessions", "/session switch 2", "/session list", "/session load x"])
async def test_listing_and_switching_stay_off(env, raw):
    out = await tg.execute_telegram_command(env.server, raw, env.agent)
    assert out.startswith("Not available on Telegram")
    assert env.agent.session.id == "old"


async def test_help_lists_new(monkeypatch):
    sent: list = []

    async def _send(server, chat_id, text, **kw):
        sent.append(text)

    async def _agent_for(server, message):
        return types.SimpleNamespace(session=None)

    monkeypatch.setattr(tg, "_tg_send", _send)
    monkeypatch.setattr(tg, "_tg_get_or_create_agent", _agent_for)
    server = types.SimpleNamespace(
        _telegram_bridge=object(), agent=object(), _approved_telegram_users={USER: {}},
    )
    msg = types.SimpleNamespace(
        business_connection_id="", user_id=int(USER), chat_id=1, message_id=1, text="/help",
        photo_file_id="", document_file_id="", contact_phone="", username="", first_name="",
    )
    await tg._handle_telegram_message(server, msg)
    assert sent and "/new" in sent[0]


def test_garbled_hint_is_wired_into_the_telegram_turn():
    """The Telegram turn asks the same check and sends the same hint."""
    import inspect

    src = inspect.getsource(tg._tg_process_with_typing)
    assert "garbled_reply.is_garbled(" in src and "garbled_reply.HINT" in src
