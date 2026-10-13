"""Vatra run ledger — the rule-based map behind `vatra(action="map")`.

Built from the events Flight Deck already streams (narration, tool calls, board
posts, answered asks); no LLM. A worker reads a bounded map of who is covering
what, which sources teammates already read, which files exist, and what has
been found. `vatra read` leaves the narration stream out unless asked.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from captain_claw.flight_deck import auth as auth_mod
from captain_claw.flight_deck import basna_routes as b
from captain_claw.flight_deck import vatra_ledger as vl
from captain_claw.flight_deck import vatra_routes as vr
from captain_claw.flight_deck.db import FlightDeckDB


@pytest.fixture()
async def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    d = FlightDeckDB(str(Path(tmp) / "fd.db"))
    await d.init()
    u = await d.create_user("t@x.co", "h")
    sess = await d.create_basna_session(u["id"], "research the peers")
    d._test_sid = sess["id"]
    monkeypatch.setattr(auth_mod, "get_db", lambda: d)
    monkeypatch.setattr(vr, "get_db", lambda: d)
    yield d
    await d.close()


async def _drain() -> None:
    while vl._pending:
        await asyncio.gather(*list(vl._pending))


# ── db ───────────────────────────────────────────────────────────────


async def test_a_repeat_bumps_the_count_and_names_the_other_actor(db):
    sid = db._test_sid
    await db.upsert_vatra_ledger(sid, "source", "https://ex.com/a", owner="r1", subtask_id="s1",
                                 label="Researcher")
    await db.upsert_vatra_ledger(sid, "source", "https://ex.com/a", owner="a1", subtask_id="s2",
                                 label="Analyst")
    await db.upsert_vatra_ledger(sid, "source", "https://ex.com/a", owner="a1", subtask_id="s2",
                                 label="Analyst")
    (row,) = await db.list_vatra_ledger(sid)
    assert row["count"] == 3 and row["label"] == "Researcher" and row["others"] == "Analyst"


async def test_teammates_fetching_the_same_page_at_once_all_land(db):
    sid = db._test_sid
    for i in range(8):
        vl.record_action(sid, f"o{i}", f"s{i}", f"Role {i}, piece",
                         {"tool": "web_fetch", "args": {"url": "https://ex.com/a"}})
    await _drain()
    (row,) = await db.list_vatra_ledger(sid)
    assert row["count"] == 8
    assert row["others"].split(",") == [f"Role {i}  piece" for i in range(1, 8)]


async def test_focus_is_replaced_not_counted(db):
    sid = db._test_sid
    await db.upsert_vatra_ledger(sid, "focus", "s1", label="R", detail="reading filings", replace=True)
    await db.upsert_vatra_ledger(sid, "focus", "s1", label="R", detail="comparing margins", replace=True)
    (row,) = await db.list_vatra_ledger(sid, kinds=["focus"])
    assert row["detail"] == "comparing margins" and row["count"] == 1


# ── recording from the event stream ──────────────────────────────────


def test_ledger_args_keep_the_keys_and_stay_small():
    args = vl.ledger_args({"url": " https://ex.com/a ", "content": "x" * 10_000,
                           "urls": [f"https://ex.com/{i}" for i in range(30)]})
    assert args == {"url": "https://ex.com/a", "urls": [f"https://ex.com/{i}" for i in range(10)]}
    assert vl.ledger_args("not a dict") is None
    assert vl.ledger_args({"content": "x"}) is None


def test_ledger_args_mark_failed_calls_and_drop_failed_batch_urls():
    assert vl.ledger_args({"url": "https://ex.com/a"}, "Error: HTTP error 403")["failed"] is True
    assert vl.ledger_args({"path": "x.md"}, "DUPLICATE CALL BLOCKED: …")["failed"] is True
    batch = ("[URL: https://ex.com/a]\n[Status: 200]\n[Mode: md]\n\nbody\n\n"
             "[URL: https://ex.com/b]\n[Status: 403]\n[FAILED: forbidden]")
    args = vl.ledger_args({"urls": ["https://ex.com/a", "https://ex.com/b"]}, batch)
    assert args == {"urls": ["https://ex.com/a"]}


async def test_a_failed_call_is_not_a_source_or_a_file(db):
    sid = db._test_sid
    vl.record_action(sid, "r", "s1", "R", {"tool": "web_fetch",
                                           "args": {"url": "https://ex.com/a", "failed": True}})
    vl.record_action(sid, "r", "s1", "R", {"tool": "write", "args": {"path": "x.md", "failed": True}})
    await _drain()
    assert await db.list_vatra_ledger(sid) == []


async def test_worker_events_become_ledger_rows(db):
    sid = db._test_sid
    who = (sid, "researcher", "s1", "Researcher")
    vl.record_action(*who, {"tool": "narration", "detail": "Reading the 2025 filings  now"})
    vl.record_action(*who, {"tool": "web_fetch", "detail": "ex.com", "args": {"url": "https://ex.com/a"}})
    vl.record_action(*who, {"tool": "web_fetch_batch", "args": {"urls": ["https://ex.com/b", "https://ex.com/c"]}})
    vl.record_action(*who, {"tool": "pdf_extract", "args": {"path": "/ws/in/report-2025.pdf"}})
    vl.record_action(*who, {"tool": "write", "args": {"path": "vfs:p/notes/s1-notes.md"}})
    vl.record_action(*who, {"tool": "shell", "args": {"command": "ls"}})
    await _drain()
    rows = {(r["kind"], r["key"]) for r in await db.list_vatra_ledger(sid)}
    assert rows == {
        ("focus", "s1"), ("source", "https://ex.com/a"), ("source", "https://ex.com/b"),
        ("source", "https://ex.com/c"), ("source", "doc:report-2025.pdf"),
        ("file", "vfs:p/notes/s1-notes.md"),
    }
    (focus,) = await db.list_vatra_ledger(sid, kinds=["focus"])
    assert focus["detail"] == "Reading the 2025 filings now"


async def test_the_live_callback_gets_the_exact_args_but_the_stored_actions_stay_small():
    from websockets.asyncio.server import serve

    async def handler(ws):
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.send(json.dumps({"type": "replay_done"}))
        async for raw in ws:
            if json.loads(raw).get("type") != "chat":
                continue
            await ws.send(json.dumps({"type": "monitor", "tool_name": "web_fetch",
                                      "arguments": {"url": "https://ex.com/a-very-long-path"}}))
            await ws.send(json.dumps({"type": "chat_message", "role": "assistant", "content": "done"}))
            await ws.send(json.dumps({"type": "status", "status": "idle"}))

    seen: list[dict] = []
    async with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        out, actions = await b._send_chat_and_collect(port, "", "go", 5.0, on_action=seen.append)
    assert out == "done"
    assert seen[0]["args"] == {"url": "https://ex.com/a-very-long-path"}
    assert "args" not in actions[0]


# ── the map ──────────────────────────────────────────────────────────


async def test_the_map_shows_who_covers_what(db):
    sid = db._test_sid
    vl.record_action(sid, "researcher", "s1", "Researcher", {"tool": "narration", "detail": "peer margins"})
    vl.record_action(sid, "researcher", "s1", "Researcher",
                     {"tool": "web_fetch", "args": {"url": "https://ex.com/a"}})
    vl.record_action(sid, "analyst", "s2", "Analyst", {"tool": "web_fetch", "args": {"url": "https://ex.com/a"}})
    vl.record_action(sid, "analyst", "s2", "Analyst", {"tool": "write", "args": {"path": "vfs:p/model.md"}})
    vl.record_finding(sid, "analyst", "s2", "Analyst", 41, "EBITDA bridge", "the bridge text")
    vl.record_ask_answer(sid, 7, "Which fiscal year does Acme report?", "Researcher")
    await _drain()
    activity = {"s1": {"role": "Researcher", "state": "running"},
                "s2": {"role": "Analyst", "state": "running"},
                "s3": {"role": "Writer", "state": "waiting"}}
    text = await vl.render_map(sid, "s2", activity)
    assert "## Researcher · running (piece s1) — peer margins" in text
    # a teammate's block carries its files and findings
    other = await vl.render_map(sid, "s1", activity)
    assert "files: vfs:p/model.md" in other and "#41 note: EBITDA bridge" in other
    assert "## YOU — Analyst (piece s2): 1 file(s), 0 source(s) so far" in text
    assert "## Writer · waiting (piece s3)" in text
    assert "## Writer · waiting (piece s3)" in text
    assert "https://ex.com/a — Researcher (+1 more)" in text
    assert "#7 Which fiscal year does Acme report?" in text


async def test_the_map_is_bounded(db):
    sid = db._test_sid
    for i in range(400):
        vl.record_action(sid, f"o{i % 20}", f"s{i % 20}", f"Role {i % 20}",
                         {"tool": "narration", "detail": "x" * 190})
        vl.record_action(sid, f"o{i % 20}", f"s{i % 20}", f"Role {i % 20}",
                         {"tool": "write", "args": {"path": f"vfs:p/{'y' * 80}-{i}.md"}})
    await _drain()
    text = await vl.render_map(sid, "s1", {})
    assert len(text) <= vl.MAP_MAX_CHARS
    # every owner keeps at least its one line; details fill what is left
    assert all(f"(piece s{i})" in text for i in range(20))
    assert "## YOU — Role 1 (piece s1)" in text


async def test_the_team_source_list_survives_a_crowded_map(db):
    sid = db._test_sid
    for o in range(10):
        who = (sid, f"o{o}", f"s{o}", f"Role {o}")
        vl.record_action(*who, {"tool": "narration", "detail": "z" * 200})
        for f in range(4):
            vl.record_action(*who, {"tool": "write", "args": {"path": f"vfs:p/{'w' * 60}-{o}-{f}.md"}})
        for k in range(5):
            vl.record_finding(sid, f"o{o}", f"s{o}", f"Role {o}", o * 10 + k, "", "q " * 80)
        for u in range(8):
            vl.record_action(*who, {"tool": "web_fetch",
                                    "args": {"url": f"https://example.org/{'a' * 80}/{o}/{u}"}})
    await _drain()
    text = await vl.render_map(sid, "s0", {})
    assert len(text) <= vl.MAP_MAX_CHARS
    assert "Sources read across the team: 80. Most recent:" in text
    assert text.count("https://example.org/") == min(vl._RECENT_SOURCES, max(5, 20 - 10))


async def test_the_reporter_gets_a_coverage_appendix(db):
    sid = db._test_sid
    assert await vl.coverage_appendix(sid) == ""
    vl.record_action(sid, "r", "s1", "Researcher", {"tool": "web_fetch", "args": {"url": "https://ex.com/a"}})
    vl.record_action(sid, "r", "s1", "Researcher", {"tool": "write", "args": {"path": "vfs:p/notes/s1-notes.md"}})
    await _drain()
    text = await vl.coverage_appendix(sid)
    assert "- Researcher: 1 source(s) read; files: vfs:p/notes/s1-notes.md" in text


# ── endpoints ────────────────────────────────────────────────────────


async def _no_auth(body):
    return "u", body.session_id


async def test_the_map_endpoint(db, monkeypatch):
    sid = db._test_sid
    monkeypatch.setattr(vr, "_board_session", _no_auth)
    monkeypatch.setitem(vr._owner_activity, sid, {"s1": {"role": "Researcher", "state": "running"}})
    vl.record_action(sid, "r", "s1", "Researcher", {"tool": "web_fetch", "args": {"url": "https://ex.com/a"}})
    await _drain()
    out = await vr.agent_map(vr._VatraMapReq(session_id=sid, subtask_id="s1"))
    assert "## YOU — Researcher (piece s1): 0 file(s), 1 source(s) so far" in out["map"]


async def test_read_leaves_narration_out_unless_asked(db, monkeypatch):
    sid = db._test_sid
    monkeypatch.setattr(vr, "_board_session", _no_auth)
    await db.add_vatra_board(sid, "a", "s2", "note", "margins", "gross margin 41%")
    for i in range(30):
        await db.add_vatra_board(sid, "a", "s2", "narration", "", f"thinking {i}")
    out = await vr.agent_board_read(vr._VatraBoardReadReq(session_id=sid, subtask_id="s1", limit=10))
    assert [e["kind"] for e in out["entries"]] == ["note"]
    out = await vr.agent_board_read(vr._VatraBoardReadReq(session_id=sid, subtask_id="s1", limit=10,
                                                          kind="narration"))
    assert len(out["entries"]) == 10 and {e["kind"] for e in out["entries"]} == {"narration"}


async def test_a_board_post_becomes_a_finding(db, monkeypatch):
    sid = db._test_sid
    monkeypatch.setattr(vr, "_board_session", _no_auth)
    monkeypatch.setitem(vr._owner_activity, sid, {"s2": {"role": "Analyst"}})
    res = await vr.agent_board_post(vr._VatraBoardPostReq(session_id=sid, owner="a", subtask_id="s2",
                                                          title="Peer set", text="ACME, BETA, GAMMA"))
    await _drain()
    (row,) = await db.list_vatra_ledger(sid, kinds=["finding"])
    assert row["key"] == f"board:{res['id']}" and row["label"] == "Analyst"
    assert row["detail"] == "note: Peer set"


def test_the_brief_asks_for_notes_and_names_the_map():
    st = {"id": "s2", "title": "Peer margins", "brief": "compare margins", "owner_archetype_id": "analyst"}
    prompt = vr._build_subtask_prompt("Analyst", "research the peers", st, [], [st], vfs_project="acme")
    assert "vatra(action=\"map\")" in prompt
    assert "vfs:acme/notes/s2-notes.md" in prompt
    scoped = vr._build_subtask_prompt("Analyst", "x", st, [], [st], vfs_project="acme",
                                      session_id="0f1e2d3c-aaaa-bbbb")
    assert "vfs:acme/notes/0f1e2d3c-s2-notes.md" in scoped
    # no shared folder → no notes directive
    assert "notes/" not in vr._build_subtask_prompt("Analyst", "x", st, [], [st])


# ── review follow-ups: notes are scratch, not contributions or deliverables ──


def test_a_notes_write_alone_does_not_pass_the_acted_gate():
    from captain_claw.flight_deck.quality_profile import worker_produced_nothing

    notes_only = {"output": "", "actions": [
        {"tool": "web_fetch", "detail": "url=https://ex.com/a"},
        {"tool": "write", "detail": "path=vfs:acme/notes/0f1e2d3c-s2-notes.md, content=- a, append=True"}]}
    assert worker_produced_nothing(notes_only, skip_notes=True) is True
    assert worker_produced_nothing(notes_only) is False          # Basna: unchanged
    real = {"output": "", "actions": [{"tool": "write", "detail": "path=vfs:acme/peer-margins.md, content=#"}]}
    assert worker_produced_nothing(real, skip_notes=True) is False
    # a real notes/ package or notes.md deliverable is work, even in Vatra
    for path in ("vfs:x/notes/models.py", "vfs:x/notes.md", "notes/views.py"):
        app = {"output": "", "actions": [{"tool": "write", "detail": f"path={path}, content=x"}]}
        assert worker_produced_nothing(app, skip_notes=True) is False


def test_pointer_recovery_never_ships_notes_or_sources(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "s1-notes.md").write_text("n" * 5000)
    (tmp_path / "report.md").write_text("the report " * 100)

    def _resolve(user_id, project, path):
        return tmp_path / path.split("/", 1)[1]

    monkeypatch.setattr(vr, "_vfs_resolve_under", _resolve)
    text, path = vr._resolve_deliverable("u", "acme", ["vfs:acme/notes/s1-notes.md", "vfs:acme/report.md"])
    assert path == "vfs:acme/report.md"
    # a real notes/ deliverable still resolves; only the run's top-level sources/ is skipped
    (notes / "study.md").write_text("study " * 2000)
    text, path = vr._resolve_deliverable("u", "acme", ["vfs:acme/notes/study.md", "vfs:acme/report.md"])
    assert path == "vfs:acme/notes/study.md"


def test_the_continuation_manifest_skips_notes(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "s1-notes.md").write_text("scratch")
    (tmp_path / "report.md").write_text("report")
    (tmp_path / "notes" / "models.py").write_text("class Note: ...")
    body = b._manifest_body(tmp_path, "acme")
    assert "vfs:acme/report.md" in body and "vfs:acme/notes/models.py" in body
    assert "s1-notes.md" not in body


async def test_horizon_keeps_ledger_args_out_of_the_stored_actions():
    from captain_claw.flight_deck.horizon_worker import HorizonConfig, run_worker_horizon

    async def spawn(suffix):
        return 1000, "tok", f"slug-{suffix}"

    async def send(port, token, prompt, timeout, *, on_action=None, **kw):
        on_action({"tool": "web_fetch", "detail": "ex.com", "args": {"url": "https://ex.com/a"}})
        return "Answer: 42"

    async def stop(slug):
        return None

    live: list[dict] = []
    res = await run_worker_horizon(
        spawn=spawn, tier="fast", prompt="Q", send=send, stop=stop, on_action=live.append,
        cfg=HorizonConfig.from_dict({"samples": 1, "fix_attempts": 0, "agreement_threshold": 0.5}))
    assert live and live[0]["args"] == {"url": "https://ex.com/a"}   # the live view keeps them
    assert res["actions"] and all("args" not in a for a in res["actions"])


async def test_a_crowded_map_keeps_the_callers_line_and_every_owner(db):
    sid = db._test_sid
    for o in range(1, 11):
        who = (sid, f"o{o}", f"s{o}", f"Role {o}")
        vl.record_action(*who, {"tool": "narration", "detail": "y" * 200})
        vl.record_action(*who, {"tool": "write", "args": {"path": f"vfs:p/{'w' * 60}-{o}.md"}})
        for k in range(2):
            vl.record_finding(sid, f"o{o}", f"s{o}", f"Role {o}", o * 10 + k, "", "q " * 80)
        for u in range(4):
            vl.record_action(*who, {"tool": "web_fetch",
                                    "args": {"url": f"https://example.org/{'a' * 90}/{o}/{u}"}})
    for a in range(5):
        vl.record_ask_answer(sid, a, "x " * 100, "Role 1")
    await _drain()
    for caller in ("s3", "s10"):
        text = await vl.render_map(sid, caller, {})
        assert len(text) <= vl.MAP_MAX_CHARS
        assert f"## YOU — Role {caller[1:]} (piece {caller})" in text
        assert "Sources read across the team: 40." in text
    text = await vl.render_map(sid, "s10", {})
    heads = [line for line in text.splitlines() if line.startswith("## Role")]
    assert heads[:2] == [h for h in heads if "(piece s1)" in h or "(piece s2)" in h][:2]  # s2 before s10
    assert text.index("(piece s2)") < text.index("(piece s9)")
    assert len(heads) == 9 or "more owner(s)" in text
