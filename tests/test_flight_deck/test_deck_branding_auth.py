"""Admin-set passwords the user must replace, and the deck's branding.

* An account an admin creates (or whose password an admin sets) carries
  ``must_change_password``: the sign-in payload says so, and the flag clears
  only when the user picks a password of their own (PUT /fd/auth/me).
* Branding: the instance name and description in the system config, and a
  logo image an admin uploads, served publicly for the sign-in page.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from captain_claw.flight_deck import admin_routes, auth_routes, branding_routes
from captain_claw.flight_deck.auth import get_current_user, hash_password, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

LOOPBACK = ("127.0.0.1", 40001)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>'


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
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setattr(auth_routes, "_password_change_limiter", auth_routes._PairLimiter())
    admin = await db.create_user("boss@example.com", hash_password("boss-pass"), "Boss", "admin")
    try:
        yield db, admin
    finally:
        await db.close()
        fd_auth._db = prev


def _app(user: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(branding_routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


async def _login(c, email: str, password: str) -> dict:
    r = await c.post("/fd/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


async def _change(db, email: str, current: str, new: str) -> httpx.Response:
    """PUT /fd/auth/me as *email* (the signed-in user, fresh from the DB)."""
    user = await db.get_user_by_email(email)
    me = await db.get_user_by_id(user["id"])
    async with _client(_app(me)) as c:
        return await c.put("/fd/auth/me", json={"current_password": current, "password": new})


# ── Admin-set passwords ──

async def test_a_new_account_must_change_its_password(fd_db):
    db, admin = fd_db
    async with _client(_app(admin)) as c:
        r = await c.post("/fd/admin/users", json={"email": "new@example.com", "password": "temp-pass"})
        assert r.status_code == 201, r.text
        assert r.json()["user"]["must_change_password"] is True
        assert (await _login(c, "new@example.com", "temp-pass"))["user"]["must_change_password"] is True

    # The password the admin gave doesn't count as a new one.
    r = await _change(db, "new@example.com", "temp-pass", "temp-pass")
    assert r.status_code == 400 and "not the one you were given" in r.json()["detail"]

    r = await _change(db, "new@example.com", "temp-pass", "my-own-pass")
    assert r.status_code == 200, r.text
    assert r.json()["must_change_password"] is False
    async with _client(_app()) as c:
        assert (await _login(c, "new@example.com", "my-own-pass"))["user"]["must_change_password"] is False


async def test_the_admin_can_leave_it_off(fd_db):
    """A shared kiosk account everyone signs in with keeps the admin's password."""
    db, admin = fd_db
    async with _client(_app(admin)) as c:
        r = await c.post("/fd/admin/users", json={
            "email": "kiosk@example.com", "password": "shared-pass", "must_change_password": False})
        assert r.json()["user"]["must_change_password"] is False
        assert (await _login(c, "kiosk@example.com", "shared-pass"))["user"]["must_change_password"] is False


async def test_an_admin_reset_can_ask_for_a_new_password(fd_db):
    db, admin = fd_db
    async with _client(_app(admin)) as c:
        await c.post("/fd/admin/users", json={
            "email": "u@example.com", "password": "first-pass", "must_change_password": False})
        uid = (await db.get_user_by_email("u@example.com"))["id"]
        r = await c.put(f"/fd/admin/users/{uid}", json={"password": "reset-pass", "must_change_password": True})
        assert r.status_code == 200, r.text
        assert (await _login(c, "u@example.com", "reset-pass"))["user"]["must_change_password"] is True
        # …and take it back.
        await c.put(f"/fd/admin/users/{uid}", json={"must_change_password": False})
        assert (await _login(c, "u@example.com", "reset-pass"))["user"]["must_change_password"] is False
        # Never on the admin's own account (they use their profile).
        await c.put(f"/fd/admin/users/{admin['id']}", json={"must_change_password": True})
        assert (await _login(c, "boss@example.com", "boss-pass"))["user"]["must_change_password"] is False


async def test_flag_survives_other_metadata_edits(fd_db):
    db, admin = fd_db
    async with _client(_app(admin)) as c:
        await c.post("/fd/admin/users", json={"email": "k@example.com", "password": "temp-pass"})
        uid = (await db.get_user_by_email("k@example.com"))["id"]
        await c.put(f"/fd/admin/users/{uid}", json={"kiosk": True, "plan": "pro"})
    meta = json.loads((await db.get_user_by_id(uid))["metadata"])
    assert meta["must_change_password"] is True and meta["kiosk"] is True


async def test_waitlist_and_self_registration_never_set_it(fd_db, monkeypatch):
    db, _admin = fd_db
    monkeypatch.setenv("FD_REGISTRATION_OPEN", "1")
    async with _client(_app()) as c:
        r = await c.post("/fd/auth/register", json={"email": "self@example.com", "password": "own-pass"})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["must_change_password"] is False


# ── Branding ──

