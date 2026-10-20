"""The kiosk chat's skill cards (``/fd/agent-skills``).

* the model's reply is cleaned: unknown icons / sources fall back, empty and
  duplicate cards go, the set is capped;
* without the model, cards come from the agent's scheduled tasks, cron jobs,
  intentions and topics, plus "What can you do?";
* a cached set is fresh until the agent's make-up changes or it is too old
  (a model-written set lives longer than one built without it);
* the owner reads their agent's cards; another user's agent is a 404;
* a write caches the model's cards in the agent's folder, and falls back to
  the cards built without it when the reply is no good.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import yaml

from captain_claw.flight_deck import agent_skills as skills
from captain_claw.flight_deck import auth as fd_auth
from captain_claw.flight_deck import fd_scheduler as sched
from captain_claw.flight_deck import rate_limiter, server
from captain_claw.flight_deck.auth import create_access_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

BOB = "user-bob"
CAROL = "user-carol"


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Auth-enabled deck: Bob and Carol own one (stopped) process agent each."""
    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "fd.db")
    await db.init()
    set_auth_db(db)
    for uid in (BOB, CAROL):
        await db._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
            " VALUES (?, ?, 'h', ?, 'user', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (uid, f"{uid}@x.co", uid))
    await db._db.commit()

    data = tmp_path / "fd-data"
    data.mkdir()
    monkeypatch.setattr(server, "DATA_DIR", data)
    monkeypatch.setattr(server, "PROCESS_REGISTRY_FILE", data / ".processes.json")
    monkeypatch.setattr(server, "_processes", {})
    monkeypatch.setattr(server, "AUTH_ENABLED", True)
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("FD_ALLOWED_HOSTS", "fd.test")
    monkeypatch.delenv("FD_LOCKDOWN", raising=False)
    monkeypatch.setattr(rate_limiter, "_api_limiter", rate_limiter._SlidingWindow())
    monkeypatch.setattr(sched, "_STORE", sched.SchedulerStore(db_path=tmp_path / "scheduler.db"))
    monkeypatch.setattr(skills, "_RUNNING", {})

    server._save_process_registry({
        "bob-agent": {"slug": "bob-agent", "name": "Bob's agent", "description": "Deep Researcher",
                      "web_port": 24101, "web_auth": "bob-tok", "owner": BOB, "pid": None},
        "carol-agent": {"slug": "carol-agent", "name": "Carol's agent", "web_port": 24102,
                        "web_auth": "carol-tok", "owner": CAROL, "pid": None},
    })
    for slug in ("bob-agent", "carol-agent"):
        (data / slug).mkdir()
        (data / slug / "config.yaml").write_text(yaml.dump({"tools": {"enabled": ["gmail", "datastore"]}}))
    try:
        yield db
    finally:
        await db.close()
        fd_auth._db = prev


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, client=("203.0.113.9", 40001)), base_url="http://fd.test")


