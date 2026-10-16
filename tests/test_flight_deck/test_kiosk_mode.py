"""Kiosk (simple-chat) lock — deck mode + per-user flag.

An admin picks the deck's kiosk mode in Admin → Settings: ``off``, ``all``
(every non-admin) or ``selected`` (users marked in Admin → Users), plus whether
new accounts start marked. Until a mode is picked, CLI ``--simple-chat`` /
``FD_SIMPLE_CHAT`` is the default. The resolved lock travels with the user
(``kiosk``) on login / refresh / me; admins are never locked.
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


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=LOOPBACK),
        base_url="http://fd.test")


@pytest.fixture
async def fd_db(tmp_path: Path, monkeypatch):
    from captain_claw.flight_deck import auth as fd_auth
    monkeypatch.delenv("FD_SIMPLE_CHAT", raising=False)
    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "fd.db")
    await db.init()
    set_auth_db(db)
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


async def _set_config(db, **cfg) -> None:
    await db.set_system_setting("fd:system-config", json.dumps(cfg))


async def _login(db, email: str) -> dict:
    app = FastAPI()
    app.include_router(auth_routes.router)
    async with _client(app) as c:
        r = await c.post("/fd/auth/login", json={"email": email, "password": "sixchars"})
    assert r.status_code == 200, r.text
    return r.json()["user"]


async def _user(db, email: str, role: str = "user", kiosk: bool = False) -> dict:
    await db.create_user(email, hash_password("sixchars"), email.split("@")[0], role,
                         metadata={"kiosk": True} if kiosk else None)
    return await db.get_user_by_email(email)


# ── Resolution ──

def test_kiosk_locked_by_mode():
    marked = {"role": "user", "metadata": json.dumps({"kiosk": True})}
    plain = {"role": "user", "metadata": "{}"}
    admin = {"role": "admin", "metadata": json.dumps({"kiosk": True})}
    for mode, want in (("off", (False, False)), ("all", (True, True)),
                       ("selected", (True, False))):
        cfg = {"kiosk_mode": mode}
        assert (admin_routes.kiosk_locked(marked, cfg),
                admin_routes.kiosk_locked(plain, cfg)) == want, mode
        assert admin_routes.kiosk_locked(admin, cfg) is False, mode


def test_env_flag_is_the_default_until_picked(monkeypatch):
    monkeypatch.delenv("FD_SIMPLE_CHAT", raising=False)
    assert admin_routes._load_system_config(None)["kiosk_mode"] == "off"
    monkeypatch.setenv("FD_SIMPLE_CHAT", "1")
    cfg = admin_routes._load_system_config(None)
    assert cfg["kiosk_mode"] == "all" and cfg["kiosk_mode_from_env"] is True
    # An admin's pick wins over the flag.
    cfg = admin_routes._load_system_config(json.dumps({"kiosk_mode": "selected"}))
    assert cfg["kiosk_mode"] == "selected" and cfg["kiosk_mode_from_env"] is False


# ── Admin config ──

async def test_config_roundtrip_and_validation(fd_db):
    async with _client(_app(ADMIN)) as c:
        r = await c.put("/fd/admin/config", json={"kiosk_mode": "selected",
                                                  "kiosk_new_users": True})
        assert r.status_code == 200, r.text
        assert r.json()["kiosk_mode"] == "selected"
        assert r.json()["kiosk_new_users"] is True
        r = await c.put("/fd/admin/config", json={"kiosk_mode": "everyone"})
        assert r.status_code == 400
        r = await c.get("/fd/admin/config")
    assert r.json()["kiosk_mode"] == "selected"


async def test_unrelated_toggle_does_not_freeze_env_default(fd_db, monkeypatch):
    monkeypatch.setenv("FD_SIMPLE_CHAT", "1")
    async with _client(_app(ADMIN)) as c:
        r = await c.put("/fd/admin/config", json={"docker_spawn_enabled": False})
    assert r.json()["kiosk_mode"] == "all"
    stored = json.loads(await fd_db.get_system_setting("fd:system-config"))
    assert "kiosk_mode" not in stored  # dropping --simple-chat still turns it off
    monkeypatch.delenv("FD_SIMPLE_CHAT")
    assert (await admin_routes.user_kiosk_locked(
        fd_db, await _user(fd_db, "a@x.test"))) is False


async def test_config_needs_admin(fd_db):
    async with _client(_app({"id": "u", "role": "user"})) as c:
        r = await c.put("/fd/admin/config", json={"kiosk_mode": "all"})
    assert r.status_code == 403


# ── Per-user flag ──

async def test_admin_marks_and_unmarks_user(fd_db):
    u = await _user(fd_db, "tam@x.test")
    async with _client(_app(ADMIN)) as c:
        r = await c.put(f"/fd/admin/users/{u['id']}", json={"kiosk": True})
        assert r.status_code == 200, r.text
        assert json.loads((await fd_db.get_user_by_id(u["id"]))["metadata"])["kiosk"] is True
        r = await c.put(f"/fd/admin/users/{u['id']}", json={"kiosk": False})
    assert "kiosk" not in json.loads((await fd_db.get_user_by_id(u["id"]))["metadata"])


async def test_new_users_follow_deck_default(fd_db):
    await _set_config(fd_db, kiosk_mode="selected", kiosk_new_users=True)
    async with _client(_app(ADMIN)) as c:
        r1 = await c.post("/fd/admin/users", json={"email": "a@x.test", "password": "sixchars"})
        r2 = await c.post("/fd/admin/users", json={"email": "b@x.test", "password": "sixchars",
                                                   "kiosk": False})
        r3 = await c.post("/fd/admin/users", json={"email": "c@x.test", "password": "sixchars",
                                                   "role": "admin"})
    assert r1.json()["user"]["kiosk"] is True   # deck default
    assert r2.json()["user"]["kiosk"] is False  # admin's explicit choice
    assert r3.json()["user"]["kiosk"] is False  # admins never marked
    await _set_config(fd_db, kiosk_mode="selected", kiosk_new_users=False)
    async with _client(_app(ADMIN)) as c:
        r4 = await c.post("/fd/admin/users", json={"email": "d@x.test", "password": "sixchars"})
    assert r4.json()["user"]["kiosk"] is False


async def test_self_registration_follows_deck_default(fd_db, monkeypatch):
    monkeypatch.setenv("FD_REGISTRATION_OPEN", "1")
    monkeypatch.setattr(auth_routes, "_fd_auth_enabled", lambda: True)
    await _user(fd_db, "first@x.test", role="admin")
    await _set_config(fd_db, kiosk_mode="selected", kiosk_new_users=True)
    app = FastAPI()
    app.include_router(auth_routes.router)
    async with _client(app) as c:
        r = await c.post("/fd/auth/register", json={"email": "new@x.test",
                                                    "password": "sixchars"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["kiosk"] is True


# ── The lock travels with the user ──

async def test_login_and_me_carry_resolved_lock(fd_db):
    marked = await _user(fd_db, "m@x.test", kiosk=True)
    await _user(fd_db, "p@x.test")
    await _user(fd_db, "boss@x.test", role="admin", kiosk=True)

    await _set_config(fd_db, kiosk_mode="selected")
    assert (await _login(fd_db, "m@x.test"))["kiosk"] is True
    assert (await _login(fd_db, "p@x.test"))["kiosk"] is False
    assert (await _login(fd_db, "boss@x.test"))["kiosk"] is False

    await _set_config(fd_db, kiosk_mode="all")
    assert (await _login(fd_db, "p@x.test"))["kiosk"] is True

    await _set_config(fd_db, kiosk_mode="off")
    assert (await _login(fd_db, "m@x.test"))["kiosk"] is False

    await _set_config(fd_db, kiosk_mode="selected")
    async with _client(_app(marked)) as c:
        r = await c.get("/fd/auth/me")
    assert r.json()["kiosk"] is True