async def test_instance_name_and_description(fd_db):
    db, admin = fd_db
    async with _client(_app(admin)) as c:
        cfg = (await c.get("/fd/admin/config")).json()
        assert cfg["instance_name"] == "Flight Deck"
        assert cfg["instance_description"] == "Agent Management Console"

        r = await c.put("/fd/admin/config", json={
            "instance_name": "  Acme ‮ Agents  ", "instance_description": "Our\nagents,\tyour work."})
        assert r.status_code == 200, r.text
        assert r.json()["instance_name"] == "Acme Agents"
        assert r.json()["instance_description"] == "Our agents, your work."

        # Blank name: the default again. Blank description: none.
        r = await c.put("/fd/admin/config", json={"instance_name": " ", "instance_description": ""})
        assert r.json()["instance_name"] == "Flight Deck"
        assert r.json()["instance_description"] == ""
        assert "instance_name" not in json.loads(await db.get_system_setting("fd:system-config"))

        r = await c.put("/fd/admin/config", json={"instance_name": "x" * 61})
        assert r.status_code == 400
        r = await c.put("/fd/admin/config", json={"instance_description": "x" * 301})
        assert r.status_code == 400


async def test_powered_by_captain_switch(fd_db, monkeypatch):
    from captain_claw.flight_deck import server

    db, admin = fd_db
    monkeypatch.setattr(server, "AUTH_ENABLED", True)
    monkeypatch.setattr(server.app.state, "fd_db", db, raising=False)
    app = _app(admin)
    app.add_api_route("/fd/auth/status", server.auth_status)
    async with _client(app) as c:
        # On by default — a deck that never stored it shows the links.
        assert (await c.get("/fd/admin/config")).json()["show_powered_by"] is True
        assert (await c.get("/fd/auth/status")).json()["show_powered_by"] is True

        r = await c.put("/fd/admin/config", json={"show_powered_by": False})
        assert r.status_code == 200, r.text
        assert r.json()["show_powered_by"] is False
        assert (await c.get("/fd/auth/status")).json()["show_powered_by"] is False

        # An unrelated save leaves it off.
        r = await c.put("/fd/admin/config", json={"instance_name": "Acme"})
        assert r.json()["show_powered_by"] is False

        r = await c.put("/fd/admin/config", json={"show_powered_by": True})
        assert (await c.get("/fd/auth/status")).json()["show_powered_by"] is True


def _data_url(data: bytes, claimed: str = "image/png") -> str:
    return f"data:{claimed};base64,{base64.b64encode(data).decode()}"


async def test_logo_upload_serve_and_remove(fd_db):
    db, admin = fd_db
    async with _client(_app(admin)) as c:
        assert (await c.get("/fd/branding/logo")).status_code == 404
        assert await branding_routes.logo_url(db) == ""

        r = await c.put("/fd/admin/branding/logo", json={"data_url": _data_url(PNG)})
        assert r.status_code == 200, r.text
        url = r.json()["logo"]
        assert url.startswith("/fd/branding/logo?v=") and url == await branding_routes.logo_url(db)

        # Public, the bytes as uploaded, never runnable as a page.
        async with _client(_app()) as anon:
            r = await anon.get(url)
        assert r.status_code == 200 and r.content == PNG
        assert r.headers["content-type"] == "image/png"
        assert "sandbox" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"

        # The type is what the bytes are, not what the upload claimed.
        r = await c.put("/fd/admin/branding/logo", json={"data_url": _data_url(SVG, "image/png")})
        assert r.status_code == 200
        assert r.json()["logo"] != url
        assert (await c.get("/fd/branding/logo")).headers["content-type"].startswith("image/svg+xml")

        r = await c.delete("/fd/admin/branding/logo")
        assert r.json()["logo"] == ""
        assert (await c.get("/fd/branding/logo")).status_code == 404


async def test_logo_refuses_what_isnt_an_image(fd_db):
    _db, admin = fd_db
    async with _client(_app(admin)) as c:
        for data_url in (
            _data_url(b"<html><script>alert(1)</script></html>", "image/svg+xml"),
            _data_url(b"MZ\x90\x00 not an image"),
            "data:image/png;base64,@@@",
            "https://example.com/logo.png",
        ):
            r = await c.put("/fd/admin/branding/logo", json={"data_url": data_url})
            assert r.status_code == 400, data_url
        r = await c.put("/fd/admin/branding/logo", json={
            "data_url": _data_url(PNG + b"\x00" * branding_routes.LOGO_MAX_BYTES)})
        assert r.status_code == 413


async def test_only_admins_brand_the_deck(fd_db):
    db, _admin = fd_db
    user = await db.create_user("u@example.com", hash_password("pw-pw-pw"), "U", "user")
    async with _client(_app(user)) as c:
        assert (await c.put("/fd/admin/branding/logo", json={"data_url": _data_url(PNG)})).status_code == 403
        assert (await c.delete("/fd/admin/branding/logo")).status_code == 403
        assert (await c.put("/fd/admin/config", json={"instance_name": "Mine"})).status_code == 403
