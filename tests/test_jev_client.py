"""captain_claw.jev_client — the one Jev module every surface uses.

* request shaping: normalize_questions (API map, list, JSON text, aliases,
  options / levels / yes_means shorthands) and its refusals, parse_state,
  build_request;
* reading answers: answer_value(s), format_answers, format_models, key_hint;
* JevAPI: body and headers, retries on 429 / 529 / transport errors honouring
  Retry-After (capped), error mapping (401 / 403 / 404 / 422 / 429 / 529),
  status 0 when Jev can't be reached;
* the agent side under Flight Deck: refresh_connected (rate-limited, member
  turns never refresh, an unreachable deck keeps the flag), and
  ask_via_flight_deck / models_via_flight_deck (owner headers only — never a
  speaker grant — member turns refused before any request, FD's 404 detail
  relayed as-is).

No network: Jev and Flight Deck answer through httpx.MockTransport.
"""

from __future__ import annotations

import json
import time
import types

import httpx
import pytest

from captain_claw import jev_client as jc

_RealAsyncClient = httpx.AsyncClient  # tests patch httpx.AsyncClient itself
FD = "http://fd.test"


@pytest.fixture(autouse=True)
def _config(monkeypatch, tmp_path):
    import captain_claw.config as config_mod
    from captain_claw.config import Config
    from captain_claw.flight_deck import agent_secret
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    for name in ("JEV_API_KEY", "TYPESAFE_API_KEY", "FD_URL"):
        monkeypatch.delenv(name, raising=False)
    agent_secret.reset_cache_for_tests()
    cfg = Config()
    cfg.web.auth_token = "AGENT-TOKEN"
    monkeypatch.setattr(config_mod, "_config", cfg)
    # Standalone unless a test says otherwise; never a member's turn.
    monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: ""))
    monkeypatch.setattr(GoogleOAuthManager, "_member_call", staticmethod(lambda: False))
    jc._CONNECTED = (False, 0.0)
    jc._LAST_CHECK_AT = 0.0
    yield cfg
    jc._CONNECTED = (False, 0.0)
    jc._LAST_CHECK_AT = 0.0
    agent_secret.reset_cache_for_tests()


@pytest.fixture
def sleeps(monkeypatch):
    """Record JevAPI's backoff waits instead of sleeping (only jev_client's
    asyncio is replaced)."""
    waits: list[float] = []

    async def _sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(jc, "asyncio", types.SimpleNamespace(sleep=_sleep))
    return waits