def _auth(uid: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


def _add_task(slug: str, owner: str, name: str = "Morning brief") -> None:
    sched.get_store().create(
        name=name, schedule="daily 08:00", agent_slug=slug, prompt="Brief me on today's calendar.",
        delivery_kind="agent", delivery_target="", delivery={"chat": True}, owner_id=owner)


# ── The model's reply ─────────────────────────────────────────────────


def test_parse_cleans_the_cards():
    reply = json.dumps({"skills": [
        {"title": "Plan my week", "description": "Focus time.", "prompt": "Plan my week.", "icon": "calendar",
         "source": "archetype"},
        {"title": "plan my week", "description": "dupe", "prompt": "again", "icon": "calendar"},
        {"title": "Odd icon", "description": "x", "prompt": "Do it.", "icon": "rocket", "source": "magic"},
        {"title": "", "description": "no title", "prompt": "Do it."},
        {"title": "No prompt", "description": "x", "prompt": ""},
        "not a card",
    ]})
    cards = skills.parse_skills(f"```json\n{reply}\n```")
    assert [c["title"] for c in cards] == ["Plan my week", "Odd icon"]
    assert cards[1]["icon"] == "sparkles" and cards[1]["source"] == "archetype"


def test_parse_keeps_only_sources_the_agent_has():
    reply = json.dumps({"skills": [
        {"title": "Chase Ana", "description": "", "prompt": "Follow up with Ana.", "source": "intention"},
        {"title": "Morning brief", "description": "", "prompt": "Brief me.", "source": "schedule"},
    ]})
    have = skills._sources({"scheduled": [{"name": "Morning brief"}]}, {})
    assert have == {"archetype", "schedule"}
    assert [c["source"] for c in skills.parse_skills(reply, have)] == ["archetype", "schedule"]


def test_parse_caps_and_refuses_empty():
    many = {"skills": [{"title": f"Task {i}", "prompt": f"Do {i}", "description": ""} for i in range(30)]}
    assert len(skills.parse_skills(json.dumps(many))) == skills._MAX_SKILLS
    with pytest.raises(ValueError):
        skills.parse_skills('{"skills": []}')


def test_parse_salvages_a_reply_cut_off_mid_list():
    cut = ('{"skills": [{"title": "Plan my week", "description": "x", "prompt": "Plan my week.", "icon": "calendar"},'
           ' {"title": "Label my emails", "description": "y", "prompt": "Label my emails.", "icon": "mail"},'
           ' {"title": "Prep my meet')
    assert [c["title"] for c in skills.parse_skills(cut)] == ["Plan my week", "Label my emails"]


def test_system_prompt_per_pass():
    assert "- 6 cards, the most useful first" in skills._system(None)
    more = skills._system([])
    assert "- 8 MORE cards" in more and "Already offered" in more
    assert skills._MAX_SKILLS == 22


# ── Without the model ─────────────────────────────────────────────────


def test_basic_cards_come_from_what_the_agent_does():
    make_up = {"scheduled": [{"name": "Morning brief", "schedule": "daily 08:00",
                              "prompt": "Brief me.", "enabled": True}]}
    done = {
        "cron": [{"text": "Check the inbox for invoices", "schedule": "every 2h"}],
        "intentions": [{"title": "Follow up with Ana", "why": "She asked for the deck"}],
        "topics": [{"title": "Q3 budget", "summary": "Numbers for Q3"}],
    }
    cards = skills.basic_skills(make_up, done)
    by_source = {c["source"] for c in cards}
    assert by_source == {"schedule", "intention", "topic", "archetype"}
    assert cards[0] == {"title": "Morning brief", "description": "Run it now — normally daily 08:00",
                        "prompt": "Brief me.", "icon": "clock", "source": "schedule"}
    assert cards[-1]["title"] == "What can you do?"
    assert skills.basic_skills({})[0]["title"] == "What can you do?"


def test_freshness():
    now = time.time()
    cache = {"fingerprint": "a", "generated_at": now - 7200, "source": "model"}
    assert skills.is_fresh(cache, "a", now)
    assert not skills.is_fresh(cache, "b", now)  # the agent changed
    assert not skills.is_fresh({**cache, "source": "basic"}, "a", now)  # try the model again
    assert not skills.is_fresh({**cache, "error": "bad reply"}, "a", now)  # kept over a failed rewrite
    assert not skills.is_fresh({**cache, "generated_at": now - skills._TTL - 1}, "a", now)
    assert not skills.is_fresh(None, "a", now)


# ── Routes and writes ─────────────────────────────────────────────────


async def test_owner_reads_cards_others_get_404(deck, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(skills, "_start", lambda slug, entry, owner: started.append(slug))
    _add_task("bob-agent", BOB)
    async with _client() as c:
        r = await c.get("/fd/agent-skills/bob-agent", headers=_auth(BOB))
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "basic"
        assert body["skills"][0]["title"] == "Morning brief"
        assert started == ["bob-agent"]  # nothing cached: written in the background

        r = await c.get("/fd/agent-skills/carol-agent", headers=_auth(BOB))
        assert r.status_code == 404
        r = await c.post("/fd/agent-skills/carol-agent/refresh", headers=_auth(BOB))
        assert r.status_code == 404
        r = await c.get("/fd/agent-skills/bob-agent")
        assert r.status_code == 401


async def test_write_caches_the_models_cards(deck, monkeypatch):
    seen: dict = {}

    async def tier(owner):
        return {"provider": "openai", "model": "fast-m"}

    async def model(tier, system, user, owner, max_tokens=0, **_):
        seen.update(user=user, owner=owner)
        return json.dumps({"skills": [{"title": "Label my emails", "description": "Sorted inbox.",
                                       "prompt": "Label my unread emails by category.", "icon": "mail",
                                       "source": "archetype"}]}), 42

    async def not_spent(owner):
        return False

    monkeypatch.setattr(skills, "_resolve_tier", tier)
    monkeypatch.setattr(skills, "call_model", model)
    monkeypatch.setattr(skills, "_quota_spent", not_spent)
    started: list[str] = []
    monkeypatch.setattr(skills, "_start", lambda slug, entry, owner: started.append(slug))
    entry = server._load_process_registry()["bob-agent"]

    payload = await skills.generate("bob-agent", entry, BOB)
    assert payload["source"] == "model" and payload["model"] == "fast-m"
    assert seen["owner"] == BOB
    assert "Deep Researcher" in seen["user"]       # the archetype its description names
    assert "gmail" in seen["user"]                 # its tools
    assert "Connected accounts: Google (Gmail, Calendar, Drive): no" in seen["user"]
    cached = skills.read_cache("bob-agent")
    assert cached["skills"][0]["title"] == "Label my emails"

    async with _client() as c:
        r = await c.get("/fd/agent-skills/bob-agent", headers=_auth(BOB))
    assert r.json()["skills"][0]["title"] == "Label my emails"
    assert started == []  # fresh: not written again

    _add_task("bob-agent", BOB)  # a new scheduled task changes its make-up
    async with _client() as c:
        r = await c.get("/fd/agent-skills/bob-agent", headers=_auth(BOB))
    assert r.json()["skills"][0]["title"] == "Label my emails"  # the last set, meanwhile
    assert started == ["bob-agent"]


def _patch_model(monkeypatch, replies: list[str]) -> list[str]:
    """The model answers *replies* in turn; returns the prompts it was sent."""
    sent: list[str] = []

    async def tier(owner):
        return {"provider": "openai", "model": "fast-m"}

    async def model(tier, system, user, owner, max_tokens=0, **_):
        sent.append(user)
        return replies[min(len(sent), len(replies)) - 1], 5  # the last one, over and over

    async def not_spent(owner):
        return False

    monkeypatch.setattr(skills, "_resolve_tier", tier)
    monkeypatch.setattr(skills, "call_model", model)
    monkeypatch.setattr(skills, "_quota_spent", not_spent)
    return sent


GOOD = json.dumps({"skills": [{"title": "Plan my week", "description": "Focus time.",
                               "prompt": "Plan my week.", "icon": "calendar"}]})


async def test_bad_reply_falls_back(deck, monkeypatch):
    sent = _patch_model(monkeypatch, ["I could not think of anything.", "Still nothing."])
    entry = server._load_process_registry()["bob-agent"]
    payload = await skills.generate("bob-agent", entry, BOB)
    assert len(sent) == 2 and sent[1].endswith(skills._RETRY_NUDGE)  # one retry, told to skip to the JSON
    assert payload["source"] == "basic" and payload["error"]
    assert payload["skills"][-1]["title"] == "What can you do?"


async def test_retry_can_succeed(deck, monkeypatch):
    _patch_model(monkeypatch, ["<think>hmm", GOOD])
    entry = server._load_process_registry()["bob-agent"]
    payload = await skills.generate("bob-agent", entry, BOB)
    assert payload["source"] == "model" and payload["skills"][0]["title"] == "Plan my week"


def _cards(prefix: str, n: int) -> str:
    return json.dumps({"skills": [{"title": f"{prefix} {i}", "description": f"{prefix} card {i}",
                                   "prompt": f"Do {prefix} {i}.", "icon": "list"} for i in range(n)]})


async def test_two_passes_up_front_then_more(deck, monkeypatch):
    entry = server._load_process_registry()["bob-agent"]
    seen_cache: list = []
    sent = _patch_model(monkeypatch, [_cards("Top", 9), _cards("A", 10), _cards("B", 10)])
    orig = skills.call_model

    async def watching(tier, system, user, owner, max_tokens=0, **kw):
        if len(sent) == 1:  # the "More" pass: the cards up front are cached already
            seen_cache.append(skills.read_cache("bob-agent"))
        assert kw.get("request_timeout") == skills._CALL_TIMEOUT
        return await orig(tier, system, user, owner, max_tokens)

    monkeypatch.setattr(skills, "call_model", watching)
    payload = await skills.generate("bob-agent", entry, BOB)
    titles = [c["title"] for c in payload["skills"]]
    assert titles[:6] == [f"Top {i}" for i in range(6)]          # capped at 6 up front
    assert titles[6:] == [f"A {i}" for i in range(8)] + [f"B {i}" for i in range(8)]  # 8 at a time, 16 more
    assert "Already offered:\n- Top 0: Top card 0" in sent[1]
    assert "- A 7: A card 7" in sent[2]                          # the second batch knows the first
    assert [c["title"] for c in seen_cache[0]["skills"]] == titles[:6]


async def test_failed_more_pass_keeps_the_earlier_more_list(deck, monkeypatch):
    entry = server._load_process_registry()["bob-agent"]
    _patch_model(monkeypatch, [_cards("Top", 6), _cards("A", 8), _cards("B", 8)])
    await skills.generate("bob-agent", entry, BOB)
    _patch_model(monkeypatch, [_cards("New", 6), "no", "no"])
    payload = await skills.generate("bob-agent", entry, BOB)
    titles = [c["title"] for c in payload["skills"]]
    assert titles[:6] == [f"New {i}" for i in range(6)]
    assert titles[6:] == [f"A {i}" for i in range(8)] + [f"B {i}" for i in range(8)]
    assert payload["error"] and payload["source"] == "model"


async def test_more_stops_when_nothing_new(deck, monkeypatch):
    entry = server._load_process_registry()["bob-agent"]
    sent = _patch_model(monkeypatch, [_cards("Top", 6), _cards("A", 3)])  # then A again: nothing new
    payload = await skills.generate("bob-agent", entry, BOB)
    assert len(payload["skills"]) == 9 and len(sent) == 3 and not payload["error"]


async def test_failed_rewrite_keeps_the_last_good_set(deck, monkeypatch):
    entry = server._load_process_registry()["bob-agent"]
    _patch_model(monkeypatch, [GOOD])
    await skills.generate("bob-agent", entry, BOB)
    _patch_model(monkeypatch, ["no", "no"])
    payload = await skills.generate("bob-agent", entry, BOB)
    assert payload["source"] == "model" and payload["error"]
    assert payload["skills"][0]["title"] == "Plan my week"
    cached = skills.read_cache("bob-agent")
    assert not skills.is_fresh(cached, cached["fingerprint"], cached["generated_at"] + skills._BASIC_TTL + 1)


async def test_no_tier_no_call(deck, monkeypatch):
    async def no_tier(owner):
        return None

    async def model(*a, **k):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(skills, "_resolve_tier", no_tier)
    monkeypatch.setattr(skills, "call_model", model)
    entry = server._load_process_registry()["bob-agent"]
    payload = await skills.generate("bob-agent", entry, BOB)
    assert payload["source"] == "basic" and payload["error"] == "no model tier set"


def test_spawn_records_the_archetype():
    src = Path(server.__file__).read_text()
    assert '"archetype": config.archetype.strip().partition("@")[0]' in src
