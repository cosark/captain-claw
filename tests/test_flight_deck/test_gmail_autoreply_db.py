"""Gmail topic auto-replies — the Flight Deck DB layer.

The `gmail_sends` origin columns (old rows read as 'agent', so the agent-send
limit and listings keep their meaning), the per-owner poll state, and the
exactly-once ledger: claims, compare-and-set transitions, the boot recovery
table, retention, the classification budget and the counts the caps use.
Everything runs on a real SQLite file in tmp_path; nothing is mocked.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from captain_claw.flight_deck import db as dbmod
from captain_claw.flight_deck.db import FlightDeckDB

ALICE = "user-alice"
BOB = "user-bob"
# The real clock (whole seconds): rows the helpers stamp with _utcnow() and
# rows a test backdates relative to NOW sit on one timeline.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture()
async def db(tmp_path):
    d = FlightDeckDB(tmp_path / "fd.db")
    await d.init()
    try:
        yield d
    finally:
        await d.close()


async def _columns(d: FlightDeckDB, table: str) -> dict[str, dict]:
    rows = await d._db.execute_fetchall(f"PRAGMA table_info({table})")
    return {r["name"]: dict(r) for r in rows}


async def _indexes(d: FlightDeckDB, table: str) -> set[str]:
    rows = await d._db.execute_fetchall(f"PRAGMA index_list({table})")
    return {r["name"] for r in rows}


async def _stamp(d: FlightDeckDB, rid: str, *, created: datetime | None = None,
                 updated: datetime | None = None) -> None:
    """Backdate a ledger row (claim time and/or last change)."""
    if created is not None:
        await d._db.execute("UPDATE gmail_autoreplies SET created_at = ? WHERE id = ?",
                            (created.isoformat(), rid))
    if updated is not None:
        await d._db.execute("UPDATE gmail_autoreplies SET updated_at = ? WHERE id = ?",
                            (updated.isoformat(), rid))
    await d._db.commit()


async def _row_in(d: FlightDeckDB, owner: str, mid: str, state: str, **fields) -> str:
    """Claim `mid` and force it into `state` (test setup, bypassing CAS)."""
    rid = await d.claim_autoreply(owner, mid, f"t-{mid}")
    assert rid
    if fields:
        assert await d.update_autoreply(rid, owner, **fields)
    await d._db.execute("UPDATE gmail_autoreplies SET state = ? WHERE id = ?", (state, rid))
    await d._db.commit()
    return rid


async def _state_of(d: FlightDeckDB, owner: str, rid: str) -> str:
    return (await d.get_autoreply(owner, rid))["state"]


# ── schema ───────────────────────────────────────────────────────────

class TestSchema:
    async def test_fresh_db_has_the_tables_columns_and_indexes(self, db):
        sends = await _columns(db, "gmail_sends")
        assert sends["origin"]["dflt_value"] == "'agent'"
        assert sends["topic_id"]["dflt_value"] == "''"
        assert sends["in_reply_to_message_id"]["dflt_value"] == "''"
        assert all(sends[c]["notnull"] == 1 for c in ("origin", "topic_id",
                                                      "in_reply_to_message_id"))

        state = await _columns(db, "gmail_autoreply_state")
        assert list(state) == [
            "owner_id", "history_id", "google_sub", "account_email", "enabled_at",
            "own_addresses", "own_addresses_at", "last_poll_at", "last_ok_at",
            "last_error", "classify_day", "classify_count", "updated_at"]
        assert state["owner_id"]["pk"] == 1

        ledger = await _columns(db, "gmail_autoreplies")
        assert list(ledger) == [
            "id", "owner_id", "inbound_message_id", "thread_id", "received_at",
            "from_addr", "from_domain", "subject", "state", "reason", "detail",
            "topic_id", "topic_name", "confidence", "match_judge", "reply_text",
            "findings", "judge", "sources_used", "agent_ref", "agent_name",
            "attempts", "approved_by", "sent_message_id", "created_at", "updated_at"]
        # The inbound body is never stored — there is no column for it.
        assert not {"body", "text", "inbound_text", "inbound_body"} & set(ledger)
        assert {"idx_gmail_autoreplies_owner", "idx_gmail_autoreplies_thread",
                "idx_gmail_autoreplies_sender", "idx_gmail_autoreplies_state"} \
            <= await _indexes(db, "gmail_autoreplies")
        # UNIQUE(owner_id, inbound_message_id) is a real constraint.
        uniq = [r for r in await db._db.execute_fetchall("PRAGMA index_list(gmail_autoreplies)")
                if r["unique"] and r["origin"] == "u"]
        assert len(uniq) == 1
        cols = await db._db.execute_fetchall(f"PRAGMA index_info({uniq[0]['name']})")
        assert [c["name"] for c in cols] == ["owner_id", "inbound_message_id"]

    async def test_old_schema_db_is_migrated_in_place(self, tmp_path):
        path = tmp_path / "old.db"
        con = sqlite3.connect(path)
        # gmail_sends as it shipped first (before status / attachments / origin).
        con.executescript("""
            CREATE TABLE gmail_sends (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, agent TEXT NOT NULL DEFAULT '',
                to_addrs TEXT NOT NULL DEFAULT '', cc_addrs TEXT NOT NULL DEFAULT '',
                bcc_addrs TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
                gmail_message_id TEXT NOT NULL DEFAULT '', thread_id TEXT NOT NULL DEFAULT '',
                draft_id TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL);
        """)
        con.execute("INSERT INTO gmail_sends (id, owner_id, agent, subject, created_at)"
                    " VALUES ('old1', ?, 'Scout', 'before', '2026-10-01T00:00:00+00:00')",
                    (ALICE,))
        con.commit()
        con.close()

        d = FlightDeckDB(path)
        await d.init()
        try:
            cols = await _columns(d, "gmail_sends")
            assert {"status", "attachments", "origin", "topic_id",
                    "in_reply_to_message_id"} <= set(cols)
            (old,) = await d.list_gmail_sends(ALICE)
            assert old["origin"] == "agent" and old["topic_id"] == ""
            assert old["in_reply_to_message_id"] == "" and old["status"] == "sent"
            since = "2000-01-01T00:00:00+00:00"
            # Existing rows keep counting as agent sends — the limit is unchanged.
            assert await d.count_gmail_sends_since(ALICE, since) == 1
            assert await d.count_gmail_sends_since(ALICE, since, origin="agent") == 1
            assert await d.count_gmail_sends_since(ALICE, since, origin="autoreply") == 0
            # The new tables arrive on an existing DB too.
            assert await d.list_autoreply_states() == []
            assert await d.claim_autoreply(ALICE, "m1", "t1")
        finally:
            await d.close()

        again = FlightDeckDB(path)   # a second boot is a no-op, nothing lost
        await again.init()
        try:
            assert len(await again.list_gmail_sends(ALICE)) == 1
            assert await again.count_autoreplies(ALICE, "") == 1
        finally:
            await again.close()


# ── gmail_sends origin ───────────────────────────────────────────────

class TestGmailSendsOrigin:
    async def test_default_origin_is_agent_and_new_fields_round_trip(self, db):
        sid = await db.add_gmail_send(ALICE, agent="Scout", to_addrs="x@y.co", subject="s")
        auto = await db.add_gmail_send(
            ALICE, agent="Scout", to_addrs="z@y.co", subject="Re: q", origin="autoreply",
            topic_id="t_0011aabb", in_reply_to_message_id="18c0ffee")
        rows = {r["id"]: r for r in await db.list_gmail_sends(ALICE)}
        assert rows[sid]["origin"] == "agent"
        assert rows[sid]["topic_id"] == "" and rows[sid]["in_reply_to_message_id"] == ""
        assert rows[auto]["origin"] == "autoreply"
        assert rows[auto]["topic_id"] == "t_0011aabb"
        assert rows[auto]["in_reply_to_message_id"] == "18c0ffee"

    async def test_count_and_list_filter_by_origin_none_means_all(self, db):
        for origin, subject in [("agent", "a1"), ("autoreply", "r1"), ("agent", "a2"),
                                ("autoreply_approved", "h1")]:
            await db.add_gmail_send(ALICE, subject=subject, origin=origin)
        await db.add_gmail_send(BOB, subject="b1", origin="autoreply")
        since = "2000-01-01T00:00:00+00:00"
        assert await db.count_gmail_sends_since(ALICE, since) == 4   # two-arg form: all
        assert await db.count_gmail_sends_since(ALICE, since, None) == 4
        assert await db.count_gmail_sends_since(ALICE, since, origin="agent") == 2
        assert await db.count_gmail_sends_since(ALICE, since, origin="autoreply") == 1
        assert await db.count_gmail_sends_since(ALICE, since,
                                                origin="autoreply_approved") == 1
        assert await db.count_gmail_sends_since(ALICE, "2999-01-01T00:00:00+00:00") == 0

        assert len(await db.list_gmail_sends(ALICE)) == 4
        assert {r["subject"] for r in await db.list_gmail_sends(ALICE, 20, "agent")} \
            == {"a1", "a2"}
        assert [r["subject"] for r in await db.list_gmail_sends(ALICE, origin="autoreply")] \
            == ["r1"]
        assert len(await db.list_gmail_sends(ALICE, 1)) == 1   # positional limit still works
        assert [r["owner_id"] for r in await db.list_gmail_sends(BOB)] == [BOB]


# ── per-owner poll state ─────────────────────────────────────────────

class TestAutoreplyState:
    async def test_set_creates_get_parses_list_shows_only_existing(self, db):
        assert await db.get_autoreply_state(ALICE) is None
        assert await db.list_autoreply_states() == []
        await db.set_autoreply_state(
            ALICE, history_id=12345, google_sub="sub-a", account_email="alice@x.co",
            enabled_at="2026-10-14T10:00:00+00:00",
            own_addresses=["Alice@X.co", " alias@x.co ", "alice@x.co", ""],
            own_addresses_at="2026-10-14T10:00:00+00:00")
        st = await db.get_autoreply_state(ALICE)
        assert st["history_id"] == "12345"           # numeric ids stored as text
        assert st["own_addresses"] == ["alice@x.co", "alias@x.co"]   # folded, deduped
        assert st["classify_count"] == 0 and st["classify_day"] == ""
        assert st["last_error"] == "" and st["updated_at"]
        assert [s["owner_id"] for s in await db.list_autoreply_states()] == [ALICE]

    async def test_set_merges_and_keeps_the_budget(self, db):
        await db.set_autoreply_state(ALICE, history_id="1", google_sub="sub-a",
                                     enabled_at="e1")
        assert await db.incr_classify_budget(ALICE, "2026-10-14", 5)
        await db.set_autoreply_state(ALICE, history_id="2")
        st = await db.get_autoreply_state(ALICE)
        assert st["history_id"] == "2" and st["google_sub"] == "sub-a"
        assert st["classify_day"] == "2026-10-14" and st["classify_count"] == 1

    async def test_update_never_creates_and_honours_if_enabled_at(self, db):
        # A tick that outlives a turn-off must not switch the engine back on.
        assert await db.update_autoreply_state(ALICE, history_id="9") is False
        assert await db.get_autoreply_state(ALICE) is None
        await db.set_autoreply_state(ALICE, history_id="1", enabled_at="e1")
        assert await db.update_autoreply_state(ALICE, history_id="5",
                                               last_ok_at="2026-10-14T10:01:00+00:00",
                                               last_error="")
        assert (await db.get_autoreply_state(ALICE))["history_id"] == "5"
        # The row was deleted and re-created (a fresh baseline) in between.
        assert await db.update_autoreply_state(ALICE, if_enabled_at="e0",
                                               history_id="4") is False
        assert (await db.get_autoreply_state(ALICE))["history_id"] == "5"
        assert await db.update_autoreply_state(ALICE, if_enabled_at="e1", history_id="6")
        assert (await db.get_autoreply_state(ALICE))["history_id"] == "6"
        await db.update_autoreply_state(ALICE, last_error="x" * 1000)
        assert len((await db.get_autoreply_state(ALICE))["last_error"]) == 300

    async def test_delete(self, db):
        await db.set_autoreply_state(ALICE, history_id="1")
        await db.set_autoreply_state(BOB, history_id="2")
        assert await db.delete_autoreply_state(ALICE) is True
        assert await db.delete_autoreply_state(ALICE) is False
        assert await db.get_autoreply_state(ALICE) is None
        assert [s["owner_id"] for s in await db.list_autoreply_states()] == [BOB]

    async def test_unknown_or_bad_columns_raise_and_write_nothing(self, db):
        with pytest.raises(ValueError):
            await db.set_autoreply_state(ALICE, enabled=True)
        with pytest.raises(ValueError):
            await db.set_autoreply_state(ALICE, updated_at="x")
        with pytest.raises(ValueError):
            await db.set_autoreply_state(ALICE, own_addresses="not json")
        with pytest.raises(ValueError):
            await db.set_autoreply_state(ALICE, own_addresses=42)
        assert await db.get_autoreply_state(ALICE) is None
        await db.set_autoreply_state(ALICE, own_addresses='["A@x.co"]')   # JSON text is fine
        assert (await db.get_autoreply_state(ALICE))["own_addresses"] == ["a@x.co"]
        with pytest.raises(ValueError):
            await db.update_autoreply_state(ALICE, updated_at="x")

    async def test_corrupt_own_addresses_reads_empty(self, db):
        await db.set_autoreply_state(ALICE, history_id="1")
        await db._db.execute("UPDATE gmail_autoreply_state SET own_addresses = '{oops'")
        await db._db.commit()
        assert (await db.get_autoreply_state(ALICE))["own_addresses"] == []


class TestClassifyBudget:
    async def test_no_state_row_fails_closed(self, db):
        assert await db.incr_classify_budget(ALICE, "2026-10-14", 200) is False

    async def test_counts_to_the_limit_then_refuses_and_a_new_day_resets(self, db):
        await db.set_autoreply_state(ALICE, history_id="1")
        assert [await db.incr_classify_budget(ALICE, "2026-10-14", 3) for _ in range(5)] \
            == [True, True, True, False, False]
        st = await db.get_autoreply_state(ALICE)
        assert st["classify_day"] == "2026-10-14" and st["classify_count"] == 3
        assert await db.incr_classify_budget(ALICE, "2026-10-15", 3) is True
        st = await db.get_autoreply_state(ALICE)
        assert st["classify_day"] == "2026-10-15" and st["classify_count"] == 1
        # Per owner.
        await db.set_autoreply_state(BOB, history_id="1")
        assert await db.incr_classify_budget(BOB, "2026-10-14", 3) is True

    async def test_bad_inputs_refuse(self, db):
        await db.set_autoreply_state(ALICE, history_id="1")
        assert await db.incr_classify_budget(ALICE, "2026-10-14", 0) is False
        assert await db.incr_classify_budget(ALICE, "2026-10-14", -5) is False
        assert await db.incr_classify_budget(ALICE, "", 10) is False
        assert (await db.get_autoreply_state(ALICE))["classify_count"] == 0

    async def test_concurrent_takes_never_exceed_the_limit(self, db):
        await db.set_autoreply_state(ALICE, history_id="1")
        got = await asyncio.gather(*[db.incr_classify_budget(ALICE, "2026-10-14", 5)
                                     for _ in range(12)])
        assert got.count(True) == 5
        assert (await db.get_autoreply_state(ALICE))["classify_count"] == 5


# ── the ledger ───────────────────────────────────────────────────────

class TestClaim:
    async def test_claim_once_per_owner_and_message(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        assert re.fullmatch(r"ar_[0-9a-f]{16}", rid)
        assert await db.claim_autoreply(ALICE, "m1", "t1") is None
        assert await db.claim_autoreply(ALICE, "m1") is None
        bob = await db.claim_autoreply(BOB, "m1", "t1")      # another owner's mailbox
        assert bob and bob != rid
        row = await db.get_autoreply(ALICE, rid)
        assert row["state"] == "new" and row["inbound_message_id"] == "m1"
        assert row["thread_id"] == "t1" and row["attempts"] == 0
        assert row["findings"] == [] and row["judge"] == {} and row["sources_used"] == []
        assert row["confidence"] is None and row["reply_text"] == ""

    async def test_burst_overflow_is_claimed_skipped(self, db):
        rid = await db.claim_autoreply(ALICE, "m2", "t2", state="skipped", reason="burst")
        row = await db.get_autoreply(ALICE, rid)
        assert (row["state"], row["reason"]) == ("skipped", "burst")
        # Already claimed: a later poll of the same id does not resurrect it.
        assert await db.claim_autoreply(ALICE, "m2", "t2") is None

    async def test_claim_refuses_other_states_and_empty_ids(self, db):
        for bad in ("sending", "sent", "approved", "held", "bogus"):
            with pytest.raises(ValueError):
                await db.claim_autoreply(ALICE, "m3", "t3", state=bad)
        assert await db.claim_autoreply(ALICE, "", "t3") is None
        assert await db.count_autoreplies(ALICE, "") == 0

    async def test_concurrent_claims_of_one_message_give_one_row(self, db):
        got = await asyncio.gather(*[db.claim_autoreply(ALICE, "m4", "t4") for _ in range(8)])
        assert sum(1 for g in got if g) == 1
        assert await db.count_autoreplies(ALICE, "") == 1


class TestCas:
    async def test_transition_only_from_the_expected_states(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        assert await db.cas_autoreply(rid, ALICE, ("new",), "matching") is True
        assert await db.cas_autoreply(rid, ALICE, ("new",), "matching") is False
        assert await db.cas_autoreply(rid, ALICE, "matching", "writing",
                                      topic_id="t_1", topic_name="Meetup",
                                      confidence=0.93, match_judge="tier") is True
        row = await db.get_autoreply(ALICE, rid)
        assert row["state"] == "writing" and row["topic_id"] == "t_1"
        assert row["confidence"] == pytest.approx(0.93) and row["match_judge"] == "tier"

    async def test_other_owner_and_unknown_rows_never_move(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        assert await db.cas_autoreply(rid, BOB, ("new",), "matching") is False
        assert await db.cas_autoreply("ar_0000000000000000", ALICE, ("new",), "matching") \
            is False
        assert await _state_of(db, ALICE, rid) == "new"
        assert await db.cas_autoreply(rid, ALICE, (), "matching") is False

    async def test_inc_attempts_and_same_state_requeue(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        assert await db.cas_autoreply(rid, ALICE, ("new",), "new", inc_attempts=True)
        assert await db.cas_autoreply(rid, ALICE, ("new",), "new", inc_attempts=True,
                                      reason="fetch", detail="503")
        row = await db.get_autoreply(ALICE, rid)
        assert row["attempts"] == 2 and row["reason"] == "fetch" and row["state"] == "new"
        # An explicit attempts value wins over the increment.
        assert await db.cas_autoreply(rid, ALICE, ("new",), "failed", inc_attempts=True,
                                      attempts=7, reason="fetch")
        assert (await db.get_autoreply(ALICE, rid))["attempts"] == 7

    async def test_typos_and_state_fields_raise(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        with pytest.raises(ValueError):
            await db.cas_autoreply(rid, ALICE, ("new",), "sendng")
        with pytest.raises(ValueError):
            await db.cas_autoreply(rid, ALICE, ("nwe",), "matching")
        with pytest.raises(ValueError):
            await db.cas_autoreply(rid, ALICE, ("new",), "matching", state="sent")
        with pytest.raises(ValueError):
            await db.cas_autoreply(rid, ALICE, ("new",), "matching", inbound_message_id="m9")
        assert await _state_of(db, ALICE, rid) == "new"

    async def test_commit_point_is_won_exactly_once(self, db):
        rid = await _row_in(db, ALICE, "m1", "approved")
        got = await asyncio.gather(*[db.cas_autoreply(rid, ALICE, ("approved",), "sending")
                                     for _ in range(6)])
        assert got.count(True) == 1
        assert await _state_of(db, ALICE, rid) == "sending"

    async def test_owner_path_held_to_approved(self, db):
        rid = await _row_in(db, ALICE, "m1", "held", reply_text="Hello", reason="judge:x")
        assert await db.cas_autoreply(rid, ALICE, ("held",), "approved", approved_by=ALICE)
        assert await db.cas_autoreply(rid, ALICE, ("held",), "approved",
                                      approved_by=ALICE) is False   # second click
        row = await db.get_autoreply(ALICE, rid)
        assert row["approved_by"] == ALICE and row["reply_text"] == "Hello"


@dataclass(frozen=True)
class _Finding:
    kind: str
    level: str
    hint: str
    echo: bool


class TestUpdate:
    async def test_whitelisted_columns_clipped_and_folded(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        assert await db.update_autoreply(
            rid, ALICE, from_addr="  Ana@Example.COM ", from_domain="Example.COM",
            subject="s" * 500, detail="d" * 900, reason="r" * 400, topic_name="n" * 300,
            received_at="2026-10-14T11:59:00+00:00", agent_ref="process:scout:0123",
            agent_name="Scout", sent_message_id="", reply_text="x" * 5000)
        row = await db.get_autoreply(ALICE, rid)
        assert row["from_addr"] == "ana@example.com" and row["from_domain"] == "example.com"
        assert len(row["subject"]) == 200 and len(row["detail"]) == 300
        assert len(row["reason"]) == 200 and len(row["topic_name"]) == 120
        assert len(row["reply_text"]) == 5000          # reply text is never clipped
        assert row["state"] == "new"                   # update never moves state

    async def test_json_columns_round_trip(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        findings = (_Finding("iban", "hold", "HR…89", False),
                    _Finding("email", "hold", "an…om", True))
        await db.update_autoreply(rid, ALICE, findings=findings,
                                  judge={"off_topic": 0.05, "note": "ok"},
                                  sources_used={"folder:faq.md", "insights"})
        row = await db.get_autoreply(ALICE, rid)
        assert row["findings"] == [
            {"kind": "iban", "level": "hold", "hint": "HR…89", "echo": False},
            {"kind": "email", "level": "hold", "hint": "an…om", "echo": True}]
        assert row["judge"] == {"off_topic": 0.05, "note": "ok"}
        assert row["sources_used"] == ["folder:faq.md", "insights"]   # sets sorted

    async def test_corrupt_or_mistyped_json_reads_empty(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        await db._db.execute("UPDATE gmail_autoreplies SET findings = '{bad',"
                             " judge = '[1]', sources_used = '\"x\"' WHERE id = ?", (rid,))
        await db._db.commit()
        row = await db.get_autoreply(ALICE, rid)
        assert row["findings"] == [] and row["judge"] == {} and row["sources_used"] == []

    async def test_non_whitelisted_columns_raise(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        for bad in ("state", "id", "inbound_message_id", "created_at", "updated_at",
                    "body", "subjct"):
            with pytest.raises(ValueError):
                await db.update_autoreply(rid, ALICE, **{bad: "x"})
        with pytest.raises(TypeError):   # the owner is the positional scope, never a field
            await db.update_autoreply(rid, ALICE, **{"owner_id": BOB})
        with pytest.raises(TypeError):
            await db.update_autoreply(rid, ALICE, findings=[object()])

    async def test_scoped_to_the_owner(self, db):
        rid = await db.claim_autoreply(ALICE, "m1", "t1")
        assert await db.update_autoreply(rid, BOB, detail="hijack") is False
        assert await db.update_autoreply(rid, ALICE) is False   # nothing to write
        assert (await db.get_autoreply(ALICE, rid))["detail"] == ""
        assert await db.get_autoreply(BOB, rid) is None


class TestQueries:
    async def test_next_is_oldest_first_by_state_and_owner(self, db):
        ids = []
        for i in range(4):
            rid = await db.claim_autoreply(ALICE, f"m{i}", "t")
            await _stamp(db, rid, created=NOW + timedelta(minutes=10 - i))  # m3 oldest
            ids.append(rid)
        await db.cas_autoreply(ids[1], ALICE, ("new",), "waiting_agent")
        await db.cas_autoreply(ids[2], ALICE, ("new",), "skipped", reason="labels")
        await db.claim_autoreply(BOB, "mb", "t")
        got = await db.next_autoreplies(ALICE, ("new", "waiting_agent"), limit=5)
        assert [r["id"] for r in got] == [ids[3], ids[1], ids[0]]
        assert [r["id"] for r in await db.next_autoreplies(ALICE, "new", 1)] == [ids[3]]
        assert await db.next_autoreplies(ALICE, ()) == []
        with pytest.raises(ValueError):
            await db.next_autoreplies(ALICE, ("neww",))

    async def test_count_filters(self, db):
        a = await _row_in(db, ALICE, "m1", "sent", from_addr="ana@ex.com", topic_id="t_a")
        b = await _row_in(db, ALICE, "m2", "unknown", from_addr="ANA@ex.com", topic_id="t_a")
        c = await _row_in(db, ALICE, "m3", "skipped", from_addr="bo@ex.com",
                          reason="cap_flood")
        await _row_in(db, BOB, "m4", "sent", from_addr="ana@ex.com")
        for rid in (a, b, c):
            await _stamp(db, rid, created=NOW - timedelta(hours=2),
                         updated=NOW - timedelta(hours=2))
        sends = dbmod.AUTOREPLY_SEND_STATES
        hour_ago = (NOW - timedelta(hours=1)).isoformat()
        day_ago = (NOW - timedelta(days=1)).isoformat()
        assert await db.count_autoreplies(ALICE, day_ago, sends) == 2
        assert await db.count_autoreplies(ALICE, hour_ago, sends) == 0
        assert await db.count_autoreplies(ALICE, "", sends) == 2          # lifetime
        assert await db.count_autoreplies(ALICE, "", None) == 3           # any state
        assert await db.count_autoreplies(ALICE, "", sends, thread_id="t-m1") == 1
        assert await db.count_autoreplies(ALICE, "", sends, from_addr=" Ana@Ex.com") == 2
        assert await db.count_autoreplies(ALICE, "", sends, topic_id="t_a") == 2
        assert await db.count_autoreplies(ALICE, "", "skipped", from_addr="bo@ex.com",
                                          reasons=("cap_flood",)) == 1
        assert await db.count_autoreplies(ALICE, "", None, reasons="cap_flood") == 1
        assert await db.count_autoreplies(ALICE, "", None, reasons=("burst",)) == 0
        with pytest.raises(ValueError):
            await db.count_autoreplies(ALICE, "", ("sent", "sendng"))

    async def test_count_since_includes_rows_changed_in_the_window(self, db):
        # A held reply the owner approves days after it arrived counts from
        # the moment it went out — the thread cap sees it.
        rid = await _row_in(db, ALICE, "m1", "held", reply_text="hi")
        await _stamp(db, rid, created=NOW - timedelta(days=3), updated=NOW - timedelta(days=3))
        day_ago = (NOW - timedelta(days=1)).isoformat()
        sends = dbmod.AUTOREPLY_SEND_STATES
        assert await db.count_autoreplies(ALICE, day_ago, sends, thread_id="t-m1") == 0
        await db._db.execute("UPDATE gmail_autoreplies SET state = 'approved' WHERE id = ?",
                             (rid,))
        assert await db.cas_autoreply(rid, ALICE, ("approved",), "sent", sent_message_id="g1")
        assert await db.count_autoreplies(ALICE, day_ago, sends, thread_id="t-m1") == 1

    async def test_list_newest_first_hides_quiet_skips(self, db):
        made = {}
        for i, (state, reason) in enumerate([
                ("skipped", "labels"), ("skipped", "no_match"), ("skipped", "burst"),
                ("skipped", "cap_daily"), ("held", "judge:off_topic"), ("sent", ""),
                ("failed", "fetch")]):
            rid = await _row_in(db, ALICE, f"m{i}", state, reason=reason)
            await _stamp(db, rid, created=NOW + timedelta(minutes=i))
            made[f"m{i}"] = rid
        await db.claim_autoreply(BOB, "mb", "t")
        shown = await db.list_autoreplies(ALICE)
        assert [r["inbound_message_id"] for r in shown] == ["m6", "m5", "m4", "m3"]
        everything = await db.list_autoreplies(ALICE, 50, True)
        assert len(everything) == 7 and everything[0]["inbound_message_id"] == "m6"
        custom = await db.list_autoreplies(ALICE, 50, False, ("cap_daily",))
        assert "m3" not in {r["inbound_message_id"] for r in custom} and len(custom) == 6
        assert [r["inbound_message_id"] for r in await db.list_autoreplies(ALICE, 2)] \
            == ["m6", "m5"]
        assert len(await db.list_autoreplies(ALICE, 0)) == 1     # clamped to 1
        held = await db.list_autoreplies(ALICE, 50, include_quiet=True, states=("held",))
        assert [r["id"] for r in held] == [made["m4"]]
        recent = await db.list_autoreplies(
            ALICE, 50, True, since_iso=(NOW + timedelta(minutes=5)).isoformat())
        assert [r["inbound_message_id"] for r in recent] == ["m6", "m5"]
        assert all(r["owner_id"] == ALICE for r in everything)

    async def test_sent_ids_and_match_stats(self, db):
        await _row_in(db, ALICE, "m1", "sent", sent_message_id="g1", thread_id="T")
        await _row_in(db, ALICE, "m2", "sent", sent_message_id="g2", thread_id="T")
        await _row_in(db, ALICE, "m3", "unknown", thread_id="T")
        await _row_in(db, BOB, "m4", "sent", sent_message_id="gB", thread_id="T")
        assert await db.autoreply_sent_ids(ALICE, "T") == {"g1", "g2"}
        assert await db.autoreply_sent_ids(ALICE, "") == set()

        for i, (judge, topic) in enumerate([("tier", "t_a"), ("jev", "t_a"), ("tier", ""),
                                            ("tier", "t_b"), ("", "")]):
            await _row_in(db, ALICE, f"s{i}", "skipped", match_judge=judge, topic_id=topic)
        old = await _row_in(db, ALICE, "s9", "sent", match_judge="tier", topic_id="t_a")
        await _stamp(db, old, created=NOW - timedelta(days=30))
        stats = await db.autoreply_match_stats(ALICE, (NOW - timedelta(days=7)).isoformat())
        assert stats == {"classified": 4, "matched": {"t_a": 2, "t_b": 1}}
        assert await db.autoreply_match_stats(BOB, "") == {"classified": 0, "matched": {}}


class TestRecover:
    async def test_the_restart_table(self, db):
        sending = await _row_in(db, ALICE, "m1", "sending", reply_text="maybe sent")
        bob_sending = await _row_in(db, BOB, "m1", "sending")
        requeue = {s: await _row_in(db, ALICE, f"q-{s}", s, reply_text="unchecked")
                   for s in dbmod.AUTOREPLY_WORK_STATES}
        spent = {s: await _row_in(db, ALICE, f"x-{s}", s, attempts=2, reply_text="t")
                 for s in dbmod.AUTOREPLY_WORK_STATES}
        owner_ok = await _row_in(db, ALICE, "h1", "approved", approved_by=ALICE,
                                 reply_text="edited by Alice", attempts=5,
                                 reason="judge:off_topic")
        new = await _row_in(db, ALICE, "n1", "new", attempts=1)
        waiting = await _row_in(db, ALICE, "w1", "waiting_agent")
        terminal = {s: await _row_in(db, ALICE, f"z-{s}", s, reply_text="kept")
                    for s in ("sent", "unknown", "held", "skipped", "failed", "blocked")}

        moved = await db.recover_autoreplies()
        assert {r["id"] for r in moved} == {sending, bob_sending}
        assert all(r["state"] == "unknown" and r["reason"] == "interrupted" for r in moved)

        row = await db.get_autoreply(ALICE, sending)
        assert row["state"] == "unknown" and row["reply_text"] == "maybe sent"
        for s, rid in requeue.items():
            row = await db.get_autoreply(ALICE, rid)
            assert (row["state"], row["attempts"], row["reply_text"]) == ("new", 1, ""), s
        for s, rid in spent.items():
            row = await db.get_autoreply(ALICE, rid)
            assert (row["state"], row["reason"]) == ("failed", "interrupted"), s
            assert row["detail"] == f"interrupted while {s}" and row["reply_text"] == ""
            assert row["attempts"] == 2
        row = await db.get_autoreply(ALICE, owner_ok)
        assert (row["state"], row["approved_by"]) == ("held", "")
        assert row["reply_text"] == "edited by Alice" and row["reason"] == "judge:off_topic"
        assert (await db.get_autoreply(ALICE, new))["attempts"] == 1
        assert await _state_of(db, ALICE, new) == "new"
        assert await _state_of(db, ALICE, waiting) == "waiting_agent"
        for s, rid in terminal.items():
            row = await db.get_autoreply(ALICE, rid)
            assert (row["state"], row["reply_text"]) == (s, "kept")

        # A second boot moves nothing that already settled.
        assert await db.recover_autoreplies() == []
        for rid in requeue.values():
            assert await _state_of(db, ALICE, rid) == "new"

    async def test_requeued_twice_then_failed(self, db):
        rid = await _row_in(db, ALICE, "m1", "writing")
        await db.recover_autoreplies()
        row = await db.get_autoreply(ALICE, rid)
        assert (row["state"], row["attempts"]) == ("new", 1)
        await db._db.execute("UPDATE gmail_autoreplies SET state = 'checking' WHERE id = ?",
                             (rid,))
        await db.recover_autoreplies()
        assert (await db.get_autoreply(ALICE, rid))["attempts"] == 2
        await db._db.execute("UPDATE gmail_autoreplies SET state = 'approved' WHERE id = ?",
                             (rid,))
        await db.recover_autoreplies()
        row = await db.get_autoreply(ALICE, rid)
        assert (row["state"], row["reason"]) == ("failed", "interrupted")

    async def test_unknown_rows_count_toward_the_caps(self, db):
        await _row_in(db, ALICE, "m1", "sending", from_addr="ana@ex.com")
        await db.recover_autoreplies()
        assert await db.count_autoreplies(ALICE, (NOW - timedelta(days=1)).isoformat(),
                                          dbmod.AUTOREPLY_SEND_STATES,
                                          from_addr="ana@ex.com") == 1


class TestPurge:
    async def test_retention(self, db):
        def ago(days, hours=0):
            return NOW - timedelta(days=days, hours=hours)

        plan = {
            "held_fresh": ("held", ago(6), "proposal"),
            "held_old": ("held", ago(8), "proposal"),
            "sent_fresh": ("sent", ago(13), "reply"),
            "sent_15": ("sent", ago(15), "reply"),
            "skip_29": ("skipped", ago(29), ""),
            "skip_31": ("skipped", ago(31), ""),
            "fail_31": ("failed", ago(31), ""),
            "blocked_31": ("blocked", ago(31), ""),
            "sent_89": ("sent", ago(89), "reply"),
            "sent_91": ("sent", ago(91), "reply"),
            "unknown_91": ("unknown", ago(91), ""),
        }
        ids = {}
        for name, (state, created, text) in plan.items():
            rid = await _row_in(db, ALICE, name, state, reply_text=text)
            await _stamp(db, rid, created=created, updated=created)
            ids[name] = rid
        bob_old = await _row_in(db, BOB, "b", "skipped")
        await _stamp(db, bob_old, created=ago(40))

        got = await db.purge_autoreplies(NOW)
        # deleted: skip_31, fail_31, Bob's (30-day rule) + sent_91, unknown_91 (90-day)
        assert got == {"expired": 1, "cleared": 3, "deleted": 5}

        assert await db.get_autoreply(BOB, bob_old) is None    # every owner
        for gone in ("skip_31", "fail_31", "sent_91", "unknown_91"):
            assert await db.get_autoreply(ALICE, ids[gone]) is None, gone
        held_old = await db.get_autoreply(ALICE, ids["held_old"])
        assert (held_old["state"], held_old["reason"]) == ("dismissed", "expired")
        assert held_old["reply_text"] == "proposal"            # < 14 days: text kept
        assert await _state_of(db, ALICE, ids["held_fresh"]) == "held"
        assert (await db.get_autoreply(ALICE, ids["sent_fresh"]))["reply_text"] == "reply"
        sent_15 = await db.get_autoreply(ALICE, ids["sent_15"])
        assert sent_15["reply_text"] == "" and sent_15["state"] == "sent"
        # Clearing text leaves updated_at alone: an old send stays out of the caps.
        assert sent_15["updated_at"] == ago(15).isoformat()
        assert (await db.get_autoreply(ALICE, ids["sent_89"]))["reply_text"] == ""
        assert (await db.get_autoreply(ALICE, ids["skip_29"])) is not None
        assert (await db.get_autoreply(ALICE, ids["blocked_31"])) is not None

        assert await db.purge_autoreplies(NOW) == {"expired": 0, "cleared": 0, "deleted": 0}

    async def test_now_forms(self, db):
        rid = await _row_in(db, ALICE, "m1", "skipped")
        await _stamp(db, rid, created=NOW - timedelta(days=31))
        naive = (NOW - timedelta(days=2)).replace(tzinfo=None)    # naive = UTC
        assert (await db.purge_autoreplies(naive))["deleted"] == 0
        assert (await db.purge_autoreplies(NOW.isoformat()))["deleted"] == 1
        rid = await _row_in(db, ALICE, "m2", "skipped")
        await _stamp(db, rid, created=datetime.now(timezone.utc) - timedelta(days=31))
        assert (await db.purge_autoreplies())["deleted"] == 1


class TestNotificationCount:
    async def test_counts_by_type_ref_and_window(self, db):
        await db.add_notification(ALICE, "email_autoreply_issue", "t", "", "gmail_autoreply",
                                  "agent_offline")
        await db.add_notification(ALICE, "email_autoreply_issue", "t", "", "gmail_autoreply",
                                  "missing_scopes")
        await db.add_notification(ALICE, "email_autoreply_held", "t", "", "gmail_autoreply",
                                  "ar_1")
        await db.add_notification(ALICE, "email_sent", "t")
        await db.add_notification(BOB, "email_autoreply_held", "t")
        await db.mark_all_notifications_read(ALICE)              # read ones still count
        since = "2000-01-01T00:00:00+00:00"
        assert await db.count_notifications_since(ALICE, since) == 4
        assert await db.count_notifications_since(
            ALICE, since, ("email_autoreply_held", "email_autoreply_blocked")) == 1
        assert await db.count_notifications_since(
            ALICE, since, "email_autoreply_issue", ref_id="agent_offline") == 1
        assert await db.count_notifications_since(
            ALICE, since, ref_type="gmail_autoreply") == 3
        assert await db.count_notifications_since(ALICE, "2999-01-01T00:00:00+00:00") == 0
