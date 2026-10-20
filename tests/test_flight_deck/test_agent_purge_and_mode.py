"""Removing an agent for good, and the kiosk's agent settings.

* ``DELETE /fd/processes/{slug}?purge=true`` takes the agent off the registry
  AND deletes its folder, Flight Deck's chat log with it (every lane) and the
  scheduled tasks that run it — another agent's are untouched; without
  ``purge`` the folder stays, as before;
* the folder delete never leaves DATA_DIR (a symlink goes, not its target);
* ``GET /fd/agent-mode`` reads the mode as the agent resolves it, and ``PUT``
  writes Neutra too (an archetype's default mode would otherwise come back);
* ``PUT /fd/agent-model`` with only ``vision`` leaves the model alone.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from captain_claw.flight_deck import auth as fd_auth
from captain_claw.flight_deck import fd_scheduler as sched
from captain_claw.flight_deck import rate_limiter, server
from captain_claw.flight_deck.auth import create_access_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

BOB = "user-bob"
CAROL = "user-carol"


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Auth-enabled deck: Bob and Carol own one process agent each."""
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
    monkeypatch.setattr(server, "_schedule_fleet_notify", lambda *a, **k: None)
    monkeypatch.setattr(rate_limiter, "_api_limiter", rate_limiter._SlidingWindow())
    monkeypatch.setattr(sched, "_STORE", sched.SchedulerStore(db_path=tmp_path / "scheduler.db"))

    server._save_process_registry({
        "bob-agent": {"slug": "bob-agent", "name": "Bob's agent", "web_port": 24101,
                      "web_auth": "bob-tok", "owner": BOB, "pid": None},
        "carol-agent": {"slug": "carol-agent", "name": "Carol's agent", "web_port": 24102,
                        "web_auth": "carol-tok", "owner": CAROL, "pid": None},
    })
    for slug in ("bob-agent", "carol-agent"):
        cc = data / slug / "data" / "home-config-parent" / ".captain-claw"
        cc.mkdir(parents=True)
        (data / slug / "data" / "workspace").mkdir(parents=True)
        (data / slug / "data" / "workspace" / "notes.md").write_text("hi\n")
        (data / slug / "config.yaml").write_text(yaml.dump({"model": {"provider": "openai", "model": f"{slug}-m"}}))
        (cc / "config.yaml").write_text((data / slug / "config.yaml").read_text())
    try:
        yield db
    finally:
        await db.close()
        fd_auth._db = prev


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, client=("203.0.113.9", 40001)), base_url="http://fd.test")


