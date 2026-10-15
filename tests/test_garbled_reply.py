"""A reply that came out garbled gets a /new hint — Jev judges.

The check (``captain_claw/garbled_reply.py``) asks Jev one yes/no question
about the reply; the web/WhatsApp/HUD turn path and the Telegram path send
the hint after the reply when Jev is confident. Never in the way: no key, an
error or a timeout means no hint.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from captain_claw import garbled_reply as gr
from captain_claw import jev_client as jc
from captain_claw import mail_authority
from captain_claw.config import get_config
from captain_claw.session import Session
from captain_claw.web_server import WebServer

GARBAGE = "the the the the the <|im_end|> {\"tool\": \"read\"} <|im_end|> the the the"


def _answer(p: float) -> dict:
    return {"answers": {gr.QUESTION_NAME: {"type": "noul", "noul": p}}, "model": "jev-latest"}


@pytest.fixture
def jev(monkeypatch):
    """Standalone with a key; JevAPI.ask scripted (the last answer repeats)."""
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: ""))
    monkeypatch.setattr(GoogleOAuthManager, "_member_call", staticmethod(lambda: False))
    monkeypatch.setenv("JEV_API_KEY", "jev-test")
    monkeypatch.setattr(get_config().tools.jev, "api_key", "")
    monkeypatch.setattr(get_config().tools.jev, "check_replies", True)
    state = types.SimpleNamespace(calls=[], answers=[_answer(0.95)])

    async def ask(self, st, questions, model=None):
        state.calls.append((st, questions))
        jc.build_request(st, questions, model)  # what would leave must validate
        ans = state.answers[0] if len(state.answers) == 1 else state.answers.pop(0)
        if isinstance(ans, BaseException):
            raise ans
        return ans

    monkeypatch.setattr(jc.JevAPI, "ask", ask)
    return state


# ── The question and the state ─────────────────────────────────────────


def test_the_question_is_a_valid_jev_noul():
    q = jc.normalize_questions(gr.QUESTIONS)[gr.QUESTION_NAME]
    assert q["type"] == "noul" and set(q["criteria"]) == {"true", "false"}


def test_state_drops_the_surface_block_and_keeps_the_end_of_a_long_reply():
    block = ("[SYSTEM CONTEXT — do not echo, quote, or acknowledge this block in your reply.]\n"
             "rules\n---\nUSER MESSAGE:\n")
    st = gr.build_state("x" * 10 + "y" * 7000, block + "what's on today?")
    assert st["person_message"] == "what's on today?"
    assert st["reply"].startswith("[…start of the reply cut…]") and st["reply"].endswith("y" * 100)
    assert len(st["reply"]) < 6100
    assert gr.build_state("fine", "") == {"reply": "fine"}


def test_hint_frame_is_a_notice_recommending_new():
    f = gr.hint_frame()
    assert f["type"] == "chat_message" and f["role"] == "assistant" and f["notice"] is True
    assert "/new" in f["content"]


# ── is_garbled ─────────────────────────────────────────────────────────


async def test_confident_yes_is_garbled_and_below_threshold_is_not(jev):
    assert await gr.is_garbled(GARBAGE, "hi") is True
    st, questions = jev.calls[0]
    assert st == {"person_message": "hi", "reply": GARBAGE} and gr.QUESTION_NAME in questions
    jev.answers = [_answer(0.6)]
    assert await gr.is_garbled("A perfectly fine answer.", "hi") is False


@pytest.mark.parametrize("why", ["off", "no_key", "empty", "member"])
async def test_no_check_and_no_call_when_it_cannot_run(jev, monkeypatch, why):
    reply = GARBAGE
    if why == "off":
        monkeypatch.setattr(get_config().tools.jev, "check_replies", False)
    elif why == "no_key":
        monkeypatch.delenv("JEV_API_KEY")
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    elif why == "empty":
        reply = "   "
    else:
        from captain_claw.google_oauth_manager import GoogleOAuthManager

        monkeypatch.setattr(GoogleOAuthManager, "_member_call", staticmethod(lambda: True))
    assert await gr.is_garbled(reply, "hi") is False
    assert jev.calls == []


@pytest.mark.parametrize("failure", [
    jc.JevError(429, "slow down"), jc.JevError(0, "unreachable"), RuntimeError("boom"),
])
async def test_a_jev_failure_means_no_hint(jev, failure):
    jev.answers = [failure]
    assert await gr.is_garbled(GARBAGE, "hi") is False


async def test_an_odd_answer_means_no_hint(jev):
    jev.answers = [{"answers": {}}]
    assert await gr.is_garbled(GARBAGE) is False


async def test_a_slow_jev_times_out_without_a_hint(jev, monkeypatch):
    async def slow(self, st, questions, model=None):
        await asyncio.sleep(5)
        return _answer(0.99)

    monkeypatch.setattr(jc.JevAPI, "ask", slow)
    monkeypatch.setattr(gr, "TIMEOUT_SECONDS", -2.9)  # wait_for gets 0.1 s
    assert await gr.is_garbled(GARBAGE) is False


async def test_under_flight_deck_the_owner_key_is_asked_with_a_short_timeout(monkeypatch):
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: "http://fd.test"))
    monkeypatch.setattr(GoogleOAuthManager, "_member_call", staticmethod(lambda: False))
    monkeypatch.setattr(get_config().tools.jev, "check_replies", True)

    async def connected(force=False):
        return True

    seen = {}

    async def via_fd(state, questions, model=None, *, timeout):
        seen.update(state=state, timeout=timeout)
        return _answer(0.9)

    monkeypatch.setattr(jc, "refresh_connected", connected)
    monkeypatch.setattr(jc, "ask_via_flight_deck", via_fd)
    assert await gr.is_garbled(GARBAGE, "hi") is True
    assert seen["timeout"] == gr.TIMEOUT_SECONDS and seen["state"]["reply"] == GARBAGE


# ── The web turn (WhatsApp / HUD / dashboard) ──────────────────────────


class FakeWS:
    def __init__(self):
        self.closed = False
        self.sent: list[str] = []

    def frames(self) -> list[dict]:
        return [json.loads(d) for d in self.sent]


@pytest.fixture
def turn(monkeypatch):
    def _send(ws, data):
        ws.sent.append(data)

    monkeypatch.setattr("captain_claw.web_server.fire_and_forget_send", _send)
    monkeypatch.setattr("captain_claw.web.chat_handler.fire_and_forget_send", _send)

    async def _noop(*a, **k):
        return None

    for target in ("captain_claw.reflections.maybe_auto_reflect",
                   "captain_claw.insights.maybe_extract_insights",
                   "captain_claw.nervous_system.maybe_dream",
                   "captain_claw.conversation_topics.maybe_classify_topics",
                   "captain_claw.intentions_generator.maybe_auto_propose"):
        monkeypatch.setattr(target, _noop)
    monkeypatch.setattr(get_config().ui, "next_steps", False)
    checked: list = []
    verdict = types.SimpleNamespace(garbled=True)

    async def fake_is_garbled(reply, message=""):
        checked.append((reply, message))
        return verdict.garbled

    monkeypatch.setattr(gr, "is_garbled", fake_is_garbled)

    class _Agent:
        def __init__(self, reply):
            self.reply = reply
            self.session = Session(id="main", name="default")
            self.last_usage: dict = {}
            self.total_usage: dict = {}
            self.last_context_window: dict = {}
            self.provider = object()
            self.tools = types.SimpleNamespace(set_session_policy=lambda *a: None,
                                               clear_session_policy=lambda *a: None,
                                               list_tools=lambda *a, **k: [])

        def get_runtime_model_details(self):
            return {"provider": "p", "model": "m"}

        def _current_session_slug(self):
            return "default"

        async def complete(self, content):
            return self.reply

    async def run(reply, *, automation=None, **kw):
        from captain_claw.web import chat_handler

        agent = _Agent(reply)
        s = WebServer.__new__(WebServer)
        s.agent = agent
        s._lane_agents, s._lane_locks, s._lane_sockets, s._public_agents = {}, {}, {}, {}
        s._busy, s._active_task, s._orchestrator = False, None, None
        viewer = FakeWS()
        s.clients = {viewer}
        await chat_handler._run_agent(s, viewer, agent, "what's on today?", None,
                                      no_flow=True, automation=automation, **kw)
        for _ in range(5):
            await asyncio.sleep(0)
        return viewer.frames()

    return types.SimpleNamespace(run=run, checked=checked, verdict=verdict)


def _kinds(frames):
    out = []
    for f in frames:
        if f.get("type") == "chat_message" and f.get("role") == "assistant":
            out.append("hint" if f.get("notice") else "reply")
        elif f.get("type") == "status" and f.get("status") == "ready":
            out.append("ready")
    return out


async def test_a_garbled_reply_gets_the_hint_after_the_reply_and_before_ready(turn):
    frames = await turn.run(GARBAGE)
    assert _kinds(frames) == ["reply", "hint", "ready"]
    assert turn.checked == [(GARBAGE, "what's on today?")]
    hint = next(f for f in frames if f.get("notice"))
    assert hint["content"] == gr.HINT


async def test_a_good_reply_gets_no_hint(turn):
    turn.verdict.garbled = False
    assert _kinds(await turn.run("Two meetings: 10:00 and 15:00.")) == ["reply", "ready"]
    assert len(turn.checked) == 1


async def test_automated_and_flow_consult_turns_are_not_checked(turn):
    auto = mail_authority.Authority(mode="automated", kind="peer_relay")
    assert _kinds(await turn.run(GARBAGE, automation=auto)) == ["reply", "ready"]
    assert _kinds(await turn.run(GARBAGE, no_broadcast=True)) == ["reply", "ready"]
    assert turn.checked == []


async def test_a_worker_turn_is_not_checked(turn, monkeypatch):
    from captain_claw.agent_reasoning_mixin import _FD_WORKER_MARKERS

    monkeypatch.setenv(_FD_WORKER_MARKERS[0], "1")
    await turn.run(GARBAGE)
    assert turn.checked == []