class _Server:
    """A scripted HTTP peer: each request pops the next response (the last
    one repeats); records the requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        hit = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(hit, Exception):
            raise hit
        if callable(hit):
            return hit(request)
        return hit

    def body(self, i: int = -1):
        return json.loads(self.requests[i].content or b"null")


def _api(server: _Server, **kw) -> jc.JevAPI:
    return jc.JevAPI("jv-secret-key-1234", client=_RealAsyncClient(transport=httpx.MockTransport(server)), **kw)


def _ok(answers=None, **extra) -> httpx.Response:
    return httpx.Response(200, json={
        "model": "jev-latest",
        "answers": answers if answers is not None else {"urgent": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 42, "output_tokens": 1},
        **extra,
    })


_Q = [{"name": "urgent", "type": "noul", "instructions": "Does this need a reply today?"}]


# ── normalize_questions ──────────────────────────────────────────────


class TestNormalizeQuestions:
    def test_api_map_passes_through(self):
        raw = {
            "urgent": {"type": "noul", "instructions": "Urgent?", "criteria": {"true": "due today"}},
            "topic": {"type": "choice", "instructions": "Topic?", "criteria": {"billing": "money", "bug": None}},
            "tone": {"type": "score", "instructions": "How polite?", "criteria": ["rude", "neutral", "warm"]},
        }
        out = jc.normalize_questions(raw)
        assert out == {
            "urgent": {"type": "noul", "instructions": "Urgent?", "criteria": {"true": "due today"}},
            "topic": {"type": "choice", "instructions": "Topic?", "criteria": {"billing": "money", "bug": None}},
            "tone": {"type": "score", "instructions": "How polite?", "criteria": ["rude", "neutral", "warm"]},
        }

    def test_list_with_shorthands(self):
        out = jc.normalize_questions([
            {"name": " urgent ", "type": "yes/no", "instructions": " Urgent? ",
             "yes_means": "due today", "no_means": "  "},
            {"name": "topic", "type": "choice", "instructions": "Topic?",
             "options": [{"name": "billing", "description": "money"}, {"name": "bug"}, "other"]},
            {"name": "tone", "type": "rating", "question": "How polite?", "levels": ["rude", "warm"]},
            {"name": "plain", "type": "boolean", "instructions": "Plain?"},
        ])
        assert out["urgent"] == {"type": "noul", "instructions": "Urgent?", "criteria": {"true": "due today"}}
        assert out["topic"]["criteria"] == {"billing": "money", "bug": None, "other": None}
        assert out["tone"] == {"type": "score", "instructions": "How polite?", "criteria": ["rude", "warm"]}
        assert out["plain"] == {"type": "noul", "instructions": "Plain?"}  # no criteria key at all

    def test_json_text_and_default_names(self):
        out = jc.normalize_questions(json.dumps([
            {"type": "noul", "instructions": "A?"},
            {"type": "noul", "instructions": "B?"},
        ]))
        assert list(out) == ["q1", "q2"]

    def test_options_as_a_map_and_noul_yes_no_keys(self):
        out = jc.normalize_questions({
            "c": {"type": "select", "instructions": "C?", "options": {"a": "first", "b": ""}},
            "n": {"type": "noul", "instructions": "N?", "criteria": {"Yes": "y", "no": "n"}},
        })
        assert out["c"]["criteria"] == {"a": "first", "b": None}
        assert out["n"]["criteria"] == {"true": "y", "false": "n"}

    @pytest.mark.parametrize("raw,match", [
        ([], "at least one question"),
        ("", "questions is empty"),
        ("[not json", "not valid JSON"),
        (42, "must be a list of questions or a map"),
        (["x"], "#1 must be an object"),
        ({"a": "x"}, "'a' must be an object"),
        ([{"name": "a", "instructions": "A?"}], "type must be one of"),
        ([{"name": "a", "type": "essay", "instructions": "A?"}], "type must be one of"),
        ([{"name": "a", "type": "noul"}], "instructions"),
        ([{"name": "a", "type": "noul", "instructions": "  "}], "instructions"),
        ([{"name": "a", "type": "noul", "instructions": "A?"},
          {"name": "a", "type": "noul", "instructions": "B?"}], "used twice"),
        ({" ": {"type": "noul", "instructions": "A?"}}, "needs a name"),
        ([{"name": "c", "type": "choice", "instructions": "C?"}], "needs options"),
        ([{"name": "c", "type": "choice", "instructions": "C?", "options": ["only"]}], "2–255 options, got 1"),
        ([{"name": "c", "type": "choice", "instructions": "C?", "options": ["a", "a"]}], "appears twice"),
        ([{"name": "c", "type": "choice", "instructions": "C?", "options": ["a", {"description": "x"}]}],
         "every option needs a name"),
        ([{"name": "c", "type": "choice", "instructions": "C?", "options": "a,b"}], "list of names or a map"),
        ([{"name": "s", "type": "score", "instructions": "S?"}], "needs levels"),
        ([{"name": "s", "type": "score", "instructions": "S?", "levels": ["one"]}], "2–10 levels, got 1"),
        ([{"name": "s", "type": "score", "instructions": "S?", "levels": [str(i) for i in range(11)]}],
         "2–10 levels, got 11"),
        ([{"name": "s", "type": "score", "instructions": "S?", "levels": ["low", " "]}], "needs a description"),
        ([{"name": "n", "type": "noul", "instructions": "N?", "criteria": {"maybe": "x"}}], "only 'true' and 'false'"),
        ([{"name": "n", "type": "noul", "instructions": "N?", "criteria": ["x"]}], "must be an object"),
    ])
    def test_refusals(self, raw, match):
        with pytest.raises(jc.JevInputError, match=match):
            jc.normalize_questions(raw)

    def test_runaway_guard(self):
        many = [{"type": "noul", "instructions": f"Q{i}?"} for i in range(jc.MAX_QUESTIONS + 1)]
        with pytest.raises(jc.JevInputError, match=f"at most {jc.MAX_QUESTIONS}"):
            jc.normalize_questions(many)
        assert len(jc.normalize_questions(many[:jc.MAX_QUESTIONS])) == jc.MAX_QUESTIONS

    def test_input_error_is_a_value_error(self):
        assert issubclass(jc.JevInputError, ValueError)


# ── parse_state / build_request ──────────────────────────────────────


class TestState:
    def test_text_is_stripped_and_kept(self):
        assert jc.parse_state("  hello there \n") == "hello there"

    def test_json_text_becomes_structured(self):
        assert jc.parse_state(' {"a": 1} ') == {"a": 1}
        assert jc.parse_state("[1, 2]") == [1, 2]

    def test_brace_text_that_is_not_json_stays_text(self):
        assert jc.parse_state("{not json} but notes") == "{not json} but notes"
        assert jc.parse_state('"quoted"') == '"quoted"'  # a JSON scalar stays text

    def test_objects_pass_through(self):
        assert jc.parse_state({"k": [1]}) == {"k": [1]}

    @pytest.mark.parametrize("raw,match", [
        (None, "state is required"),
        ("   ", "state is empty"),
        ({}, "state is empty"),
        ("[]", "state is empty"),
    ])
    def test_refusals(self, raw, match):
        with pytest.raises(jc.JevInputError, match=match):
            jc.parse_state(raw)

    def test_too_long(self):
        with pytest.raises(jc.JevInputError, match="Send only the part"):
            jc.parse_state("x" * (jc.MAX_STATE_CHARS + 1))
        assert len(jc.parse_state("x" * jc.MAX_STATE_CHARS)) == jc.MAX_STATE_CHARS
        with pytest.raises(jc.JevInputError, match="characters"):
            jc.parse_state({"t": "x" * jc.MAX_STATE_CHARS})

    def test_build_request(self):
        body = jc.build_request("text", _Q)
        assert body == {
            "model": "jev-latest",
            "state": "text",
            "questions": {"urgent": {"type": "noul", "instructions": "Does this need a reply today?"}},
        }
        assert jc.build_request("t", _Q, "  jev-2 ")["model"] == "jev-2"
        assert jc.build_request("t", _Q, "  ")["model"] == jc.DEFAULT_MODEL


# ── answers ──────────────────────────────────────────────────────────


_RESPONSE = {
    "model": "jev-1",
    "answers": {
        "urgent": {"type": "noul", "noul": 0.82},
        "topic": {"type": "choice", "choice": "billing", "confidence": 0.7,
                  "probabilities": {"bug": 0.2, "billing": 0.7, "other": 0.1}},
        "tone": {"type": "score", "score": 2.4, "confidence": 0.55,
                 "legend": {"1": "rude", "2": "neutral", "3": "warm"},
                 "probabilities": {"1": 0.1, "2": 0.4, "3": 0.5}},
        "odd": {"type": "mystery", "x": 1},
    },
    "usage": {"input_tokens": 120, "output_tokens": 3},
}


class TestAnswers:
    def test_values(self):
        assert jc.answer_values(_RESPONSE) == {"urgent": 0.82, "topic": "billing", "tone": 2.4, "odd": None}
        assert jc.answer_values({}) == {} and jc.answer_values("nope") == {}
        assert jc.answer_value({"type": "noul", "noul": "bad"}) is None

    def test_format(self):
        text = jc.format_answers(_RESPONSE)
        lines = text.splitlines()
        assert lines[0] == "- urgent [yes/no]: p(yes) = 0.82 → yes"
        assert lines[1] == "- topic [choice]: billing (confidence 0.70) · billing 0.70, bug 0.20, other 0.10"
        assert lines[2] == '- tone [score]: 2.40 on 1–3 (confidence 0.55) · nearest level 2: "neutral"'
        assert lines[3].startswith("- odd: {")
        assert lines[-1] == "(model jev-1 · 120 input tokens)"

    def test_noul_words(self):
        def word(p):
            return jc.format_answers({"answers": {"q": {"type": "noul", "noul": p}}}).splitlines()[0].rsplit(" ", 1)[-1]

        assert word(0.65) == "yes" and word(0.35) == "no" and word(0.5) == "uncertain"

    def test_many_choice_options_are_trimmed(self):
        probs = {f"o{i}": 0.1 for i in range(8)}
        line = jc.format_answers({"answers": {"c": {"type": "choice", "choice": "o0", "probabilities": probs}}})
        assert ", +3 more" in line

    def test_no_answers(self):
        assert jc.format_answers({"answers": {}}) == "Jev returned no answers."
        assert jc.format_answers(None) == "Jev returned no answers."

    def test_models_and_key_hint(self):
        text = jc.format_models([{"name": "jev-1", "description": "first", "release_date": "2026-01-01"}])
        assert text == "Jev models:\n- jev-1 (2026-01-01) — first"
        assert jc.format_models([]) == "Jev lists no models for this key."
        assert jc.key_hint("jv-secret-key-ab12") == "····ab12"
        assert jc.key_hint("short") == "····" and jc.key_hint("") == ""


# ── JevAPI ───────────────────────────────────────────────────────────


class TestJevAPI:
    async def test_ask_body_and_headers(self):
        server = _Server(_ok())
        resp = await _api(server, base_url="https://jev.example/").ask("Pay the invoice today", _Q, "jev-2")
        assert resp["answers"]["urgent"]["noul"] == 0.9
        req = server.requests[0]
        assert req.method == "POST" and str(req.url) == "https://jev.example/v1/systemone"
        assert req.headers["Authorization"] == "Bearer jv-secret-key-1234"
        assert req.headers["Accept"] == "application/json"
        assert server.body() == {
            "model": "jev-2",
            "state": "Pay the invoice today",
            "questions": {"urgent": {"type": "noul", "instructions": "Does this need a reply today?"}},
        }

    async def test_models(self):
        server = _Server(httpx.Response(200, json={"models": [{"name": "jev-1"}, "junk"]}))
        assert await _api(server).models() == [{"name": "jev-1"}]
        assert server.requests[0].method == "GET"
        assert str(server.requests[0].url) == "https://api.typesafe.ai/v1/models"

    async def test_bad_input_never_leaves(self):
        server = _Server(_ok())
        with pytest.raises(jc.JevInputError):
            await _api(server).ask("text", [{"name": "a", "type": "choice", "instructions": "A?"}])
        assert server.requests == []

    async def test_no_key(self):
        server = _Server(_ok())
        api = jc.JevAPI("  ", client=_RealAsyncClient(transport=httpx.MockTransport(server)))
        with pytest.raises(jc.JevError) as exc:
            await api.ask("text", _Q)
        assert exc.value.status == 0 and server.requests == []

    async def test_429_retried_honouring_retry_after(self, sleeps):
        server = _Server(httpx.Response(429, headers={"retry-after": "3"}, json={"detail": "slow down"}), _ok())
        resp = await _api(server).ask("text", _Q)
        assert resp["model"] == "jev-latest"
        assert sleeps == [3.0] and len(server.requests) == 2

    async def test_retry_wait_is_capped_and_backs_off(self, sleeps):
        server = _Server(
            httpx.Response(529, headers={"retry-after": "120"}),
            httpx.Response(503),
            _ok(),
        )
        await _api(server).ask("text", _Q)
        assert sleeps == [8.0, 1.5]  # capped; then 0.5 * 3 ** 1 with no header

    async def test_retries_exhausted(self, sleeps):
        server = _Server(httpx.Response(529, headers={"retry-after": "2"}))
        with pytest.raises(jc.JevError) as exc:
            await _api(server, max_retries=2).ask("text", _Q)
        assert exc.value.status == 529 and "overloaded" in str(exc.value)
        assert exc.value.retry_after == 2.0
        assert len(server.requests) == 3 and sleeps == [2.0, 2.0]

    async def test_429_without_retries(self, sleeps):
        server = _Server(httpx.Response(429, headers={"retry-after": "7"}))
        with pytest.raises(jc.JevError) as exc:
            await _api(server, max_retries=0).ask("text", _Q)
        assert exc.value.status == 429 and exc.value.retry_after == 7.0
        assert "rate limit" in str(exc.value) and sleeps == []

    async def test_401_is_not_retried(self, sleeps):
        server = _Server(httpx.Response(401, json={"detail": "bad key"}, headers={"x-typesafe-request-id": "req-9"}))
        with pytest.raises(jc.JevError) as exc:
            await _api(server).ask("text", _Q)
        assert exc.value.status == 401
        assert "refused the API key" in str(exc.value) and "(request req-9)" in str(exc.value)
        assert len(server.requests) == 1 and sleeps == []

    async def test_403_and_404_keep_the_detail(self):
        server = _Server(httpx.Response(403, json={"detail": "plan exhausted"}))
        with pytest.raises(jc.JevError, match=r"denied access \(403\): plan exhausted"):
            await _api(server).ask("text", _Q)
        server = _Server(httpx.Response(404, json={"detail": "no such model"}))
        with pytest.raises(jc.JevError, match="no such model — is the model name right"):
            await _api(server).ask("text", _Q, "jev-nope")

    async def test_422_validation_detail(self):
        server = _Server(httpx.Response(422, json={"detail": [
            {"loc": ["body", "questions", "urgent", "type"], "msg": "bad type"},
            {"loc": ["body"], "msg": "whole body"},
        ]}))
        with pytest.raises(jc.JevError) as exc:
            await _api(server).ask("text", _Q)
        assert exc.value.status == 422
        assert str(exc.value) == "Jev rejected the request (422): questions.urgent.type: bad type; whole body"

    async def test_other_status_and_plain_text_detail(self):
        server = _Server(httpx.Response(418, text="teapot"))
        with pytest.raises(jc.JevError, match=r"Jev API error \(HTTP 418\): teapot"):
            await _api(server).ask("text", _Q)

    async def test_network_error_is_status_0(self, sleeps):
        server = _Server(httpx.ConnectError("boom"))
        with pytest.raises(jc.JevError) as exc:
            await _api(server, max_retries=1).ask("text", _Q)
        assert exc.value.status == 0
        assert str(exc.value) == "Could not reach Jev (failed: ConnectError)"
        assert len(server.requests) == 2 and sleeps == [0.5]

    async def test_timeout_is_status_0(self, sleeps):
        server = _Server(httpx.ReadTimeout("slow"))
        with pytest.raises(jc.JevError, match=r"timed out: ReadTimeout") as exc:
            await _api(server, max_retries=0).ask("text", _Q)
        assert exc.value.status == 0

    async def test_transport_error_then_success(self, sleeps):
        server = _Server(httpx.ConnectError("blip"), _ok())
        assert (await _api(server).ask("text", _Q))["answers"]
        assert sleeps == [0.5]

    async def test_odd_success_bodies(self):
        with pytest.raises(jc.JevError, match="non-JSON"):
            await _api(_Server(httpx.Response(200, text="<html>"))).ask("text", _Q)
        with pytest.raises(jc.JevError, match="unexpected response"):
            await _api(_Server(httpx.Response(200, json=[1]))).ask("text", _Q)
        with pytest.raises(jc.JevError, match="no answers") as exc:
            await _api(_Server(httpx.Response(200, json={"model": "m"}))).ask("text", _Q)
        assert exc.value.status == 200

    async def test_without_a_client_uses_a_fresh_httpx_client(self, monkeypatch):
        server = _Server(_ok())
        monkeypatch.setattr(jc.httpx, "AsyncClient",
                            lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(server), **kw))
        await jc.JevAPI("jv-secret-key-1234", timeout=12).ask("text", _Q)
        assert server.requests[0].headers["Authorization"] == "Bearer jv-secret-key-1234"


# ── standalone key ───────────────────────────────────────────────────


class TestStandalone:
    def test_key_from_config_then_env(self, _config, monkeypatch):
        assert jc.standalone_api_key() == ""
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
        assert jc.standalone_api_key() == "ts-key"
        monkeypatch.setenv("JEV_API_KEY", "jev-key")
        assert jc.standalone_api_key() == "jev-key"
        _config.tools.jev.api_key = " cfg-key "
        assert jc.standalone_api_key() == "cfg-key"

    def test_standalone_api_uses_config(self, _config):
        _config.tools.jev.api_key = "cfg-key"
        _config.tools.jev.base_url = "https://jev.example/"
        _config.tools.jev.timeout = 9
        api = jc.standalone_api()
        assert api.api_key == "cfg-key" and api.base_url == "https://jev.example" and api.timeout == 9.0

    def test_usable_and_refresh_standalone(self, _config):
        assert jc.under_flight_deck() is False and jc.jev_usable() is False
        _config.tools.jev.api_key = "cfg-key"
        assert jc.jev_usable() is True


# ── under Flight Deck ────────────────────────────────────────────────


class TestFlightDeck:
    def _fd(self, monkeypatch, *responses) -> _Server:
        from captain_claw.google_oauth_manager import GoogleOAuthManager

        monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: FD))
        server = _Server(*responses)
        monkeypatch.setattr(jc.httpx, "AsyncClient",
                            lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(server), **kw))
        return server

    def _member(self, monkeypatch):
        from captain_claw.google_oauth_manager import GoogleOAuthManager

        monkeypatch.setattr(GoogleOAuthManager, "_member_call", staticmethod(lambda: True))

    async def test_refresh_reads_the_owner_status(self, monkeypatch, _config):
        _config.tools.jev.api_key = "standalone-key-is-ignored"
        server = self._fd(monkeypatch, httpx.Response(200, json={"connected": True}))
        assert jc.jev_usable() is False  # under FD the standalone key means nothing
        assert await jc.refresh_connected() is True
        assert jc.is_jev_connected_cached() and jc.jev_usable()
        req = server.requests[0]
        assert req.method == "GET" and str(req.url) == f"{FD}/fd/jev/agent/status"
        assert req.headers["X-Agent-Auth"] == "AGENT-TOKEN"
        assert req.headers.get("X-Agent-Secret")
        assert "X-FD-Speaker-Grant" not in req.headers
        assert "Authorization" not in req.headers

    async def test_refresh_is_rate_limited_unless_forced(self, monkeypatch):
        server = self._fd(monkeypatch, httpx.Response(200, json={"connected": True}),
                          httpx.Response(200, json={"connected": False}))
        assert await jc.refresh_connected() is True
        assert await jc.refresh_connected() is True and len(server.requests) == 1
        assert await jc.refresh_connected(force=True) is False and len(server.requests) == 2
        assert jc.jev_usable() is False

    async def test_refresh_after_the_gap(self, monkeypatch):
        server = self._fd(monkeypatch, httpx.Response(200, json={"connected": False}))
        jc._mark(True)
        jc._LAST_CHECK_AT = time.time() - jc._REFRESH_MIN_GAP - 1
        assert await jc.refresh_connected() is False and len(server.requests) == 1

    async def test_throttled_refresh_keeps_a_connected_flag_fresh(self, monkeypatch):
        # A long turn between two throttled refreshes must not age the flag
        # out from under the call (the registry re-checks it at execute time).
        server = self._fd(monkeypatch, httpx.Response(200, json={"connected": True}))
        assert await jc.refresh_connected() is True and len(server.requests) == 1
        jc._CONNECTED = (True, time.time() - jc._CACHE_MAX_AGE + 5)  # nearly stale
        assert await jc.refresh_connected() is True and len(server.requests) == 1
        jc._CONNECTED = (True, jc._CONNECTED[1] - 10)
        assert jc.is_jev_connected_cached() is True

    async def test_not_connected_is_rechecked_every_time(self, monkeypatch):
        # The user saves a key in Flight Deck: the very next turn sees it.
        server = self._fd(monkeypatch, httpx.Response(200, json={"connected": False}),
                          httpx.Response(200, json={"connected": True}))
        assert await jc.refresh_connected() is False
        assert await jc.refresh_connected() is True and len(server.requests) == 2

    async def test_stale_flag_hides(self, monkeypatch):
        self._fd(monkeypatch, httpx.Response(200, json={"connected": True}))
        jc._CONNECTED = (True, time.time() - jc._CACHE_MAX_AGE - 1)
        assert jc.is_jev_connected_cached() is False and jc.jev_usable() is False

    @pytest.mark.parametrize("status", [404, 401, 403, 500])
    async def test_refresh_refused_marks_not_connected(self, monkeypatch, status):
        self._fd(monkeypatch, httpx.Response(status, json={"detail": "nope"}))
        jc._mark(True)
        assert await jc.refresh_connected(force=True) is False
        assert jc.is_jev_connected_cached() is False

    async def test_unreachable_deck_keeps_the_flag(self, monkeypatch):
        self._fd(monkeypatch, httpx.ConnectError("down"))
        jc._mark(True)
        assert await jc.refresh_connected(force=True) is True
        jc._CONNECTED = (False, 0.0)
        jc._LAST_CHECK_AT = 0.0
        assert await jc.refresh_connected(force=True) is False

    async def test_member_turn_never_refreshes(self, monkeypatch):
        server = self._fd(monkeypatch, httpx.Response(200, json={"connected": False}))
        jc._mark(True)
        self._member(monkeypatch)
        assert await jc.refresh_connected(force=True) is False
        assert server.requests == [] and jc.is_jev_connected_cached() is True  # owner's flag kept

    async def test_ask_via_flight_deck(self, monkeypatch):
        fd_answer = {"model": "jev-latest", "answers": {"urgent": {"type": "noul", "noul": 0.2}},
                     "usage": {"input_tokens": 9}, "values": {"urgent": 0.2}, "summary": "x", "latency_ms": 5}
        server = self._fd(monkeypatch, httpx.Response(200, json=fd_answer))
        resp = await jc.ask_via_flight_deck('{"subject": "hi"}', _Q)
        assert resp == fd_answer and jc.is_jev_connected_cached()
        req = server.requests[0]
        assert req.method == "POST" and str(req.url) == f"{FD}/fd/jev/agent/ask"
        assert req.headers["X-Agent-Auth"] == "AGENT-TOKEN" and "X-FD-Speaker-Grant" not in req.headers
        assert server.body() == {
            "model": "jev-latest",
            "state": {"subject": "hi"},
            "questions": {"urgent": {"type": "noul", "instructions": "Does this need a reply today?"}},
        }

    async def test_bad_input_never_reaches_flight_deck(self, monkeypatch):
        server = self._fd(monkeypatch, httpx.Response(200, json={}))
        with pytest.raises(jc.JevInputError):
            await jc.ask_via_flight_deck("", _Q)
        assert server.requests == []

    async def test_member_turn_refused_before_any_request(self, monkeypatch):
        server = self._fd(monkeypatch, httpx.Response(200, json={}))
        self._member(monkeypatch)
        with pytest.raises(jc.JevError) as exc:
            await jc.ask_via_flight_deck("text", _Q)
        assert str(exc.value) == jc.MEMBER_REFUSED and exc.value.status == 0
        with pytest.raises(jc.JevError, match="shared-agent member"):
            await jc.models_via_flight_deck()
        assert server.requests == []

    async def test_not_connected_detail_is_relayed(self, monkeypatch):
        self._fd(monkeypatch, httpx.Response(404, json={"detail": jc.NOT_CONNECTED}))
        jc._mark(True)
        with pytest.raises(jc.JevError) as exc:
            await jc.ask_via_flight_deck("text", _Q)
        assert exc.value.status == 404 and str(exc.value) == jc.NOT_CONNECTED
        assert jc.is_jev_connected_cached() is False

    async def test_a_deck_without_jev_routes(self, monkeypatch):
        self._fd(monkeypatch, httpx.Response(404, json={"detail": "Not Found"}))
        with pytest.raises(jc.JevError, match="no Jev support yet"):
            await jc.ask_via_flight_deck("text", _Q)

    async def test_upstream_errors_keep_status_and_retry_after(self, monkeypatch):
        self._fd(monkeypatch, httpx.Response(429, json={"detail": "Jev rate limit reached (429)"},
                                             headers={"Retry-After": "5"}))
        jc._mark(True)
        with pytest.raises(jc.JevError) as exc:
            await jc.ask_via_flight_deck("text", _Q)
        assert exc.value.status == 429 and exc.value.retry_after == 5.0
        assert "rate limit" in str(exc.value)
        assert jc.is_jev_connected_cached() is True  # a 429 says nothing about the key

    async def test_refused_agent_hides_the_tool(self, monkeypatch):
        self._fd(monkeypatch, httpx.Response(403, json={"detail": "Unknown agent"}))
        jc._mark(True)
        with pytest.raises(jc.JevError, match="Unknown agent"):
            await jc.ask_via_flight_deck("text", _Q)
        assert jc.is_jev_connected_cached() is False

    async def test_unreachable_flight_deck(self, monkeypatch):
        self._fd(monkeypatch, httpx.ConnectError("down"))
        with pytest.raises(jc.JevError) as exc:
            await jc.ask_via_flight_deck("text", _Q)
        assert exc.value.status == 0 and "Could not reach Flight Deck" in str(exc.value)

    async def test_models_via_flight_deck(self, monkeypatch):
        server = self._fd(monkeypatch, httpx.Response(200, json={"models": [{"name": "jev-1"}, 3]}))
        assert await jc.models_via_flight_deck() == [{"name": "jev-1"}]
        assert server.requests[0].method == "GET"
        assert str(server.requests[0].url) == f"{FD}/fd/jev/agent/models"


class TestForgivingShapes:
    def test_one_bare_question_is_a_list_of_one(self):
        q = jc.normalize_questions({"name": "urgent", "type": "noul", "instructions": "Urgent?"})
        assert list(q) == ["urgent"] and q["urgent"]["type"] == "noul"

    def test_one_bare_question_as_json_text(self):
        q = jc.normalize_questions('{"type": "noul", "instructions": "Urgent?"}')
        assert list(q) == ["q1"]

    def test_a_questions_wrapper_is_unwrapped(self):
        q = jc.normalize_questions({"questions": [{"name": "a", "type": "noul", "instructions": "A?"}]})
        assert list(q) == ["a"]

    def test_a_real_map_with_a_question_named_type_still_works(self):
        q = jc.normalize_questions({"type": {"type": "choice", "instructions": "Kind?",
                                             "criteria": {"x": None, "y": None}}})
        assert list(q) == ["type"] and q["type"]["type"] == "choice"

    def test_a_map_of_non_objects_says_what_a_question_is(self):
        with pytest.raises(jc.JevInputError, match="must be an object with type and instructions"):
            jc.normalize_questions({"urgent": "Is it urgent?"})


class TestImpossibleKeys:
    @pytest.mark.parametrize("key", ["jev_abc\u200bdef", "\u201cjev_abcdef\u201d", "jev abc"])
    async def test_never_reaches_the_network(self, key):
        seen = []

        def handler(req):
            seen.append(req)
            return httpx.Response(200, json={"models": []})

        api = jc.JevAPI(key, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(jc.JevError) as exc:
            await api.models()
        assert exc.value.status == 401 and "copy it again" in str(exc.value)
        assert seen == []