def _hdr(uid: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _chat(db, sid: str, uid: str, agent_id: str) -> None:
    await db.upsert_chat_session(sid, uid, agent_id, agent_id)
    await db._db.execute(
        "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, 'user', 'x', '2026')", (sid,))
    await db._db.commit()


async def _rows(db, sql: str) -> list:
    cur = await db._db.execute(sql)
    return [tuple(r) for r in await cur.fetchall()]


def _task(slug: str, owner: str) -> dict:
    return sched.get_store().create(owner_id=owner, name=slug, schedule="daily 08:00", agent_slug=slug,
                                    prompt="brief", delivery_kind="agent", delivery={"chat": True})


# ── remove for good ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_deletes_the_folder_chat_log_and_tasks_of_that_agent_only(deck):
    await _chat(deck, "proc-bob-agent", BOB, "proc-bob-agent")
    await _chat(deck, "proc-bob-agent::side", BOB, "proc-bob-agent")
    await _chat(deck, "proc-carol-agent", CAROL, "proc-carol-agent")
    bob_task, carol_task = _task("bob-agent", BOB), _task("carol-agent", CAROL)
    store = sched.get_store()
    store.start_run(bob_task, "manual")

    async with _client() as c:
        r = await c.delete("/fd/processes/bob-agent?purge=true", headers=_hdr(BOB))
    assert r.status_code == 200 and r.json()["ok"], r.text

    assert set(server._load_process_registry()) == {"carol-agent"}
    assert not (server.DATA_DIR / "bob-agent").exists()
    assert (server.DATA_DIR / "carol-agent" / "data" / "workspace" / "notes.md").is_file()
    assert await _rows(deck, "SELECT id FROM chat_sessions") == [("proc-carol-agent",)]
    assert await _rows(deck, "SELECT DISTINCT session_id FROM chat_messages") == [("proc-carol-agent",)]
    assert store.get(bob_task["id"]) is None and store.get(carol_task["id"]) is not None
    assert store.list_runs(job_id=bob_task["id"]) == []


@pytest.mark.asyncio
async def test_without_purge_the_folder_and_log_stay(deck):
    await _chat(deck, "proc-bob-agent", BOB, "proc-bob-agent")
    async with _client() as c:
        r = await c.delete("/fd/processes/bob-agent", headers=_hdr(BOB))
    assert r.status_code == 200
    assert (server.DATA_DIR / "bob-agent" / "config.yaml").is_file()
    assert await _rows(deck, "SELECT id FROM chat_sessions") == [("proc-bob-agent",)]


@pytest.mark.asyncio
async def test_nobody_purges_someone_elses_agent(deck):
    async with _client() as c:
        r = await c.delete("/fd/processes/bob-agent?purge=true", headers=_hdr(CAROL))
    assert r.status_code in (403, 404)
    assert (server.DATA_DIR / "bob-agent" / "config.yaml").is_file()
    assert "bob-agent" in server._load_process_registry()


@pytest.mark.parametrize("slug", ["", ".", "..", "a/b", "..\\x"])
def test_the_folder_delete_refuses_anything_but_one_folder_name(deck, slug):
    assert server._remove_agent_dir(slug)
    assert (server.DATA_DIR / "bob-agent").is_dir()


def test_a_symlinked_folder_loses_the_link_not_its_target(deck, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine")
    (server.DATA_DIR / "linked").symlink_to(outside)
    assert server._remove_agent_dir("linked") == ""
    assert not (server.DATA_DIR / "linked").exists() and (outside / "keep.txt").is_file()


def test_read_only_entries_go_too(deck):
    ro = server.DATA_DIR / "bob-agent" / "data" / "workspace" / "repo"
    ro.mkdir()
    (ro / "obj").write_text("x")
    (ro / "obj").chmod(0o444)
    ro.chmod(0o555)
    assert server._remove_agent_dir("bob-agent") == ""
    assert not (server.DATA_DIR / "bob-agent").exists()


# ── cognitive mode ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mode_reads_the_file_then_the_config_default_then_neutra(deck):
    cc = server.DATA_DIR / "bob-agent" / "data" / "home-config-parent" / ".captain-claw"
    async with _client() as c:
        get = lambda: c.get("/fd/agent-mode/process/bob-agent", headers=_hdr(BOB))  # noqa: E731
        assert (await get()).json() == {"mode": "neutra"}

        (cc / "config.yaml").write_text(yaml.dump({"cognitive_mode": {"default_mode": "aeolian"}}))
        assert (await get()).json() == {"mode": "aeolian"}

        (cc / "cognitive_mode.txt").write_text("lydian\n")
        assert (await get()).json() == {"mode": "lydian"}

        # Neutra is written, not left out — or the archetype's aeolian comes back.
        r = await c.put("/fd/agent-mode/process/bob-agent", headers=_hdr(BOB), json={"mode": "neutra"})
        assert r.status_code == 200
        assert (cc / "cognitive_mode.txt").read_text() == "neutra"
        assert (await get()).json() == {"mode": "neutra"}

        assert (await c.get("/fd/agent-mode/process/bob-agent", headers=_hdr(CAROL))).status_code in (403, 404)


# ── vision only ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_vision_only_update_keeps_the_model(deck):
    async with _client() as c:
        r = await c.put("/fd/agent-model/process/bob-agent", headers=_hdr(BOB), json={"vision": "on"})
        assert r.status_code == 200, r.text
        model = yaml.safe_load((server.DATA_DIR / "bob-agent" / "config.yaml").read_text())["model"]
        assert model == {"provider": "openai", "model": "bob-agent-m", "vision": True}

        r = await c.put("/fd/agent-model/process/bob-agent", headers=_hdr(BOB), json={})
        assert r.status_code == 400
