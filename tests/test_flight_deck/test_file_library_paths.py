"""The File library names each file as the agent's Files panel does.

The kiosk has no Files panel any more, so its Library is where a file is
opened, saved, turned into a deck or shared by public link — through the
agent's own routes, which take a file by its ``physical`` path and
``logical`` name. A library item carries both, exactly as the agent-files
proxy's workspace scan names the same file on disk.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from captain_claw.flight_deck import auth as fd_auth
from captain_claw.flight_deck import file_library as fl
from captain_claw.flight_deck import rate_limiter, server
from captain_claw.flight_deck.auth import create_access_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

BOB = "user-bob"
CAROL = "user-carol"


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Auth-enabled deck: Bob owns one stopped process agent with a report
    in a subfolder of its workspace; Carol owns nothing."""
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
    monkeypatch.setattr(fl, "_STORE", fl.LibraryStore(tmp_path / "file_library.db"))

    server._save_process_registry({
        "bob-agent": {"slug": "bob-agent", "name": "Bob's agent", "web_port": 24101,
                      "web_auth": "bob-tok", "owner": BOB, "pid": None},
    })
    report = data / "bob-agent" / "data" / "workspace" / "reports" / "q3.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Q3\n\nRevenue grew.\n")
    # What a scan does before it asks the model for summaries.
    fl.get_store().sync_agent("bob-agent", fl.walk_workspace("bob-agent"))
    try:
        yield data
    finally:
        await db.close()
        fd_auth._db = prev


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, client=("203.0.113.9", 40001)), base_url="http://fd.test")


def _auth(uid: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def test_item_names_the_file_as_the_files_panel_does(deck: Path):
    async with _client() as c:
        r = await c.get("/fd/file-library/files", headers=_auth(BOB))
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    path = deck / "bob-agent" / "data" / "workspace" / "reports" / "q3.md"
    assert item["physical"] == str(path)
    assert item["logical"] == "workspace/reports/q3.md"
    assert Path(item["physical"]).read_text().startswith("# Q3")

    # The very names the agent-files proxy gives the same file.
    [panel] = [f for f in server._scan_workspace_files(deck / "bob-agent") if f["filename"] == "q3.md"]
    assert (item["physical"], item["logical"]) == (panel["physical"], panel["logical"])


async def test_another_user_never_sees_the_paths(deck: Path):
    async with _client() as c:
        r = await c.get("/fd/file-library/files", headers=_auth(CAROL))
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
