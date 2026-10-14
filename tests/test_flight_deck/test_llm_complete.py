"""POST /fd/llm/complete — one completion on the owner's configured tier.

Resolves the owner's saved Library tier server-side (so a caller need not, and
must not, handle raw creds) and runs a single LLM call. Used for structured
one-shot generations where a multi-agent run would be the wrong tool.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import llm_routes
from captain_claw.flight_deck.auth import get_current_user


class _FakeDB:
    def __init__(self, forge_tiers_json: str = ""):
        self._settings = ({"fd:forge-tiers": forge_tiers_json}
                          if forge_tiers_json else {})

    async def get_all_settings(self, owner_id):
        return self._settings


_TIER_SET = (
    '{"activeSetId": "s1", "sets": [{"id": "s1", "tiers": '
    '{"reason": {"provider": "openai", "model": "deepseek-v4-pro", '
    '"base_url": "https://api.deepseek.com", "api_key": "sk-x"}}, '
    '"envVars": []}]}'
)


def _app(monkeypatch, db, provider_factory=None):
    monkeypatch.setattr(llm_routes, "get_db", lambda: db)
    app = FastAPI()
    app.include_router(llm_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    return app


def test_complete_uses_owner_reason_tier(monkeypatch):
    captured: dict = {}

    class _Resp:
        content = "```json\n{\"ok\": true}\n```"

    class _Provider:
        async def complete(self, messages, temperature, max_tokens):
            captured["messages"] = messages
            return _Resp()

    def _create_provider(**kwargs):
        captured["creds"] = kwargs
        return _Provider()

    import captain_claw.llm as llm
    monkeypatch.setattr(llm, "create_provider", _create_provider)

    app = _app(monkeypatch, _FakeDB(_TIER_SET))
    client = TestClient(app)
    r = client.post("/fd/llm/complete",
                    json={"prompt": "make a manifest", "system": "you configure",
                          "tier": "reason"})
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "deepseek-v4-pro"
    assert body["provider"] == "openai"
    assert "ok" in body["content"]
    # It resolved the owner's saved reason tier — not a hardcoded default.
    assert captured["creds"]["provider"] == "openai"
    assert captured["creds"]["model"] == "deepseek-v4-pro"
    assert captured["creds"]["base_url"] == "https://api.deepseek.com"
    # system + user messages, in order.
    roles = [m.role for m in captured["messages"]]
    assert roles == ["system", "user"]


def test_complete_400_when_tier_has_no_model(monkeypatch):
    # No saved tiers and (by monkeypatch) an empty registry tier → no model.
    from captain_claw.flight_deck import basna_routes
    monkeypatch.setattr(basna_routes, "_load_registry",
                        lambda: {"tiers": {}}, raising=False)
    app = _app(monkeypatch, _FakeDB(""))
    client = TestClient(app)
    r = client.post("/fd/llm/complete", json={"prompt": "x", "tier": "reason"})
    assert r.status_code == 400
    assert "no model" in r.json()["detail"]


def test_complete_502_surfaces_llm_error(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("missing Anthropic API key")

    import captain_claw.llm as llm
    monkeypatch.setattr(llm, "create_provider", _boom)
    app = _app(monkeypatch, _FakeDB(_TIER_SET))
    client = TestClient(app)
    r = client.post("/fd/llm/complete", json={"prompt": "x", "tier": "reason"})
    assert r.status_code == 502
    assert "missing Anthropic API key" in r.json()["detail"]


# ── complete_for_owner: the route without HTTP ───────────────────────


def _tier_set(**tiers: str) -> str:
    """A saved Library tier set: tier name → model, each on its own key."""
    import json
    return json.dumps({"activeSetId": "s1", "sets": [{"id": "s1", "envVars": [], "tiers": {
        name: {"provider": "openai", "model": model, "base_url": "https://llm.example",
               "api_key": f"sk-{name}"}
        for name, model in tiers.items()}}]})


class _FakeLLM:
    """``create_provider`` stand-in: answers per model — a reply, an exception
    to raise, or ``"hang"`` — and records every provider made and call run."""

    def __init__(self, monkeypatch, behaviour: dict | None = None, *,
                 finish_reason: str | None = "stop"):
        self.behaviour = behaviour or {}
        self.finish_reason = finish_reason
        self.created: list[dict] = []
        self.calls: list[dict] = []
        import captain_claw.llm as llm
        monkeypatch.setattr(llm, "create_provider", self._create)

    def _create(self, **kwargs):
        self.created.append(kwargs)
        fake = self

        class _Provider:
            async def complete(self, messages, temperature, max_tokens):
                fake.calls.append({"model": kwargs["model"], "messages": messages,
                                   "temperature": temperature, "max_tokens": max_tokens})
                how = fake.behaviour.get(kwargs["model"], "  {\"ok\": true}\n")
                if isinstance(how, Exception):
                    raise how
                if how == "hang":
                    await asyncio.sleep(30)
                resp = SimpleNamespace(content=how, usage={"prompt_tokens": 11,
                                                           "completion_tokens": 5})
                if fake.finish_reason is not None:
                    resp.finish_reason = fake.finish_reason
                return resp

        return _Provider()

    @property
    def models(self) -> list[str]:
        return [c["model"] for c in self.calls]


async def test_complete_for_owner_returns_content_finish_provider_model(monkeypatch):
    fake = _FakeLLM(monkeypatch)
    usage: dict = {}
    out = await llm_routes.complete_for_owner(
        _FakeDB(_TIER_SET), "u1", "reason", "you sort mail", "the email",
        max_tokens=200, temperature=0, usage_out=usage)
    assert out == ('{"ok": true}', "stop", "openai", "deepseek-v4-pro")
    content, finish_reason, provider, model = out
    assert (out.content, out.finish_reason, out.provider, out.model) == (
        content, finish_reason, provider, model)
    assert usage == {"prompt_tokens": 11, "completion_tokens": 5}
    # The owner's saved tier, with the caller's sampling settings.
    made = fake.created[0]
    assert (made["provider"], made["model"], made["base_url"], made["api_key"]) == (
        "openai", "deepseek-v4-pro", "https://api.deepseek.com", "sk-x")
    assert made["temperature"] == 0 and made["max_tokens"] == 200
    call = fake.calls[0]
    assert (call["temperature"], call["max_tokens"]) == (0, 200)
    assert [(m.role, m.content) for m in call["messages"]] == [
        ("system", "you sort mail"), ("user", "the email")]


async def test_complete_for_owner_reads_finish_reason_leniently(monkeypatch):
    # A provider (or fake) that reports no finish reason gives "" — never raises.
    fake = _FakeLLM(monkeypatch, finish_reason=None)
    out = await llm_routes.complete_for_owner(
        _FakeDB(_TIER_SET), "u1", "reason", "", "x", max_tokens=64, temperature=0.7)
    assert out.finish_reason == ""
    # An empty system prompt sends no system message.
    assert [m.role for m in fake.calls[0]["messages"]] == ["user"]


async def test_complete_for_owner_reports_truncation_without_judging_it(monkeypatch):
    _FakeLLM(monkeypatch, finish_reason="length")
    out = await llm_routes.complete_for_owner(
        _FakeDB(_TIER_SET), "u1", "reason", "s", "u", max_tokens=64, temperature=0)
    assert out.finish_reason == "length"


async def test_complete_for_owner_errors_carry_the_route_status(monkeypatch):
    from captain_claw.flight_deck import basna_routes
    # No model for the tier → 400, before any provider is made.
    fake = _FakeLLM(monkeypatch)
    monkeypatch.setattr(basna_routes, "_load_registry", lambda: {"tiers": {}})
    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_for_owner(
            _FakeDB(""), "u1", "reason", "", "x", max_tokens=64, temperature=0)
    assert ei.value.status == 400
    assert ei.value.detail == "no model configured for the 'reason' tier"
    assert fake.created == []

    # An LLM error → 502 with the real message.
    monkeypatch.undo()
    _FakeLLM(monkeypatch, {"deepseek-v4-pro": RuntimeError("quota exceeded")})
    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_for_owner(
            _FakeDB(_TIER_SET), "u1", "reason", "", "x", max_tokens=64, temperature=0)
    assert ei.value.status == 502
    assert ei.value.detail == "LLM call failed: quota exceeded"


async def test_complete_for_owner_timeout_is_504(monkeypatch):
    _FakeLLM(monkeypatch, {"deepseek-v4-pro": "hang"})
    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_for_owner(
            _FakeDB(_TIER_SET), "u1", "reason", "", "x", max_tokens=64,
            temperature=0, timeout=0.05)
    assert ei.value.status == 504
    assert "timed out" in ei.value.detail


async def test_complete_for_owner_missing_registry_is_500(monkeypatch):
    from fastapi import HTTPException

    from captain_claw.flight_deck import basna_routes

    def _missing():
        raise HTTPException(500, "Archetype registry not found")

    monkeypatch.setattr(basna_routes, "_load_registry", _missing)
    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_for_owner(
            _FakeDB(_TIER_SET), "u1", "reason", "", "x", max_tokens=64, temperature=0)
    assert (ei.value.status, ei.value.detail) == (500, "Archetype registry not found")


# ── the route delegates, with its response unchanged ────────────────


def test_route_delegates_and_keeps_its_response_shape(monkeypatch):
    seen: dict = {}

    async def _service(db, owner, tier, system, user, *, max_tokens, temperature):
        seen.update(db=db, owner=owner, tier=tier, system=system, user=user,
                    max_tokens=max_tokens, temperature=temperature)
        return llm_routes.OwnerCompletion("hello", "length", "openai", "m1")

    monkeypatch.setattr(llm_routes, "complete_for_owner", _service)
    db = _FakeDB(_TIER_SET)
    client = TestClient(_app(monkeypatch, db))
    r = client.post("/fd/llm/complete",
                    json={"prompt": "p", "system": "s", "tier": "not-a-tier",
                          "max_tokens": 100, "temperature": 0.2})
    assert r.status_code == 200
    # Exactly the old keys — finish_reason stays internal.
    assert r.json() == {"content": "hello", "provider": "openai", "model": "m1"}
    # An unknown tier still means "balanced"; the route's db and the session user.
    assert seen == {"db": db, "owner": "u1", "tier": "balanced", "system": "s",
                    "user": "p", "max_tokens": 100, "temperature": 0.2}


def test_route_turns_service_errors_into_the_same_statuses(monkeypatch):
    from captain_claw.flight_deck import basna_routes

    def _missing():
        from fastapi import HTTPException
        raise HTTPException(500, "Archetype registry not found")

    monkeypatch.setattr(basna_routes, "_load_registry", _missing)
    client = TestClient(_app(monkeypatch, _FakeDB(_TIER_SET)))
    r = client.post("/fd/llm/complete", json={"prompt": "x", "tier": "reason"})
    assert r.status_code == 500
    assert r.json() == {"detail": "Archetype registry not found"}

    monkeypatch.undo()
    _FakeLLM(monkeypatch, {"deepseek-v4-pro": RuntimeError("boom")})
    client = TestClient(_app(monkeypatch, _FakeDB(_TIER_SET)))
    r = client.post("/fd/llm/complete", json={"prompt": "x", "tier": "reason"})
    assert r.status_code == 502
    assert r.json() == {"detail": "LLM call failed: boom"}


# ── unattended decisions: reason / balanced only, with fallback ─────


def test_fallback_orders_never_name_a_weak_tier():
    assert llm_routes.CLASSIFIER_TIER_ORDER == ("balanced", "reason")
    assert llm_routes.JUDGE_TIER_ORDER == ("reason", "balanced")
    assert llm_routes.UNATTENDED_TIERS == {"reason", "balanced"}


async def test_owner_tier_model(monkeypatch):
    from captain_claw.flight_deck import basna_routes
    monkeypatch.setattr(basna_routes, "_load_registry", lambda: {"tiers": {}})
    db = _FakeDB(_tier_set(reason="r-model"))
    assert await llm_routes.owner_tier_model(db, "u1", "reason") == ("openai", "r-model")
    assert await llm_routes.owner_tier_model(db, "u1", "balanced") is None


async def test_owner_tier_model_never_raises(monkeypatch):
    from captain_claw.flight_deck import basna_routes

    async def _broken(db, owner):
        raise RuntimeError("db gone")

    monkeypatch.setattr(basna_routes, "_load_owner_tiers", _broken)
    assert await llm_routes.owner_tier_model(_FakeDB(), "u1", "reason") is None
    assert await llm_routes.first_owner_tier(
        _FakeDB(), "u1", llm_routes.JUDGE_TIER_ORDER) is None


async def test_first_owner_tier_follows_the_order(monkeypatch):
    from captain_claw.flight_deck import basna_routes
    monkeypatch.setattr(basna_routes, "_load_registry", lambda: {"tiers": {}})
    both = _FakeDB(_tier_set(reason="r-model", balanced="b-model"))
    assert await llm_routes.first_owner_tier(
        both, "u1", llm_routes.CLASSIFIER_TIER_ORDER) == ("balanced", "openai", "b-model")
    assert await llm_routes.first_owner_tier(
        both, "u1", llm_routes.JUDGE_TIER_ORDER) == ("reason", "openai", "r-model")
    # A tier with no model is passed over.
    only_reason = _FakeDB(_tier_set(reason="r-model"))
    assert await llm_routes.first_owner_tier(
        only_reason, "u1", llm_routes.CLASSIFIER_TIER_ORDER) == ("reason", "openai", "r-model")
    assert await llm_routes.first_owner_tier(
        _FakeDB(""), "u1", llm_routes.CLASSIFIER_TIER_ORDER) is None


async def test_first_owner_tier_never_picks_micro_or_fast(monkeypatch):
    from captain_claw.flight_deck import basna_routes
    monkeypatch.setattr(basna_routes, "_load_registry", lambda: {"tiers": {}})
    weak = _FakeDB(_tier_set(micro="qwen-4b", fast="haiku", longctx="long"))
    order = ("micro", "fast", "longctx", "balanced", "reason")
    assert await llm_routes.first_owner_tier(weak, "u1", order) is None
    mixed = _FakeDB(_tier_set(micro="qwen-4b", fast="haiku", reason="r-model"))
    assert await llm_routes.first_owner_tier(mixed, "u1", order) == (
        "reason", "openai", "r-model")


async def test_complete_with_fallback_uses_the_first_tier(monkeypatch):
    fake = _FakeLLM(monkeypatch)
    db = _FakeDB(_tier_set(reason="r-model", balanced="b-model"))
    usage: dict = {}
    out = await llm_routes.complete_with_fallback(
        db, "u1", llm_routes.JUDGE_TIER_ORDER, "judge", "reply",
        max_tokens=300, temperature=0, usage_out=usage)
    assert out == ('{"ok": true}', "stop", "openai", "r-model")
    assert fake.models == ["r-model"]
    assert usage["completion_tokens"] == 5


async def test_complete_with_fallback_moves_on_when_a_tier_fails(monkeypatch):
    fake = _FakeLLM(monkeypatch, {"b-model": RuntimeError("provider down")})
    db = _FakeDB(_tier_set(reason="r-model", balanced="b-model"))
    out = await llm_routes.complete_with_fallback(
        db, "u1", llm_routes.CLASSIFIER_TIER_ORDER, "sort", "mail",
        max_tokens=200, temperature=0)
    assert out.model == "r-model"
    assert fake.models == ["b-model", "r-model"]


async def test_complete_with_fallback_timeout_applies_per_attempt(monkeypatch):
    fake = _FakeLLM(monkeypatch, {"r-model": "hang"})
    db = _FakeDB(_tier_set(reason="r-model", balanced="b-model"))
    out = await llm_routes.complete_with_fallback(
        db, "u1", llm_routes.JUDGE_TIER_ORDER, "judge", "reply",
        max_tokens=300, temperature=0, timeout=0.05)
    assert out.model == "b-model"
    assert fake.models == ["r-model", "b-model"]


async def test_complete_with_fallback_skips_a_tier_without_a_model(monkeypatch):
    from captain_claw.flight_deck import basna_routes
    monkeypatch.setattr(basna_routes, "_load_registry", lambda: {"tiers": {}})
    fake = _FakeLLM(monkeypatch)
    out = await llm_routes.complete_with_fallback(
        _FakeDB(_tier_set(reason="r-model")), "u1", llm_routes.CLASSIFIER_TIER_ORDER,
        "sort", "mail", max_tokens=200, temperature=0)
    assert out.model == "r-model"
    assert fake.models == ["r-model"]


async def test_complete_with_fallback_never_runs_micro_or_fast(monkeypatch):
    from captain_claw.flight_deck import basna_routes
    monkeypatch.setattr(basna_routes, "_load_registry", lambda: {"tiers": {}})
    fake = _FakeLLM(monkeypatch, {"b-model": RuntimeError("down")})
    db = _FakeDB(_tier_set(micro="qwen-4b", fast="haiku", balanced="b-model"))
    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_with_fallback(
            db, "u1", ("micro", "fast", "balanced", "reason"), "sort", "mail",
            max_tokens=200, temperature=0)
    # balanced failed and reason has no model — the weak tiers are never tried.
    assert ei.value.status == 502
    assert fake.models == ["b-model"]
    assert all(c["model"] not in ("qwen-4b", "haiku") for c in fake.created)

    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_with_fallback(
            db, "u1", ("micro", "fast"), "sort", "mail", max_tokens=200, temperature=0)
    assert ei.value.status == 400
    assert fake.models == ["b-model"]


async def test_complete_with_fallback_does_not_retry_the_same_model(monkeypatch):
    fake = _FakeLLM(monkeypatch, {"same": RuntimeError("down")})
    db = _FakeDB(_tier_set(reason="same", balanced="same"))
    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_with_fallback(
            db, "u1", llm_routes.JUDGE_TIER_ORDER, "judge", "reply",
            max_tokens=300, temperature=0)
    assert ei.value.status == 502
    assert fake.models == ["same"]


async def test_complete_with_fallback_no_tier_is_400(monkeypatch):
    from captain_claw.flight_deck import basna_routes
    monkeypatch.setattr(basna_routes, "_load_registry", lambda: {"tiers": {}})
    fake = _FakeLLM(monkeypatch)
    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_with_fallback(
            _FakeDB(""), "u1", llm_routes.CLASSIFIER_TIER_ORDER, "s", "u",
            max_tokens=200, temperature=0)
    assert ei.value.status == 400
    assert ei.value.detail == "no model configured for the 'balanced' or 'reason' tier"
    assert fake.created == []


async def test_complete_with_fallback_raises_only_owner_tier_error(monkeypatch):
    from captain_claw.flight_deck import basna_routes

    async def _broken(db, owner):
        raise RuntimeError("db gone")

    monkeypatch.setattr(basna_routes, "_load_owner_tiers", _broken)
    with pytest.raises(llm_routes.OwnerTierError) as ei:
        await llm_routes.complete_with_fallback(
            _FakeDB(), "u1", llm_routes.JUDGE_TIER_ORDER, "s", "u",
            max_tokens=300, temperature=0)
    assert ei.value.status == 500
    assert "db gone" not in str(ei.value.detail)
