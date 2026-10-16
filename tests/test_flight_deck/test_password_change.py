"""Password changes — a user's own (``PUT /fd/auth/me``) and an admin's reset
(``PUT /fd/admin/users/{id}``), each with an optional sign-out of the user's
other sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from captain_claw.flight_deck import admin_routes, auth_routes
from captain_claw.flight_deck.auth import (
    REFRESH_COOKIE, get_current_user, hash_password, hash_token, set_auth_db, verify_password,
)
from captain_claw.flight_deck.db import FlightDeckDB

LOOPBACK = ("127.0.0.1", 40001)


def _client(app, cookies: dict | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=LOOPBACK),
        base_url="http://fd.test", cookies=cookies)


@pytest.fixture
async def fd_db(tmp_path: Path, monkeypatch):
    from captain_claw.flight_deck import auth as fd_auth
    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "fd.db")
    await db.init()
    set_auth_db(db)
    monkeypatch.setattr(auth_routes, "_password_change_limiter", auth_routes._PairLimiter())
    try:
        yield db
    finally:
        await db.close()
        fd_auth._db = prev


def _app(user: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return app


async def _user(db, email: str, role: str = "user") -> dict:
    await db.create_user(email, hash_password("old-pass"), email.split("@")[0], role)
    return await db.get_user_by_email(email)


async def _sessions(db, user_id: str, n: int) -> list[str]:
    """n refresh sessions for the user; returns their raw refresh tokens."""
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    tokens = [f"tok-{user_id}-{i}" for i in range(n)]
    for t in tokens:
        await db.create_refresh_session(user_id, hash_token(t), expires)
    return tokens


async def _session_hashes(db, user_id: str) -> set[str]:
    async with db._db.execute(
        "SELECT refresh_token_hash FROM user_sessions WHERE user_id = ?", (user_id,)
    ) as cur:
        return {r[0] for r in await cur.fetchall()}


# ── Own password ──

async def test_user_changes_own_password(fd_db):
    u = await _user(fd_db, "u@x.test")
    tokens = await _sessions(fd_db, u["id"], 3)
    async with _client(_app(u), cookies={REFRESH_COOKIE: tokens[0]}) as c:
        r = await c.put("/fd/auth/me", json={"current_password": "old-pass",
                                             "password": "new-pass"})
    assert r.status_code == 200, r.text
    row = await fd_db.get_user_by_email("u@x.test")
    assert verify_password("new-pass", row["password_hash"])
    assert len(await _session_hashes(fd_db, u["id"])) == 3  # nobody signed out


async def test_sign_out_others_keeps_own_session(fd_db):
    u = await _user(fd_db, "u@x.test")
    tokens = await _sessions(fd_db, u["id"], 3)
    async with _client(_app(u), cookies={REFRESH_COOKIE: tokens[1]}) as c:
        r = await c.put("/fd/auth/me", json={"current_password": "old-pass",
                                             "password": "new-pass",
                                             "sign_out_others": True})
    assert r.status_code == 200, r.text
    assert await _session_hashes(fd_db, u["id"]) == {hash_token(tokens[1])}


async def test_wrong_current_password_changes_nothing(fd_db):
    u = await _user(fd_db, "u@x.test")
    tokens = await _sessions(fd_db, u["id"], 2)
    async with _client(_app(u), cookies={REFRESH_COOKIE: tokens[0]}) as c:
        r = await c.put("/fd/auth/me", json={"current_password": "nope",
                                             "password": "new-pass",
                                             "sign_out_others": True})
        assert r.status_code == 400
        r = await c.put("/fd/auth/me", json={"password": "new-pass"})
        assert r.status_code == 400  # current password required
        r = await c.put("/fd/auth/me", json={"current_password": "old-pass", "password": "12345"})
        assert r.status_code == 400  # too short
    row = await fd_db.get_user_by_email("u@x.test")
    assert verify_password("old-pass", row["password_hash"])
    assert len(await _session_hashes(fd_db, u["id"])) == 2


async def test_guessing_current_password_is_rate_limited(fd_db):
    u = await _user(fd_db, "u@x.test")
    limit = auth_routes.PASSWORD_CHANGE_LIMIT[0]
    async with _client(_app(u)) as c:
        for _ in range(limit):
            r = await c.put("/fd/auth/me", json={"current_password": "guess", "password": "new-pass"})
            assert r.status_code == 400
        r = await c.put("/fd/auth/me", json={"current_password": "old-pass", "password": "new-pass"})
    assert r.status_code == 429
    row = await fd_db.get_user_by_email("u@x.test")
    assert verify_password("old-pass", row["password_hash"])


# ── Admin reset ──

async def test_admin_sets_password_and_signs_user_out(fd_db):
    admin = await _user(fd_db, "boss@x.test", role="admin")
    u = await _user(fd_db, "u@x.test")
    await _sessions(fd_db, u["id"], 2)
    async with _client(_app(admin)) as c:
        r = await c.put(f"/fd/admin/users/{u['id']}", json={"password": "reset-pass",
                                                             "sign_out": True})
    assert r.status_code == 200, r.text
    row = await fd_db.get_user_by_email("u@x.test")
    assert verify_password("reset-pass", row["password_hash"])
    assert await _session_hashes(fd_db, u["id"]) == set()


async def test_admin_reset_without_sign_out_keeps_sessions(fd_db):
    admin = await _user(fd_db, "boss@x.test", role="admin")
    u = await _user(fd_db, "u@x.test")
    await _sessions(fd_db, u["id"], 2)
    async with _client(_app(admin)) as c:
        r = await c.put(f"/fd/admin/users/{u['id']}", json={"password": "reset-pass"})
        assert r.status_code == 200, r.text
        r = await c.put(f"/fd/admin/users/{u['id']}", json={"password": "12345"})
    assert r.status_code == 400
    assert len(await _session_hashes(fd_db, u["id"])) == 2


async def test_admin_never_signs_out_own_session(fd_db):
    admin = await _user(fd_db, "boss@x.test", role="admin")
    await _sessions(fd_db, admin["id"], 1)
    async with _client(_app(admin)) as c:
        r = await c.put(f"/fd/admin/users/{admin['id']}", json={"password": "reset-pass",
                                                                 "sign_out": True})
    assert r.status_code == 200, r.text
    assert len(await _session_hashes(fd_db, admin["id"])) == 1


async def test_non_admin_cannot_reset_others(fd_db):
    u = await _user(fd_db, "u@x.test")
    other = await _user(fd_db, "o@x.test")
    async with _client(_app(u)) as c:
        r = await c.put(f"/fd/admin/users/{other['id']}", json={"password": "pwned1"})
    assert r.status_code == 403
    row = await fd_db.get_user_by_email("o@x.test")
    assert verify_password("old-pass", row["password_hash"])
