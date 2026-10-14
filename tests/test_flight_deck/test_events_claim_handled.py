"""EventsStore.claim_handled — the spine claim for mail Flight Deck already handled.

A Gmail message the topic auto-reply engine answered (or put in front of the
owner) is marked ``acted`` on the event spine, so the event poller's later
``add_event`` of it is deduped and the arbiter never nudges about it. An event
already surfaced, acted on or ignored is left alone. A claim that comes before
the poller writes a placeholder: the poller's event later fills it in (still
handled), and until then the listings (GET /fd/events, the Autonomous Work
page) leave it out — it has nothing to show.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import event_routes, events
from captain_claw.flight_deck.events import EventsStore

KEY = "gmail:m-123"
REAL = {"source": "gmail", "event_type": "new_email",
        "summary": "Email from Ana <ana@example.com>: Pricing",
        "metadata": {"message_id": "m-123", "thread_id": "th-1", "from": "ana@example.com",
                     "subject": "Pricing"},
        "dedup_key": KEY}


@pytest.fixture()
def store(tmp_path):
    return EventsStore(tmp_path / "events.db")


def _rows(store, user_id="alice"):
    """Every stored row of *user_id*, placeholders included."""
    with store._lock:
        rows = store._c().execute(
            "SELECT * FROM external_events WHERE user_id = ? ORDER BY ingested_at DESC",
            (user_id,)).fetchall()
    return [store._row(r) for r in rows]


class TestClaimHandled:
    def test_claim_first_dedups_the_later_event(self, store):
        assert store.claim_handled("alice", "gmail", KEY) is True
        rows = _rows(store)
        assert len(rows) == 1
        assert (rows[0]["source"], rows[0]["dedup_key"], rows[0]["status"]) == (
            "gmail", KEY, "acted")
        assert rows[0]["processed_at"] and rows[0]["ingested_at"]
        assert rows[0]["id"].startswith("evt_") and rows[0]["metadata"] == {}
        # The poller sees it later: deduped, and never in the arbiter's intake.
        assert store.add_event("alice", source="gmail", event_type="new_email",
                               summary="Email from x", dedup_key=KEY) is None
        assert store.list_new("alice") == []
        assert len(_rows(store)) == 1

    def test_claim_after_ingest_flips_new_to_acted(self, store):
        evt = store.add_event("alice", source="gmail", event_type="new_email",
                              summary="Email from x", dedup_key=KEY)
        assert [e["id"] for e in store.list_new("alice")] == [evt["id"]]
        assert store.claim_handled("alice", "gmail", KEY) is True
        got = store.get_event(evt["id"])
        assert got["status"] == "acted" and got["processed_at"]
        assert got["summary"] == "Email from x"  # the event itself is untouched
        assert store.list_new("alice") == []

    @pytest.mark.parametrize("status", ["surfaced", "acted", "ignored"])
    def test_an_event_past_new_is_left_alone(self, store, status):
        evt = store.add_event("alice", source="gmail", summary="s", dedup_key=KEY)
        store.mark([evt["id"]], status)
        before = store.get_event(evt["id"])
        assert store.claim_handled("alice", "gmail", KEY) is False
        assert store.get_event(evt["id"]) == before

    def test_a_second_claim_is_a_no_op(self, store):
        assert store.claim_handled("alice", "gmail", KEY) is True
        assert store.claim_handled("alice", "gmail", KEY) is False
        assert len(_rows(store)) == 1

    def test_scoped_per_user_and_source(self, store):
        evt_bob = store.add_event("bob", source="gmail", summary="s", dedup_key=KEY)
        evt_cal = store.add_event("alice", source="calendar", summary="s", dedup_key=KEY)
        assert store.claim_handled("alice", "gmail", KEY) is True
        assert store.get_event(evt_bob["id"])["status"] == "new"
        assert store.get_event(evt_cal["id"])["status"] == "new"
        assert [(e["source"], e["dedup_key"]) for e in _rows(store)
                if e["status"] == "acted"] == [("gmail", KEY)]

    def test_an_empty_key_claims_nothing(self, store):
        assert store.claim_handled("alice", "gmail", "") is False
        assert _rows(store) == []

    def test_blank_user_is_the_local_user_like_add_event(self, store):
        assert store.claim_handled("", "gmail", KEY) is True
        assert store.add_event("local", source="gmail", summary="s", dedup_key=KEY) is None


class TestPlaceholder:
    """A claim before the poller never costs the real event (finding 26)."""

    def test_the_pollers_event_fills_in_the_placeholder(self, store):
        assert store.claim_handled("alice", "gmail", KEY) is True
        # Still deduped: the arbiter never sees it, nothing new is ingested.
        assert store.add_event("alice", **REAL, body="Hi, what does it cost?") is None
        assert store.list_new("alice") == []
        (row,) = _rows(store)
        assert (row["status"], row["event_type"], row["summary"], row["body"]) == (
            "acted", "new_email", REAL["summary"], "Hi, what does it cost?")
        assert row["metadata"] == REAL["metadata"] and row["processed_at"]
        # Listed like any event the engine handled — with what it was about.
        (listed,) = store.list_events("alice")
        assert (listed["summary"], listed["status"]) == (REAL["summary"], "acted")

    def test_a_placeholder_is_left_out_of_every_listing(self, store):
        store.add_event("alice", source="calendar", event_type="event_changed",
                        summary="Standup moved", dedup_key="cal:1")
        assert store.claim_handled("alice", "gmail", KEY) is True
        assert [e["summary"] for e in store.list_events("alice")] == ["Standup moved"]
        assert store.list_events("alice", status="acted") == []
        assert len(_rows(store)) == 2

    def test_get_fd_events_never_shows_a_blank_row(self, store, monkeypatch):
        # GET /fd/events?limit=50 is what the Autonomous Work page lists.
        monkeypatch.setenv("FD_AUTH_ENABLED", "false")
        monkeypatch.setattr(events, "_STORE", store)
        app = FastAPI()
        app.include_router(event_routes.router)
        client = TestClient(app)
        assert store.claim_handled("", "gmail", KEY) is True
        assert client.get("/fd/events?limit=50").json() == {"events": []}
        assert store.add_event("local", **REAL) is None
        (shown,) = client.get("/fd/events?limit=50").json()["events"]
        assert (shown["source"], shown["summary"], shown["status"]) == (
            "gmail", REAL["summary"], "acted")

    def test_a_real_event_is_never_overwritten(self, store):
        evt = store.add_event("alice", **REAL)
        assert store.claim_handled("alice", "gmail", KEY) is True
        assert store.add_event("alice", source="gmail", event_type="new_email",
                               summary="Something else", dedup_key=KEY) is None
        got = store.get_event(evt["id"])
        assert (got["summary"], got["metadata"], got["status"]) == (
            REAL["summary"], REAL["metadata"], "acted")
        assert len(_rows(store)) == 1

    def test_a_blank_add_leaves_the_placeholder_as_it_is(self, store):
        assert store.claim_handled("alice", "gmail", KEY) is True
        before = _rows(store)
        assert store.add_event("alice", source="gmail", dedup_key=KEY) is None
        assert _rows(store) == before
