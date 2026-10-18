"""Member invitations — an admin invites someone (``/fd/admin/invites``), the
email goes out from the admin's own Google or Microsoft account, and the
invitee opens the link and chooses their password (``/fd/auth/invite/*``).
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from captain_claw.flight_deck import admin_routes, auth_routes, invite_routes
from captain_claw.flight_deck.auth import get_current_user, hash_password, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

LOOPBACK = ("127.0.0.1", 40001)
GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"


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
    monkeypatch.delenv("FD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("FD_GMAIL_SEND", raising=False)
    monkeypatch.setattr(invite_routes, "_lookup_limiter", auth_routes._PairLimiter())
    monkeypatch.setattr(invite_routes, "_accept_limiter", auth_routes._PairLimiter())
    boss = await db.create_user("boss@example.com", hash_password("boss-pass"), "Boss", "admin")
    try:
        yield SimpleNamespace(db=db, admin={**boss, "role": "admin"})
    finally:
        await db.close()
        fd_auth._db = prev


def _app(user: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(invite_routes.admin_router)
    app.include_router(invite_routes.public_router)
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


def _token(url: str) -> str:
    assert "/invite#" in url
    return url.split("#", 1)[1]


async def _invite(c, **overrides):
    return await c.post("/fd/admin/invites", json={
        "email": "New.Member@Example.com", "display_name": "New Member", **overrides})


# ── The whole way round ──

async def test_invite_lookup_accept_sign_in(fd_db):
    async with _client(_app(fd_db.admin)) as c:
        r = await _invite(c, note="Welcome aboard!")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["email"] == {"sent": False, "via": "link", "from": "", "error": ""}
        assert body["invite"]["email"] == "new.member@example.com"
        assert body["invite"]["status"] == "pending"
        assert body["invite"]["invited_by_name"] == "Boss"
        assert "token_hash" not in body["invite"]
        token = _token(body["url"])
        assert body["url"].startswith("http://fd.test/invite#")

        r = await c.get("/fd/admin/invites")
        [listed] = r.json()["invites"]
        assert listed["id"] == body["invite"]["id"]
        assert listed["expired"] is False
        assert "token_hash" not in listed

        r = await c.post("/fd/auth/invite/lookup", json={"token": token})
        assert r.status_code == 200, r.text
        assert r.json()["email"] == "new.member@example.com"
        assert r.json()["inviter"] == "Boss"
        assert r.json()["display_name"] == "New Member"

        r = await c.post("/fd/auth/invite/accept", json={"token": token, "password": "short"})
        assert r.status_code == 400
        r = await c.post("/fd/auth/invite/accept",
                         json={"token": token, "password": "my-own-pass", "display_name": "Nova"})
        assert r.status_code == 200, r.text
        assert r.json()["access_token"]
        assert r.json()["user"]["display_name"] == "Nova"
        assert r.json()["user"]["role"] == "user"
        assert r.json()["user"]["must_change_password"] is False
        assert "fd_refresh" in r.headers.get("set-cookie", "")

        # One use only.
        r = await c.post("/fd/auth/invite/accept", json={"token": token, "password": "another-pass"})
        assert r.status_code == 409
        r = await c.post("/fd/auth/invite/lookup", json={"token": token})
        assert r.status_code == 409

        r = await c.post("/fd/auth/login", json={"email": "new.member@example.com", "password": "my-own-pass"})
        assert r.status_code == 200, r.text

    row = await fd_db.db.get_invite(body["invite"]["id"])
    assert row["status"] == "accepted" and row["user_id"] and row["accepted_at"]
    [note] = await fd_db.db.list_notifications(fd_db.admin["id"])
    assert note["type"] == "invite_accepted" and "Nova" in note["title"]


async def test_accept_needs_no_signed_in_user(fd_db):
    async with _client(_app(fd_db.admin)) as c:
        token = _token((await _invite(c)).json()["url"])
    async with _client(_app()) as anon:
        r = await anon.post("/fd/auth/invite/accept", json={"token": token, "password": "my-own-pass"})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["display_name"] == "New Member"  # the name the admin gave


async def test_admin_routes_need_an_admin(fd_db):
    async with _client(_app({"id": "u-2", "role": "user"})) as c:
        assert (await c.get("/fd/admin/invites")).status_code == 403
        assert (await _invite(c)).status_code == 403


# ── Creating ──

async def test_refuses_existing_account_and_a_second_pending_invite(fd_db):
    async with _client(_app(fd_db.admin)) as c:
        assert (await _invite(c, email="boss@example.com")).status_code == 409
        assert (await _invite(c, email="nope")).status_code == 400
        assert (await _invite(c, role="owner")).status_code == 400
        assert (await _invite(c, send_via="pigeon")).status_code == 400
        assert (await _invite(c)).status_code == 201
        r = await _invite(c)
        assert r.status_code == 409 and "resend" in r.json()["detail"]


async def test_an_expired_invite_is_replaced_by_a_new_one(fd_db, monkeypatch):
    async with _client(_app(fd_db.admin)) as c:
        old = (await _invite(c)).json()
        later = invite_routes._now() + timedelta(days=8)
        monkeypatch.setattr(invite_routes, "_now", lambda: later)
        r = await c.get("/fd/admin/invites")
        assert r.json()["invites"][0]["expired"] is True
        assert (await c.post("/fd/auth/invite/lookup", json={"token": _token(old["url"])})).status_code == 410
        new = await _invite(c)
        assert new.status_code == 201, new.text
    assert await fd_db.db.get_invite(old["invite"]["id"]) is None


async def test_kiosk_and_role_reach_the_account(fd_db):
    async with _client(_app(fd_db.admin)) as c:
        t1 = _token((await _invite(c, email="kiosk@example.com", kiosk=True)).json()["url"])
        t2 = _token((await _invite(c, email="second@example.com", role="admin", kiosk=True)).json()["url"])
        assert (await c.post("/fd/auth/invite/accept", json={"token": t1, "password": "pass-pass"})).status_code == 200
        assert (await c.post("/fd/auth/invite/accept", json={"token": t2, "password": "pass-pass"})).status_code == 200
    k = await fd_db.db.get_user_by_email("kiosk@example.com")
    assert json.loads(k["metadata"]).get("kiosk") is True and k["role"] == "user"
    a = await fd_db.db.get_user_by_email("second@example.com")
    assert a["role"] == "admin" and "kiosk" not in json.loads(a["metadata"])


async def test_link_follows_public_url_then_the_admins_origin(fd_db, monkeypatch):
    async with _client(_app(fd_db.admin)) as c:
        r = await c.post("/fd/admin/invites", json={"email": "a@example.com"},
                         headers={"Origin": "http://localhost:5173"})
        assert r.json()["url"].startswith("http://localhost:5173/invite#")
        monkeypatch.setenv("FD_PUBLIC_URL", "https://deck.example.com/")
        r = await c.post("/fd/admin/invites", json={"email": "b@example.com"},
                         headers={"Origin": "http://localhost:5173"})
        assert r.json()["url"].startswith("https://deck.example.com/invite#")


# ── Resend / remove ──

async def test_resend_replaces_the_link(fd_db):
    async with _client(_app(fd_db.admin)) as c:
        first = (await _invite(c)).json()
        r = await c.post(f"/fd/admin/invites/{first['invite']['id']}/resend", json={"send_via": "link"})
        assert r.status_code == 200, r.text
        second = r.json()
        assert second["url"] != first["url"]
        assert (await c.post("/fd/auth/invite/lookup", json={"token": _token(first["url"])})).status_code == 404
        assert (await c.post("/fd/auth/invite/lookup", json={"token": _token(second["url"])})).status_code == 200
        r = await c.post(f"/fd/admin/invites/{first['invite']['id']}/resend")  # body optional
        assert r.status_code == 200
        assert (await c.post("/fd/admin/invites/nope/resend", json={})).status_code == 404


async def test_delete_kills_the_link(fd_db):
    async with _client(_app(fd_db.admin)) as c:
        made = (await _invite(c)).json()
        assert (await c.delete(f"/fd/admin/invites/{made['invite']['id']}")).status_code == 200
        assert (await c.post("/fd/auth/invite/lookup", json={"token": _token(made["url"])})).status_code == 404
        assert (await c.delete(f"/fd/admin/invites/{made['invite']['id']}")).status_code == 404


async def test_bad_tokens(fd_db):
    async with _client(_app()) as c:
        for token in ("", "x" * 500, "not-a-real-token"):
            r = await c.post("/fd/auth/invite/lookup", json={"token": token})
            assert r.status_code == 404


async def test_accept_settles_a_pending_waitlist_application(fd_db):
    entry = await fd_db.db.create_waitlist_entry(
        email="new.member@example.com", display_name="NM", password_hash=hash_password("x" * 8),
        referrer_name="R", referrer_email="r@example.com", cohort="c")
    async with _client(_app(fd_db.admin)) as c:
        token = _token((await _invite(c)).json()["url"])
        assert (await c.post("/fd/auth/invite/accept", json={"token": token, "password": "pass-pass"})).status_code == 200
    row = await fd_db.db.get_waitlist_entry(entry["id"])
    assert row["status"] == "approved" and row["user_id"]


async def test_auth_disabled_deck_has_no_invitations(fd_db, monkeypatch):
    monkeypatch.setenv("FD_AUTH_ENABLED", "false")
    async with _client(_app(fd_db.admin)) as c:
        assert (await _invite(c)).status_code == 403
        assert (await c.post("/fd/auth/invite/lookup", json={"token": "x"})).status_code == 403


# ── Email ──

def _connect_google(monkeypatch, scope: str = GMAIL_COMPOSE):
    async def connected(uid):
        return True

    async def tokens(db, uid):
        return SimpleNamespace(scope=f"openid email {scope}")

    async def user(db, uid):
        return {"email": "boss@gmail.com"}

    async def access(uid):
        return "g-token"

    monkeypatch.setattr(invite_routes._google, "is_google_connected", connected)
    monkeypatch.setattr(invite_routes._google, "_load_tokens", tokens)
    monkeypatch.setattr(invite_routes._google, "_load_user", user)
    monkeypatch.setattr(invite_routes._google, "get_valid_google_access_token", access)


def _connect_microsoft(monkeypatch, scope: str = "Mail.ReadWrite Mail.Send"):
    async def connected(uid):
        return True

    async def tokens(db, uid):
        return SimpleNamespace(scope=f"openid email offline_access User.Read {scope}")

    async def user(db, uid):
        return {"email": "boss@contoso.com"}

    async def access(uid):
        return "m-token"

    monkeypatch.setattr(invite_routes._microsoft, "is_microsoft_connected", connected)
    monkeypatch.setattr(invite_routes._microsoft, "_load_tokens", tokens)
    monkeypatch.setattr(invite_routes._microsoft, "_load_user", user)
    monkeypatch.setattr(invite_routes._microsoft, "get_valid_microsoft_access_token", access)


def _mock_http(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(invite_routes, "_http",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return seen


async def test_mail_options_when_nothing_is_connected(fd_db):
    async with _client(_app(fd_db.admin)) as c:
        mail = (await c.get("/fd/admin/invites")).json()["mail"]
        assert mail["google"]["available"] is False and "Connections → Google" in mail["google"]["reason"]
        assert mail["microsoft"]["available"] is False
        r = await _invite(c, send_via="google")
        assert r.status_code == 201  # the invitation stands; the link is in the answer
        assert r.json()["email"]["sent"] is False
        assert "Connections → Google" in r.json()["email"]["error"]
        assert _token(r.json()["url"])


async def test_send_from_gmail(fd_db, monkeypatch):
    _connect_google(monkeypatch)
    seen = _mock_http(monkeypatch, lambda req: httpx.Response(200, json={"id": "m1"}))
    async with _client(_app(fd_db.admin)) as c:
        mail = (await c.get("/fd/admin/invites")).json()["mail"]
        assert mail["google"] == {"available": True, "from": "boss@gmail.com", "reason": ""}
        r = await _invite(c, send_via="google", note="See you <b>soon</b>")
        assert r.status_code == 201, r.text
        assert r.json()["email"] == {"sent": True, "via": "google", "from": "boss@gmail.com", "error": ""}
        invite = r.json()["invite"]
        assert invite["sent_via"] == "google" and invite["sent_from"] == "boss@gmail.com" and invite["sent_at"]
    [req] = seen
    assert req.url.path == "/gmail/v1/users/me/messages/send"
    assert req.headers["authorization"] == "Bearer g-token"
    raw = json.loads(req.content)["raw"]
    message = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "To: new.member@example.com" in message
    assert "Boss invited you to Flight Deck" in message
    assert _token(r.json()["url"]) in message.replace("=\r\n", "")
    assert "&lt;b&gt;soon&lt;/b&gt;" in message  # the note is escaped in the HTML part


async def test_gmail_without_a_send_scope_or_with_the_deck_switch_off(fd_db, monkeypatch):
    _connect_google(monkeypatch, scope="https://www.googleapis.com/auth/gmail.readonly")
    async with _client(_app(fd_db.admin)) as c:
        mail = (await c.get("/fd/admin/invites")).json()["mail"]["google"]
        assert mail["available"] is False and "gmail.compose" in mail["reason"]
        _connect_google(monkeypatch)
        monkeypatch.setenv("FD_GMAIL_SEND", "off")
        mail = (await c.get("/fd/admin/invites")).json()["mail"]["google"]
        assert mail["available"] is False and "FD_GMAIL_SEND" in mail["reason"]


async def test_gmail_refusal_keeps_the_invitation(fd_db, monkeypatch):
    _connect_google(monkeypatch)
    _mock_http(monkeypatch, lambda req: httpx.Response(
        403, json={"error": {"code": 403, "message": "Request had insufficient authentication scopes."}}))
    async with _client(_app(fd_db.admin)) as c:
        r = await _invite(c, send_via="google")
        assert r.status_code == 201
        assert r.json()["email"]["sent"] is False
        assert "insufficient authentication scopes" in r.json()["email"]["error"]
        assert r.json()["invite"]["sent_via"] == ""


async def test_send_from_outlook(fd_db, monkeypatch):
    _connect_microsoft(monkeypatch)
    seen = _mock_http(monkeypatch, lambda req: httpx.Response(202))
    async with _client(_app(fd_db.admin)) as c:
        made = (await _invite(c, send_via="link")).json()
        r = await c.post(f"/fd/admin/invites/{made['invite']['id']}/resend", json={"send_via": "microsoft"})
        assert r.status_code == 200, r.text
        assert r.json()["email"]["sent"] is True
        assert r.json()["invite"]["sent_from"] == "boss@contoso.com"
    [req] = seen
    assert str(req.url) == "https://graph.microsoft.com/v1.0/me/sendMail"
    payload = json.loads(req.content)
    assert payload["message"]["toRecipients"] == [{"emailAddress": {"address": "new.member@example.com"}}]
    assert _token(r.json()["url"]) in payload["message"]["body"]["content"]
    assert payload["saveToSentItems"] is True


async def test_outlook_needs_mail_send(fd_db, monkeypatch):
    _connect_microsoft(monkeypatch, scope="Mail.ReadWrite")
    async with _client(_app(fd_db.admin)) as c:
        mail = (await c.get("/fd/admin/invites")).json()["mail"]["microsoft"]
        assert mail["available"] is False and "Mail (send)" in mail["reason"]
        r = await _invite(c, send_via="microsoft")
        assert r.json()["email"]["sent"] is False and "Mail (send)" in r.json()["email"]["error"]


def test_compose_invite_text_and_html():
    subject, text, html_body = invite_routes.compose_invite(
        instance_name="Acme Deck", instance_description="Agents for Acme", inviter="Boss",
        invitee="Nova", email="nova@example.com", url="https://deck.example.com/invite#abc",
        expires_at="2026-10-25T10:00:00+00:00", note="Line one\nLine two",
    )
    assert subject == "Boss invited you to Acme Deck"
    assert "Hi Nova," in text and "https://deck.example.com/invite#abc" in text
    assert "25 October 2026" in text and "  Line two" in text
    assert 'href="https://deck.example.com/invite#abc"' in html_body
    assert "Agents for Acme" in html_body
