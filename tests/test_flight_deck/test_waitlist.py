"""Waitlist — applying while self-registration is closed
(``POST /fd/auth/waitlist``) and an admin deciding (``/fd/admin/waitlist``).

An applicant gives their name, email and password plus the name and email of
whoever recommended them; approving creates the account with that password.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from captain_claw.flight_deck import admin_routes, auth_routes
from captain_claw.flight_deck.auth import get_current_user, hash_password, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

LOOPBACK = ("127.0.0.1", 40001)

ADMIN = {"id": "admin-1", "role": "admin"}

APPLICATION = {
    "email": "Newbie@Example.com", "password": "hunter22", "display_name": "New Bie",
    "referrer_name": "Ref Erer", "referrer_email": "ref@example.com",
}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=LOOPBACK),
        base_url="http://fd.test")


@pytest.fixture
async def fd_db(tmp_path: Path, monkeypatch):
    from captain_claw.flight_deck import auth as fd_auth
    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "fd.db")
    await db.init()
    set_auth_db(db)
    monkeypatch.delenv("FD_REGISTRATION_OPEN", raising=False)
    monkeypatch.delenv("FD_REGISTRATION_DISABLED", raising=False)
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setattr(auth_routes, "_waitlist_limiter", auth_routes._PairLimiter())
    # An existing admin: registration is closed from here on.
    await db.create_user("boss@example.com", hash_password("boss-pass"), "Boss", "admin")
    try:
        yield db
    finally:
        await db.close()
        fd_auth._db = prev


def _app(user: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.include_router(auth_routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


async def _apply(c, **overrides):
    return await c.post("/fd/auth/waitlist", json={**APPLICATION, **overrides})


# ── Applying ──

async def test_apply_then_approve_then_sign_in(fd_db):
    async with _client(_app(ADMIN)) as c:
        r = await _apply(c)
        assert r.status_code == 201, r.text
        assert r.json()["cohort"] == "Flight Deck"  # the instance name

        # Still waiting: the right password hears so, a wrong one doesn't.
        r = await c.post("/fd/auth/login", json={"email": "newbie@example.com", "password": "hunter22"})
        assert r.status_code == 403
        assert "waiting" in r.json()["detail"]
        r = await c.post("/fd/auth/login", json={"email": "newbie@example.com", "password": "nope-nope"})
        assert r.status_code == 401

        r = await c.get("/fd/admin/waitlist")
        assert r.status_code == 200
        body = r.json()
        assert body["pending"] == 1
        [entry] = body["entries"]
        assert entry["email"] == "newbie@example.com"
        assert entry["display_name"] == "New Bie"
        assert entry["referrer_name"] == "Ref Erer"
        assert entry["referrer_email"] == "ref@example.com"
        assert entry["cohort"] == "Flight Deck"
        assert "password_hash" not in entry

        r = await c.post(f"/fd/admin/waitlist/{entry['id']}/approve", json={})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email"] == "newbie@example.com"

        r = await c.post("/fd/auth/login", json={"email": "newbie@example.com", "password": "hunter22"})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["display_name"] == "New Bie"
        assert r.json()["user"]["role"] == "user"

    row = await fd_db.get_waitlist_entry(entry["id"])
    assert row["status"] == "approved"
    assert row["password_hash"] == ""  # not kept once decided
    assert row["decided_by"] == "admin-1"
    assert row["user_id"]
    assert await fd_db.count_waitlist("pending") == 0


@pytest.mark.parametrize("overrides, fragment", [
    ({"display_name": "  "}, "Your name"),
    ({"email": "not-an-email"}, "valid email"),
    ({"password": "short"}, "6 characters"),
    ({"password": "x" * 73}, "too long"),
    ({"referrer_name": ""}, "recommended you is required"),
    ({"referrer_email": "nobody"}, "recommended you"),
    ({"referrer_email": "NEWBIE@example.com"}, "someone else"),
])
async def test_apply_validation(fd_db, overrides, fragment):
    async with _client(_app()) as c:
        r = await _apply(c, **overrides)
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]
    assert await fd_db.count_waitlist("pending") == 0


async def test_duplicate_pending_and_existing_account(fd_db):
    async with _client(_app()) as c:
        assert (await _apply(c)).status_code == 201
        r = await _apply(c, password="other-pass")
        assert r.status_code == 409
        assert "already on the waitlist" in r.json()["detail"]
        r = await _apply(c, email="boss@example.com")
        assert r.status_code == 409
        assert "already has an account" in r.json()["detail"]
    # The first application's password still stands (no overwrite).
    entry = await fd_db.get_pending_waitlist_entry("newbie@example.com")
    from captain_claw.flight_deck.auth import verify_password
    assert verify_password("hunter22", entry["password_hash"])


async def test_rejected_can_apply_again(fd_db):
    async with _client(_app(ADMIN)) as c:
        await _apply(c)
        entry = (await c.get("/fd/admin/waitlist")).json()["entries"][0]
        r = await c.post(f"/fd/admin/waitlist/{entry['id']}/reject")
        assert r.status_code == 200
        # Decided: can't be approved or rejected again.
        assert (await c.post(f"/fd/admin/waitlist/{entry['id']}/approve", json={})).status_code == 409
        assert (await c.post(f"/fd/admin/waitlist/{entry['id']}/reject")).status_code == 409
        # Rejected: no pending hint at sign-in, no account.
        r = await c.post("/fd/auth/login", json={"email": "newbie@example.com", "password": "hunter22"})
        assert r.status_code == 401
        assert (await _apply(c)).status_code == 201
        all_ = (await c.get("/fd/admin/waitlist", params={"status": "all"})).json()
    assert [e["status"] for e in all_["entries"]] == ["pending", "rejected"]
    assert all_["pending"] == 1
    # Every status counted, whichever one is listed (the Users page's tabs).
    assert all_["counts"] == {"pending": 1, "approved": 0, "rejected": 1}
    assert await fd_db.get_user_by_email("newbie@example.com") is None


async def test_delete_application(fd_db):
    async with _client(_app(ADMIN)) as c:
        await _apply(c)
        entry = (await c.get("/fd/admin/waitlist")).json()["entries"][0]
        assert (await c.delete(f"/fd/admin/waitlist/{entry['id']}")).status_code == 200
        assert (await c.delete(f"/fd/admin/waitlist/{entry['id']}")).status_code == 404
    assert await fd_db.count_waitlist("pending") == 0


async def test_approve_when_account_exists_meanwhile(fd_db):
    async with _client(_app(ADMIN)) as c:
        await _apply(c)
        entry = (await c.get("/fd/admin/waitlist")).json()["entries"][0]
        await fd_db.create_user("newbie@example.com", hash_password("x" * 8), "Made by hand", "user")
        r = await c.post(f"/fd/admin/waitlist/{entry['id']}/approve", json={})
    assert r.status_code == 409
    assert (await fd_db.get_waitlist_entry(entry["id"]))["status"] == "pending"


async def test_approve_follows_kiosk_default_and_override(fd_db):
    await fd_db.set_system_setting("fd:system-config", json.dumps({"kiosk_new_users": True}))
    async with _client(_app(ADMIN)) as c:
        await _apply(c)
        await _apply(c, email="second@example.com")
        entries = {e["email"]: e for e in (await c.get("/fd/admin/waitlist")).json()["entries"]}
        r1 = await c.post(f"/fd/admin/waitlist/{entries['newbie@example.com']['id']}/approve", json={})
        r2 = await c.post(f"/fd/admin/waitlist/{entries['second@example.com']['id']}/approve",
                          json={"kiosk": False})
    assert r1.json()["user"]["kiosk"] is True
    assert r2.json()["user"]["kiosk"] is False


# ── When the waitlist is not offered ──

async def test_waitlist_off(fd_db):
    await fd_db.set_system_setting("fd:system-config", json.dumps({"waitlist_enabled": False}))
    async with _client(_app()) as c:
        r = await _apply(c)
        assert r.status_code == 403
        r = await c.post("/fd/auth/register", json={"email": "x@example.com", "password": "abcdefg"})
        assert "Ask an administrator" in r.json()["detail"]


async def test_registration_open_refuses_waitlist(fd_db, monkeypatch):
    monkeypatch.setenv("FD_REGISTRATION_OPEN", "1")
    async with _client(_app()) as c:
        r = await _apply(c)
    assert r.status_code == 409
    assert "Registration is open" in r.json()["detail"]


async def test_register_closed_points_at_waitlist(fd_db):
    async with _client(_app()) as c:
        r = await c.post("/fd/auth/register", json={"email": "x@example.com", "password": "abcdefg"})
    assert r.status_code == 403
    assert "waitlist" in r.json()["detail"]


async def test_cohort_name_is_admin_set(fd_db):
    async with _client(_app(ADMIN)) as c:
        r = await c.put("/fd/admin/config", json={"waitlist_cohort": "  CC   Cohort 002 "})
        assert r.status_code == 200
        assert r.json()["waitlist_cohort"] == "CC Cohort 002"
        r = await _apply(c)
        assert r.json()["cohort"] == "CC Cohort 002"
        entry = (await c.get("/fd/admin/waitlist")).json()["entries"][0]
    assert entry["cohort"] == "CC Cohort 002"


async def test_cohort_follows_the_instance_name(fd_db):
    async with _client(_app(ADMIN)) as c:
        await c.put("/fd/admin/config", json={"instance_name": "Northwind Agents"})
        r = await _apply(c, email="one@example.com")
        assert r.json()["cohort"] == "Northwind Agents"

        # A named cohort wins; blank goes back to the instance name.
        await c.put("/fd/admin/config", json={"waitlist_cohort": "Spring intake"})
        assert (await _apply(c, email="two@example.com")).json()["cohort"] == "Spring intake"
        r = await c.put("/fd/admin/config", json={"waitlist_cohort": "   "})
        assert r.status_code == 200 and r.json()["waitlist_cohort"] == ""
        assert (await _apply(c, email="three@example.com")).json()["cohort"] == "Northwind Agents"

    # The old built-in default, saved as is on an older deck, counts as not set.
    raw = json.loads(await fd_db.get_system_setting("fd:system-config"))
    await fd_db.set_system_setting(
        "fd:system-config", json.dumps({**raw, "waitlist_cohort": "CC Cohort 001"}))
    assert (await admin_routes.waitlist_settings(fd_db))["cohort"] == "Northwind Agents"


async def test_rate_limited_per_network(fd_db, monkeypatch):
    monkeypatch.setattr(auth_routes, "WAITLIST_APPLY_LIMIT", (2, 3600.0))
    async with _client(_app()) as c:
        assert (await _apply(c, email="a1@example.com")).status_code == 201
        assert (await _apply(c, email="a2@example.com")).status_code == 201
        assert (await _apply(c, email="a3@example.com")).status_code == 429
        # Bad input doesn't spend (or get stopped by) the budget.
        assert (await _apply(c, email="bad")).status_code == 400


async def test_admin_only(fd_db):
    async with _client(_app({"id": "u-1", "role": "user"})) as c:
        assert (await c.get("/fd/admin/waitlist")).status_code == 403
        await _apply(c)
        entry = await fd_db.get_pending_waitlist_entry("newbie@example.com")
        assert (await c.post(f"/fd/admin/waitlist/{entry['id']}/approve", json={})).status_code == 403
        assert (await c.post(f"/fd/admin/waitlist/{entry['id']}/reject")).status_code == 403
        assert (await c.delete(f"/fd/admin/waitlist/{entry['id']}")).status_code == 403
    assert await fd_db.count_waitlist("pending") == 1
