"""The ``jev`` agent tool and its wiring.

* standalone the tool calls Jev itself with ``tools.jev.api_key`` / JEV_API_KEY
  (no key → NO_KEY_STANDALONE, nothing sent);
* under Flight Deck it asks ``/fd/jev/agent/*`` with the agent's own headers —
  never a speaker grant, never a key — and relays FD's refusal as-is;
* a shared-agent member's turn is refused before any file read or request;
* state / state_file, models, and the injected runtime kwargs;
* the registry hides ``requires_jev`` tools until a key is usable (and from a
  member recognised by session key alone); members' allowlists never list it;
* config: enabled by default and always re-injected, env overlay
  (JEV_API_KEY, then TYPESAFE_API_KEY), registration, prompt one-liners, eco
  routing, the thinking label, the secret settings field.

No network: Jev and Flight Deck answer through httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from pathlib import Path

import httpx
import pytest

import captain_claw.agent_context_mixin as acm
from captain_claw import jev_client
from captain_claw.config import Config, ToolsConfig
from captain_claw.tools.jev import JevTool
from captain_claw.tools.registry import ToolRegistry

_RealAsyncClient = httpx.AsyncClient  # tests patch httpx.AsyncClient itself
FD = "http://fd.test"
JEV = "https://api.typesafe.ai"
_Q = [
    {"name": "urgent", "type": "noul", "instructions": "Does this need a reply today?"},
    {"name": "topic", "type": "choice", "instructions": "What is it about?",
     "options": [{"name": "billing", "description": "money, invoices"}, {"name": "bug"}]},
]
_ANSWERS = {
    "model": "jev-latest",
    "answers": {
        "urgent": {"type": "noul", "noul": 0.91},
        "topic": {"type": "choice", "choice": "billing", "confidence": 0.8,
                  "probabilities": {"billing": 0.8, "bug": 0.2}},
    },
    "usage": {"input_tokens": 30, "output_tokens": 2},
}


@pytest.fixture(autouse=True)
def _config(monkeypatch, tmp_path):
    import captain_claw.config as config_mod
    from captain_claw.flight_deck import agent_secret
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    for name in ("JEV_API_KEY", "TYPESAFE_API_KEY", "FD_URL", "CLAW_TOOLS__JEV__API_KEY"):
        monkeypatch.delenv(name, raising=False)
    agent_secret.reset_cache_for_tests()
    cfg = Config()
    cfg.web.auth_token = "AGENT-TOKEN"
    monkeypatch.setattr(config_mod, "_config", cfg)
    monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: ""))
    monkeypatch.setattr(GoogleOAuthManager, "_member_call", staticmethod(lambda: False))
    jev_client._CONNECTED = (False, 0.0)
    jev_client._LAST_CHECK_AT = 0.0
    yield cfg
    jev_client._CONNECTED = (False, 0.0)
    jev_client._LAST_CHECK_AT = 0.0
    agent_secret.reset_cache_for_tests()


class _Peer:
    """Jev or Flight Deck: one scripted response, every request recorded."""

    def __init__(self, response):
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        hit = self.response(request) if callable(self.response) else self.response
        return hit

    def body(self, i: int = -1):
        return json.loads(self.requests[i].content or b"null")


def _serve(monkeypatch, response) -> _Peer:
    peer = _Peer(response)
    monkeypatch.setattr(jev_client.httpx, "AsyncClient",
                        lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(peer), **kw))
    return peer


def _under_fd(monkeypatch):
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: FD))


def _member(monkeypatch):
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    monkeypatch.setattr(GoogleOAuthManager, "_member_call", staticmethod(lambda: True))


# The kwargs the registry injects into every execute().
def _runtime(tmp_path: Path) -> dict:
    return {
        "_runtime_base_path": tmp_path,
        "_saved_base_path": tmp_path / "saved",
        "_session_id": "s1",
        "_abort_event": asyncio.Event(),
        "_file_registry": None,
        "_workflow_started_at": None,
        "_workflow_run_dir": None,
        "_stream_callback": None,
        "_approval_callback": None,
    }


# ── standalone ───────────────────────────────────────────────────────


class TestStandalone:
    async def test_key_from_config(self, monkeypatch, tmp_path, _config):
        _config.tools.jev.api_key = "jv-config-key-0001"
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state="Invoice #12 is overdue, pay today", questions=_Q, **_runtime(tmp_path))
        assert res.success, res.error
        assert res.content.startswith("Jev answers:\n- urgent [yes/no]: p(yes) = 0.91 → yes")
        assert "- topic [choice]: billing" in res.content
        req = peer.requests[0]
        assert str(req.url) == f"{JEV}/v1/systemone"
        assert req.headers["Authorization"] == "Bearer jv-config-key-0001"
        body = peer.body()
        assert body["model"] == "jev-latest" and body["state"] == "Invoice #12 is overdue, pay today"
        assert body["questions"]["topic"]["criteria"] == {"billing": "money, invoices", "bug": None}

    async def test_key_from_env(self, monkeypatch, _config):
        monkeypatch.setenv("JEV_API_KEY", "jv-env-key-0002")
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state="text", questions=_Q)
        assert res.success, res.error
        assert peer.requests[0].headers["Authorization"] == "Bearer jv-env-key-0002"

    async def test_typesafe_env_name_works_too(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts-env-key-0003")
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        assert (await JevTool().execute(state="text", questions=_Q)).success
        assert peer.requests[0].headers["Authorization"] == "Bearer ts-env-key-0003"

    async def test_no_key(self, monkeypatch, tmp_path):
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state="text", questions=_Q, **_runtime(tmp_path))
        assert not res.success and res.error == jev_client.NO_KEY_STANDALONE
        res = await JevTool().execute(action="models")
        assert res.error == jev_client.NO_KEY_STANDALONE
        assert peer.requests == []

    async def test_model_default_from_config_and_override(self, monkeypatch, _config):
        _config.tools.jev.api_key = "jv-config-key-0001"
        _config.tools.jev.model = "jev-2"
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        await JevTool().execute(state="text", questions=_Q)
        await JevTool().execute(state="text", questions=_Q, model=" jev-3 ")
        assert [b["model"] for b in (peer.body(0), peer.body(1))] == ["jev-2", "jev-3"]

    async def test_jev_error_is_a_failed_result(self, monkeypatch, _config):
        _config.tools.jev.api_key = "jv-config-key-0001"
        _serve(monkeypatch, httpx.Response(401, json={"detail": "bad"}))
        res = await JevTool().execute(state="text", questions=_Q)
        assert not res.success and "refused the API key" in res.error

    async def test_bad_questions_never_leave(self, monkeypatch, _config):
        _config.tools.jev.api_key = "jv-config-key-0001"
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state="text", questions=[{"name": "s", "type": "score", "instructions": "S?"}])
        assert not res.success and "needs levels" in res.error
        res = await JevTool().execute(state="text")
        assert not res.success and "questions are required" in res.error
        res = await JevTool().execute(questions=_Q)
        assert not res.success and "state is required" in res.error
        assert peer.requests == []

    async def test_questions_as_json_text(self, monkeypatch, _config):
        _config.tools.jev.api_key = "jv-config-key-0001"
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state="text", questions=json.dumps(_Q))
        assert res.success, res.error
        assert set(peer.body()["questions"]) == {"urgent", "topic"}

    async def test_models(self, monkeypatch, _config):
        _config.tools.jev.api_key = "jv-config-key-0001"
        peer = _serve(monkeypatch, httpx.Response(200, json={"models": [
            {"name": "jev-latest", "description": "newest", "release_date": "2026-09-01"}]}))
        res = await JevTool().execute(action="models", state="ignored")
        assert res.success and res.content == "Jev models:\n- jev-latest (2026-09-01) — newest"
        assert peer.requests[0].method == "GET" and str(peer.requests[0].url) == f"{JEV}/v1/models"

    async def test_unknown_action(self, monkeypatch):
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(action="write")
        assert not res.success and "use ask or models" in res.error and peer.requests == []


# ── state_file ───────────────────────────────────────────────────────


class TestStateFile:
    @pytest.fixture(autouse=True)
    def _key(self, _config):
        _config.tools.jev.api_key = "jv-config-key-0001"

    async def test_relative_to_the_runtime_base(self, monkeypatch, tmp_path):
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "mail.txt").write_bytes("Pay today \xff".encode("latin-1"))
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state_file="notes/mail.txt", questions=_Q, **_runtime(tmp_path))
        assert res.success, res.error
        assert peer.body()["state"] == "Pay today �"  # utf-8, errors replaced

    async def test_absolute_json_file_is_sent_structured(self, monkeypatch, tmp_path):
        path = tmp_path / "ticket.json"
        path.write_text(json.dumps({"subject": "refund", "body": "please"}), encoding="utf-8")
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state_file=str(path), questions=_Q, state="  ")
        assert res.success, res.error
        assert peer.body()["state"] == {"subject": "refund", "body": "please"}

    async def test_state_and_state_file_together(self, monkeypatch, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state="text", state_file="a.txt", questions=_Q, **_runtime(tmp_path))
        assert not res.success and "not both" in res.error and peer.requests == []

    async def test_missing_file(self, monkeypatch, tmp_path):
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state_file="nope.txt", questions=_Q, **_runtime(tmp_path))
        assert not res.success and "state_file not found" in res.error and peer.requests == []

    async def test_found_the_way_read_finds_it(self, monkeypatch, tmp_path):
        # write(path="notes.md") lands under saved/ and in the file registry:
        # jev must follow the same mapping as read.
        saved = tmp_path / "saved" / "tmp" / "s1"
        saved.mkdir(parents=True)
        (saved / "notes.md").write_text("Refund please", encoding="utf-8")
        logical = tmp_path / "elsewhere" / "report.md"
        logical.parent.mkdir()
        logical.write_text("Quarterly numbers", encoding="utf-8")

        class _Registry:
            def resolve(self, value):
                return str(logical) if value == "report.md" else None

        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        runtime = {**_runtime(tmp_path), "_saved_base_path": str(saved), "_file_registry": _Registry()}
        res = await JevTool().execute(state_file="notes.md", questions=_Q, **runtime)
        assert res.success, res.error
        assert peer.body()["state"] == "Refund please"
        res = await JevTool().execute(state_file="report.md", questions=_Q, **runtime)
        assert res.success, res.error
        assert peer.body()["state"] == "Quarterly numbers"

    @pytest.mark.parametrize("name,data", [
        ("invoice.pdf", b"%PDF-1.7 looks like text but is not"),
        ("blob.bin", b"abc\x00def" * 10),
        ("random.dat", bytes(range(128, 256)) * 40),
    ])
    async def test_binary_files_are_refused(self, monkeypatch, tmp_path, name, data):
        (tmp_path / name).write_bytes(data)
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state_file=name, questions=_Q, **_runtime(tmp_path))
        assert not res.success and "pdf_extract" in res.error and peer.requests == []

    async def test_oversized_file_is_refused_before_reading(self, monkeypatch, tmp_path):
        big = tmp_path / "big.txt"
        with big.open("wb") as fh:
            fh.truncate(jev_client.MAX_STATE_CHARS * 4 + 1)
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        res = await JevTool().execute(state_file=str(big), questions=_Q)
        assert not res.success and "Send only the part" in res.error and peer.requests == []


# ── under Flight Deck ────────────────────────────────────────────────


class TestFlightDeck:
    async def test_asks_flight_deck_as_the_owner_agent(self, monkeypatch, tmp_path, _config):
        _config.tools.jev.api_key = "jv-standalone-never-used"
        _under_fd(monkeypatch)
        fd_answer = {**_ANSWERS, "values": {"urgent": 0.91, "topic": "billing"}, "summary": "…", "latency_ms": 80}
        peer = _serve(monkeypatch, httpx.Response(200, json=fd_answer))
        res = await JevTool().execute(state="text", questions=_Q, **_runtime(tmp_path))
        assert res.success, res.error
        assert res.content.startswith("Jev answers:\n- urgent [yes/no]: p(yes) = 0.91")
        req = peer.requests[0]
        assert req.method == "POST" and str(req.url) == f"{FD}/fd/jev/agent/ask"
        assert req.headers["X-Agent-Auth"] == "AGENT-TOKEN"
        assert "X-FD-Speaker-Grant" not in req.headers
        assert "Authorization" not in req.headers  # never a Jev key from the agent
        assert "jv-standalone-never-used" not in req.content.decode()
        body = peer.body()
        assert body["model"] == "jev-latest" and body["state"] == "text"
        assert set(body["questions"]) == {"urgent", "topic"}
        assert jev_client.is_jev_connected_cached()

    async def test_flight_deck_404_detail_is_relayed(self, monkeypatch):
        _under_fd(monkeypatch)
        _serve(monkeypatch, httpx.Response(404, json={"detail": jev_client.NOT_CONNECTED}))
        res = await JevTool().execute(state="text", questions=_Q)
        assert not res.success and res.error == jev_client.NOT_CONNECTED

    async def test_no_standalone_key_needed_under_flight_deck(self, monkeypatch):
        _under_fd(monkeypatch)
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        assert (await JevTool().execute(state="text", questions=_Q)).success
        assert len(peer.requests) == 1

    async def test_member_turn_refused_before_any_request_or_read(self, monkeypatch, tmp_path):
        _under_fd(monkeypatch)
        _member(monkeypatch)
        peer = _serve(monkeypatch, httpx.Response(200, json=_ANSWERS))
        read = []
        monkeypatch.setattr("captain_claw.tools.jev._read_state_file", lambda *a: read.append(a) or "x")
        for kwargs in ({"state": "text", "questions": _Q},
                       {"state_file": "secret.txt", "questions": _Q},
                       {"action": "models"}):
            res = await JevTool().execute(**kwargs, **_runtime(tmp_path))
            assert not res.success and res.error == jev_client.MEMBER_REFUSED
        assert peer.requests == [] and read == []

    async def test_models_through_flight_deck(self, monkeypatch):
        _under_fd(monkeypatch)
        peer = _serve(monkeypatch, httpx.Response(200, json={"models": [{"name": "jev-latest"}]}))
        res = await JevTool().execute(action="models")
        assert res.success and "- jev-latest" in res.content
        assert peer.requests[0].method == "GET" and str(peer.requests[0].url) == f"{FD}/fd/jev/agent/models"


# ── schema ───────────────────────────────────────────────────────────


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_schema_is_provider_friendly():
    tool = JevTool()
    props = tool.parameters["properties"]
    assert set(props) == {"action", "state", "state_file", "questions", "model"}
    assert "timeout" not in props  # the registry reads that as the call timeout
    assert tool.timeout_seconds == 150.0
    for node in _walk(tool.parameters):
        if isinstance(node, dict):
            assert "additionalProperties" not in node
            assert not isinstance(node.get("type"), list)
    item = props["questions"]["items"]["properties"]
    assert item["type"]["enum"] == ["noul", "choice", "score"]
    assert {"options", "levels", "yes_means", "no_means"} <= set(item)
    assert "not for math" in tool.description.lower() or "Not for math" in tool.description


# ── registry gate ────────────────────────────────────────────────────


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(JevTool(), metadata={"requires_jev": True})
    return reg


def _names(reg: ToolRegistry, **kw) -> list[str]:
    return [t.name for t in reg._resolve_tools(**kw)]


class TestRegistryGate:
    def test_standalone_needs_a_key(self, _config):
        reg = _registry()
        assert "jev" not in _names(reg)
        _config.tools.jev.api_key = "jv-config-key-0001"
        assert "jev" in _names(reg)

    def test_standalone_env_key(self, monkeypatch):
        reg = _registry()
        monkeypatch.setenv("JEV_API_KEY", "jv-env-key-0002")
        assert "jev" in _names(reg)

    def test_flight_deck_follows_the_owner_flag(self, monkeypatch, _config):
        _config.tools.jev.api_key = "standalone-key-means-nothing-under-fd"
        _under_fd(monkeypatch)
        reg = _registry()
        assert "jev" not in _names(reg)
        jev_client._mark(True)
        assert "jev" in _names(reg)
        jev_client._CONNECTED = (True, time.time() - 3600)  # stale → hidden again
        assert "jev" not in _names(reg)

    def test_member_with_nothing_bound_never_sees_it(self, monkeypatch):
        _under_fd(monkeypatch)
        jev_client._mark(True)
        reg = _registry()
        reg.register_speaker_session("member-session")
        assert "jev" not in _names(reg, session_id="member-session")
        assert "jev" in _names(reg, session_id="owner-session")

    def test_gate_fails_closed(self, monkeypatch, _config):
        _config.tools.jev.api_key = "jv-config-key-0001"

        def _boom():
            raise RuntimeError("broken")

        monkeypatch.setattr(jev_client, "jev_usable", _boom)
        assert "jev" not in _names(_registry())

    def test_members_never_get_jev(self):
        from captain_claw import speaker

        assert "jev" not in speaker.SPEAKER_TOOL_ALLOWLIST
        assert "jev" not in speaker.SPEAKER_TOOL_ALLOWLIST_MAX


# ── config, registration, prompts, routing ───────────────────────────


class TestConfig:
    def test_enabled_by_default_and_always(self):
        assert "jev" in ToolsConfig().enabled
        assert "jev" in ToolsConfig()._ALWAYS_ENABLED
        assert "jev" in ToolsConfig(enabled=["read"]).enabled  # re-injected

    def test_defaults(self):
        cfg = ToolsConfig().jev
        assert cfg.api_key == "" and cfg.model == jev_client.DEFAULT_MODEL
        assert cfg.base_url == jev_client.DEFAULT_BASE_URL and cfg.timeout == 30.0

    @pytest.fixture
    def _load_here(self, monkeypatch, tmp_path):
        import captain_claw.config as config_mod

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "home" / "config.yaml")
        (tmp_path / "config.yaml").write_text("tools:\n  jev:\n    api_key: yaml-key\n", encoding="utf-8")
        return tmp_path

    def test_yaml_key_without_env(self, _load_here):
        assert Config.load().tools.jev.api_key == "yaml-key"

    def test_env_overlay_prefers_jev_api_key(self, _load_here, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-env-key")
        assert Config.load().tools.jev.api_key == "typesafe-env-key"
        monkeypatch.setenv("JEV_API_KEY", "jev-env-key")
        assert Config.load().tools.jev.api_key == "jev-env-key"

    def test_env_overlay_from_dotenv(self, _load_here):
        (_load_here / ".env").write_text("TYPESAFE_API_KEY=dotenv-key\n", encoding="utf-8")
        assert Config.load().tools.jev.api_key == "dotenv-key"

    def test_claw_prefixed_env_wins(self, _load_here, monkeypatch):
        monkeypatch.setenv("JEV_API_KEY", "jev-env-key")
        monkeypatch.setenv("CLAW_TOOLS__JEV__API_KEY", "claw-env-key")
        assert Config.load().tools.jev.api_key == "claw-env-key"

    def test_key_is_a_secret_setting(self):
        from captain_claw.web.rest_settings import _SECRET_FIELDS

        assert "tools.jev.api_key" in _SECRET_FIELDS


class _Recorder:
    """Stands in for the agent: records what _register_default_tools registers."""

    def __init__(self):
        self.tools = ToolRegistry()

    def _register_plugin_tools(self):
        pass


class TestRegistration:
    def test_registered_with_the_gate(self, monkeypatch):
        cfg = Config()
        cfg.tools.enabled = ["read", "jev"]  # bypass validation: just these
        monkeypatch.setattr(acm, "get_config", lambda: cfg)
        agent = _Recorder()
        acm.AgentContextMixin._register_default_tools(agent)
        assert agent.tools.has_tool("jev")
        assert isinstance(agent.tools.get("jev"), JevTool)
        assert agent.tools.get_tool_metadata("jev") == {"requires_jev": True}

    def test_builtin_map_and_exports(self):
        import captain_claw.tools as tools

        assert acm.AgentContextMixin._BUILTIN_TOOL_MAP["jev"] == ["jev"]
        assert tools.JevTool is JevTool and "JevTool" in tools.__all__

    @pytest.mark.parametrize("descs", [acm._TOOL_PROMPT_DESCRIPTIONS, acm._TOOL_PROMPT_DESCRIPTIONS_MICRO])
    def test_prompt_one_liners(self, descs):
        assert "jev" in descs and "math" in descs["jev"]

    def test_connection_refreshed_where_microsofts_is(self):
        from captain_claw import agent_orchestration_mixin
        from captain_claw.web import ws_handler

        for module in (acm, agent_orchestration_mixin, ws_handler):
            src = inspect.getsource(module)
            assert "jev_client.refresh_connected()" in src, module.__name__
            assert src.index("MicrosoftOAuthManager().is_connected()") < src.index(
                "jev_client.refresh_connected()"), module.__name__


@pytest.mark.parametrize("message,hit", [
    ("ask jev whether these tickets are urgent", True),
    ("Use TypeSafe to classify this", True),
    ("check with Jev.", True),
    ("call jevtic about the invoice", False),
])
def test_eco_intent_routing(message, hit):
    from captain_claw.agent_orchestration_mixin import _eco_select_tools_by_intent

    assert ("jev" in _eco_select_tools_by_intent(message)) is hit


@pytest.mark.parametrize("args,label", [
    ({"questions": [{}, {}]}, "Asking Jev (2 questions)"),
    ({"questions": [{}]}, "Asking Jev (1 question)"),
    ({"questions": json.dumps({"a": {}, "b": {}, "c": {}})}, "Asking Jev (3 questions)"),
    ({"questions": "not json"}, "Asking Jev"),
    ({}, "Asking Jev"),
    ({"action": "models"}, "Listing Jev models"),
])
def test_thinking_label(args, label):
    from captain_claw.agent_tool_loop_mixin import AgentToolLoopMixin

    assert AgentToolLoopMixin._tool_thinking_summary("jev", args) == label


# ── Jev-first prompt policy ──────────────────────────────────────────

_INSTRUCTIONS = Path(acm.__file__).resolve().parent / "instructions"



def _prompt_agent(monkeypatch, tmp_path, micro):
    from captain_claw.agent import Agent
    from captain_claw.instructions import InstructionLoader
    from captain_claw.llm import LLMProvider, LLMResponse
    from captain_claw.session import Session

    class _Provider(LLMProvider):
        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            return LLMResponse(content="ok")

        async def complete_streaming(self, messages, tools=None, temperature=None, max_tokens=None):
            if False:
                yield ""

        def count_tokens(self, text: str) -> int:
            return len(text.split()) or 1

    # A fresh global registry: the agent's tools are only what the test adds.
    monkeypatch.setattr("captain_claw.tools.registry._registry", None)
    agent = Agent(provider=_Provider())
    agent.session = Session(id="s1", name="jev-first")
    agent.instructions = InstructionLoader(
        base_dir=_INSTRUCTIONS, personal_dir=tmp_path, use_micro=micro, use_nano=False,
    )
    return agent


@pytest.mark.parametrize("micro", [False, True])
def test_templates_carry_the_jev_block(tmp_path, micro):
    from captain_claw.instructions import InstructionLoader

    for name in ("system_prompt.md", "micro_system_prompt.md"):
        assert "{jev_block}" in (_INSTRUCTIONS / name).read_text(encoding="utf-8"), name
    loader = InstructionLoader(base_dir=_INSTRUCTIONS, personal_dir=tmp_path, use_micro=micro, use_nano=False)
    assert "<<JEV>>" in loader.render("system_prompt.md", jev_block="<<JEV>>")
    section = loader.load("section_jev.md")  # micro resolves micro_section_jev.md
    assert "whenever" in section.lower() and "`jev`" in section


@pytest.mark.parametrize("micro", [False, True])
def test_jev_first_policy_only_while_jev_is_usable(monkeypatch, tmp_path, _config, micro):
    agent = _prompt_agent(monkeypatch, tmp_path, micro)
    marker = "use `jev` whenever" if micro else "Jev-first policy"

    # No jev tool registered → no policy.
    assert marker not in agent._build_system_prompt()

    # Registered (it always is) but no usable key → still no policy: the
    # prompt never pushes a tool the agent can't see.
    agent.tools.register(JevTool(), metadata={"requires_jev": True})
    agent.instructions._cache.clear()
    prompt = agent._build_system_prompt()
    assert marker not in prompt and "{jev_block}" not in prompt

    # A usable key → the policy is in the prompt.
    _config.tools.jev.api_key = "jv-config-key-0001"
    agent.instructions._cache.clear()
    prompt = agent._build_system_prompt()
    assert marker in prompt and "{jev_block}" not in prompt


def test_jev_first_policy_follows_the_flight_deck_connection(monkeypatch, tmp_path, _config):
    _under_fd(monkeypatch)
    agent = _prompt_agent(monkeypatch, tmp_path, False)
    agent.tools.register(JevTool(), metadata={"requires_jev": True})
    assert "Jev-first policy" not in agent._build_system_prompt()
    jev_client._mark(True)
    assert "Jev-first policy" in agent._build_system_prompt()


def test_eco_mode_always_sends_the_jev_schema():
    from captain_claw.agent_orchestration_mixin import _ECO_CORE_TOOLS

    assert "jev" in _ECO_CORE_TOOLS


def test_the_tool_itself_says_to_prefer_it():
    assert "WHENEVER you would otherwise judge content yourself" in JevTool.description
    assert "WHENEVER" in acm._TOOL_PROMPT_DESCRIPTIONS["jev"]
    assert "WHENEVER" in acm._TOOL_PROMPT_DESCRIPTIONS_MICRO["jev"]
