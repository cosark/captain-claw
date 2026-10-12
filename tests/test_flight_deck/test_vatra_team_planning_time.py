"""Vatra team creation may take 30 minutes, and Vatra workers run without the
scale micro-loop (with a large iteration budget instead).

- The Lead's plan is bounded at 1800s (was 300s), and the SAME limit reaches the
  provider as the call's request timeout — otherwise LiteLLM's 180s / Ollama's
  120s per attempt would still cut a long plan.
- Every spawned Vatra worker gets CLAW_NO_SCALE and CLAW_MAX_ITERATIONS.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from captain_claw.flight_deck import basna_routes as b
from captain_claw.flight_deck import vatra_routes as vr

# ── the Lead's time limit ────────────────────────────────────────────


def test_team_creation_gets_thirty_minutes():
    assert vr._DECOMPOSE_TIMEOUT == 1800
    assert vr._decompose_timeout(None) == 1800
    assert vr._decompose_timeout(600) == 1800          # the run's 600s doesn't shrink it
    assert vr._decompose_timeout(3600) == 3600         # a longer run budget still wins
    assert "1800s" in vr._lead_error_msg(TimeoutError())


def test_provider_call_hands_the_limit_to_the_provider():
    creds = {"provider": "openai", "model": "gpt-4.1", "api_key": "k"}
    prov, _ = b._provider_call(creds, temperature=0.2, default_max=8192, cap=16384,
                               request_timeout=1800)
    assert prov.request_timeout == 1800.0
    kwargs = prov._request_kwargs(messages=[], max_tokens=10)
    assert kwargs["timeout"] == 1800.0


def test_other_calls_keep_the_provider_default():
    creds = {"provider": "openai", "model": "gpt-4.1", "api_key": "k"}
    prov, _ = b._provider_call(creds, temperature=0.2, default_max=256, cap=512)
    assert prov.request_timeout is None
    assert prov._request_kwargs(messages=[], max_tokens=10)["timeout"] == 180


def test_request_timeout_is_set_before_cost_wrapping(monkeypatch):
    token = b._run_sid.set("run-1")
    try:
        creds = {"provider": "openai", "model": "gpt-4.1", "api_key": "k"}
        prov, _ = b._provider_call(creds, temperature=0.2, default_max=256, cap=512,
                                   request_timeout=1800)
    finally:
        b._run_sid.reset(token)
    assert isinstance(prov, b._UsageRecordingProvider)
    assert prov._inner.request_timeout == 1800.0       # the real provider has it


async def test_ollama_passes_the_request_timeout_per_call(monkeypatch):
    from captain_claw.llm import Message, OllamaProvider

    prov = OllamaProvider(model="qwen3:8b", base_url="http://127.0.0.1:1")
    prov.request_timeout = 1800.0
    seen: dict = {}

    async def fake_post(url, json=None, headers=None, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(prov.client, "post", fake_post)
    with pytest.raises(Exception):
        await prov.complete([Message(role="user", content="hi")])
    assert seen.get("timeout") == 1800.0


async def test_build_plan_passes_the_limit_to_the_lead(monkeypatch):
    seen: dict = {}

    async def fake_decompose(*a, request_timeout=None, **k):
        seen["request_timeout"] = request_timeout
        return {"subtasks": [], "domain": "general", "rationale": ""}

    async def no_archetypes(db, uid):
        return []

    db = SimpleNamespace(get_archetype_reliability=lambda uid: _async([]))
    monkeypatch.setattr(vr, "_llm_decompose", fake_decompose)
    monkeypatch.setattr(vr, "merged_archetypes", no_archetypes)
    try:
        await vr._build_plan(db, "u1", "task", 3, {}, timeout=1800.0)
    except Exception:
        pass  # shaping an empty plan may fail later; the Lead call is what matters
    assert seen["request_timeout"] == 1800.0


async def _async(value):
    return value


# ── Vatra workers: no scale loop, big iteration budget ──────────────


@pytest.fixture()
def spawn_capture(monkeypatch):
    from captain_claw.flight_deck import server as srv
    captured: dict = {}

    async def fake_spawn(cfg, request, user):
        captured["cfg"] = cfg
        return SimpleNamespace(ok=True, slug="w1", message="")

    monkeypatch.setattr(srv, "spawn_process", fake_spawn)
    monkeypatch.setattr(srv, "_load_process_registry",
                        lambda: {"w1": {"web_port": 1, "web_auth": "t"}})
    return captured


def _env(cfg) -> dict:
    return {e["key"]: e["value"] for e in cfg.env_vars}


async def test_workers_spawn_without_the_scale_loop(spawn_capture):
    await vr._spawn_worker(object(), {"id": "u1"}, name="w", description="d",
                           cognitive_mode="neutra", tools=["read"], tier="fast",
                           tiers={}, api_key="", env_vars=[])
    env = _env(spawn_capture["cfg"])
    assert env["CLAW_NO_SCALE"] == "1"
    assert env["CLAW_MAX_ITERATIONS"] == str(vr._WORKER_MAX_ITERATIONS)
    assert vr._WORKER_MAX_ITERATIONS >= 200


async def test_an_explicit_env_value_is_kept_and_not_duplicated(spawn_capture):
    await vr._spawn_worker(object(), {"id": "u1"}, name="w", description="d",
                           cognitive_mode="neutra", tools=["read"], tier="fast",
                           tiers={}, api_key="", env_vars=[{"key": "CLAW_MAX_ITERATIONS", "value": "77"}],
                           extra_env=[{"key": "CLAW_NO_SCALE", "value": "1"}])
    keys = [e["key"] for e in spawn_capture["cfg"].env_vars]
    assert keys.count("CLAW_NO_SCALE") == 1 and keys.count("CLAW_MAX_ITERATIONS") == 1
    assert _env(spawn_capture["cfg"])["CLAW_MAX_ITERATIONS"] == "77"


@pytest.mark.parametrize("value,expected", [("300", 300), ("", 40), ("junk", 40), ("0", 40), ("-5", 40)])
def test_agent_reads_its_iteration_budget_from_the_env(monkeypatch, value, expected):
    from captain_claw.agent import _env_max_iterations

    monkeypatch.setenv("CLAW_MAX_ITERATIONS", value)
    assert _env_max_iterations(40) == expected


def test_no_scale_marker_turns_the_scale_system_off(monkeypatch):
    from captain_claw.agent_scale_detection_mixin import AgentScaleDetectionMixin

    class _A(AgentScaleDetectionMixin):
        pass

    monkeypatch.setenv("CLAW_NO_SCALE", "1")
    assert _A()._scale_system_disabled() is True


# ── review fixes ─────────────────────────────────────────────────────


def test_stuck_detection_warmup_ignores_a_raised_budget():
    from captain_claw.agent_orchestration_mixin import _stuck_warmup

    assert _stuck_warmup(40, 40) == 20          # unchanged for the default budget
    assert _stuck_warmup(300, 300) == 20        # a 300-iteration worker still stops early when stuck
    assert _stuck_warmup(10, 10) == 5
    assert _stuck_warmup(2, 2) == 2


async def test_antigravity_honours_the_request_timeout(monkeypatch):
    from captain_claw.llm import Message
    from captain_claw.llm import antigravity as ag

    seen: dict = {}

    async def fake_run_cli(argv, *, prompt, timeout):
        seen["argv"], seen["timeout"] = argv, timeout
        return '{"status": "SUCCESS", "response": "ok"}'

    monkeypatch.setattr(ag, "run_cli", fake_run_cli)
    prov = ag.AntigravityCLIProvider(model="gemini-3-pro")
    prov.request_timeout = 1800.0
    try:
        await prov.complete([Message(role="user", content="hi")])
    except Exception:
        pass  # response shaping is not what this checks
    assert "1800s" in seen["argv"]
    assert seen["timeout"] == 1810


async def test_browser_provider_sends_the_job_timeout(monkeypatch):
    from captain_claw.llm import BrowserProvider, Message

    prov = BrowserProvider(model="gemma", base_url="http://127.0.0.1:1")
    prov.request_timeout = 1800.0
    seen: dict = {}

    async def fake_post(url, json=None, headers=None, **kw):
        seen["body"], seen["kw"] = json, kw
        raise RuntimeError("stop")

    monkeypatch.setattr(prov.client, "post", fake_post)
    with pytest.raises(Exception):
        await prov.complete([Message(role="user", content="hi")])
    assert seen["body"]["timeout"] == 1800.0
    assert seen["kw"]["timeout"].read == 1830.0


def test_infer_complete_body_accepts_a_bounded_timeout():
    from pydantic import ValidationError

    from captain_claw.flight_deck.server import InferCompleteBody

    assert InferCompleteBody(messages=[{"role": "user", "content": "x"}]).timeout is None
    assert InferCompleteBody(timeout=1800).timeout == 1800
    with pytest.raises(ValidationError):
        InferCompleteBody(timeout=99_999)


# ── the Lead's output budget ─────────────────────────────────────────


class _FakeLead:
    """A provider double: records max_tokens, answers with a fixed body."""

    def __init__(self, content, finish_reason="stop"):
        self.content, self.finish_reason = content, finish_reason
        self.created: dict = {}
        self.completed: dict = {}

    def factory(self, **kw):
        self.created = kw
        return self

    async def complete(self, messages=None, **kw):
        self.completed = kw
        return SimpleNamespace(content=self.content, finish_reason=self.finish_reason,
                               usage={}, model="bonsai-2")


_ARCH = [{"id": "analyst", "role": "Analyst", "description": "numbers", "tier": "reason"}]
_PLAN = ('{"domain": "research", "rationale": "r", "shared_context": "c", "subtasks": '
         '[{"id": "s1", "title": "t", "owner_archetype_id": "analyst", "brief": "b"}]}')


@pytest.mark.parametrize("output_ctx,expected", [(49_152, 49_152), (200_000, 65_536), (0, 8192)])
async def test_lead_gets_the_tiers_output_limit(monkeypatch, output_ctx, expected):
    import captain_claw.llm as llm

    fake = _FakeLead(_PLAN)
    monkeypatch.setattr(llm, "create_provider", fake.factory)
    creds = {"provider": "openai", "model": "bonsai-2", "output_ctx": output_ctx}
    plan = await vr._llm_decompose("task", _ARCH, {}, creds, 3)
    assert plan["subtasks"]
    assert fake.created["max_tokens"] == expected
    assert fake.completed["max_tokens"] == expected


async def test_a_cut_off_plan_names_the_limit_and_the_reason(monkeypatch):
    from fastapi import HTTPException

    import captain_claw.llm as llm

    fake = _FakeLead('{"subtasks": [{"id": "s1", "brief": "ITDA margin, R&D', finish_reason="length")
    monkeypatch.setattr(llm, "create_provider", fake.factory)
    creds = {"provider": "openai", "model": "bonsai-2", "output_ctx": 49_152}
    with pytest.raises(HTTPException) as exc:
        await vr._llm_decompose("task", _ARCH, {}, creds, 3)
    assert exc.value.status_code == 502
    assert "49152-token output limit" in exc.value.detail
    assert "thinking model" in exc.value.detail
