"""Public file links — /fd/agent-file-share (owner) and /s/{token} (anyone).

Pins the promises the Share dialog makes: the public side serves only the
snapshot (never the agent, never a path), names nothing but the file, renders
Markdown with raw HTML escaped, sandboxes anything that can run script, and a
revoked / expired / disabled link is gone at once.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import auth, public_file_shares as pfs
from captain_claw.flight_deck.db import FlightDeckDB

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner Person", "role": "user"}
OTHER = {"id": "user-other", "email": "t@x", "display_name": "Other", "role": "user"}
SRC = "/srv/agents/secret-agent/workspace/saved/downloads/session-42/"
AGENT = "/fd/agent-file-share/localhost/24123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("FD_PUBLIC_FILE_LINKS", raising=False)
    monkeypatch.delenv("FD_PUBLIC_FILE_LINK_MAX_MB", raising=False)
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)

    files: dict[str, bytes] = {}
    seen: list[httpx.Request] = []

    def agent(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("token") != "agent-secret-token":
            return httpx.Response(401, json={"error": "Unauthorized"})
        body = files.get(request.url.params.get("path", ""))
        if body is None:
            return httpx.Response(404, json={"error": "File not found on disk"})
        return httpx.Response(200, content=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(pfs.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(agent), **kw))
    pfs.configure(data_dir=tmp_path, resolve_auth=lambda port: "agent-secret-token")

    app = FastAPI()
    app.include_router(pfs.owner_router)
    app.include_router(pfs.public_router)
    current = {"user": OWNER}
    app.dependency_overrides[auth.get_current_user] = lambda: current["user"]
    yield TestClient(app), files, seen, current, db, tmp_path
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


def _share(client, path, **extra):
    body = {"path": path, "agent_ref": "proc-secret-agent", "agent_name": "Secret Agent", **extra}
    r = client.post(AGENT, json=body)
    assert r.status_code == 200, r.text
    return r.json()["share"]


def _no_leak(text: str) -> None:
    for secret in ("secret-agent", "Secret Agent", "session-42", "user-owner", "Owner Person",
                   "24123", "agent-secret-token", "/srv/"):
        assert secret not in text, secret


def test_markdown_renders_with_raw_html_escaped(env):
    client, files, seen, *_ = env
    files[SRC + "report.md"] = (b"# Quarterly report\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
                                b"<script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://e.com)")
    share = _share(client, SRC + "report.md")
    assert share["kind"] == "markdown" and share["path"] == f"/s/{share['token']}"
    # The agent was asked with its own token, resolved server-side.
    assert seen[-1].url.params["token"] == "agent-secret-token"

    r = client.get(share["path"])
    assert r.status_code == 200
    page = r.text
    assert "<h1>Quarterly report</h1>" in page and "<table>" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "href=\"javascript:" not in page   # left as plain text, never a link
    assert 'href="https://e.com" target="_blank" rel="noopener noreferrer nofollow"' in page
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp
    assert r.headers["x-robots-tag"].startswith("noindex")
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    _no_leak(page)
    # The markdown source itself comes back as inert text.
    raw = client.get(share["path"] + "/raw")
    assert raw.headers["content-type"].startswith("text/plain")
    assert raw.headers["content-security-policy"].startswith("sandbox")


def test_html_runs_sandboxed_on_an_opaque_origin(env):
    client, files, *_ = env
    files[SRC + "deck.html"] = b"<html><body><script>document.cookie</script>hi</body></html>"
    share = _share(client, SRC + "deck.html")
    page = client.get(share["path"]).text
    assert f'<iframe src="{share["path"]}/raw"' in page
    raw = client.get(share["path"] + "/raw")
    assert raw.headers["content-type"].startswith("text/html")
    csp = raw.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
    assert raw.content == files[SRC + "deck.html"]


@pytest.mark.parametrize("name,ctype", [("logo.svg", "image/svg+xml"), ("feed.xml", "text/plain"),
                                        ("notes.txt", "text/plain"), ("photo.png", "image/png")])
def test_raw_never_serves_a_script_capable_document(env, name, ctype):
    client, files, *_ = env
    files[SRC + name] = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    share = _share(client, SRC + name)
    raw = client.get(share["path"] + "/raw")
    assert raw.headers["content-type"].startswith(ctype)
    assert raw.headers["content-security-policy"].startswith("sandbox;")
    assert raw.headers["x-content-type-options"] == "nosniff"


def test_other_types_download_only(env):
    client, files, *_ = env
    files[SRC + "Pilot Order Form.docx"] = b"PK\x03\x04docx"
    share = _share(client, SRC + "Pilot Order Form.docx")
    page = client.get(share["path"]).text
    assert "can't be previewed" in page and f'href="{share["path"]}/download"' in page
    dl = client.get(share["path"] + "/download")
    assert dl.content == b"PK\x03\x04docx"
    assert dl.headers["content-disposition"].startswith("attachment;")
    assert "Pilot%20Order%20Form.docx" in dl.headers["content-disposition"]


def test_snapshot_is_independent_of_the_agent(env):
    client, files, seen, *_ = env
    files[SRC + "a.txt"] = b"version one"
    share = _share(client, SRC + "a.txt")
    files[SRC + "a.txt"] = b"version two with a new secret"
    calls = len(seen)
    assert client.get(share["path"] + "/raw").content == b"version one"
    assert len(seen) == calls          # the public side never contacts the agent
    # Refreshing keeps the URL and publishes the new copy.
    again = _share(client, SRC + "a.txt", expires_days=7)
    assert again["token"] == share["token"] and again["expires_at"]
    assert client.get(share["path"] + "/raw").content == b"version two with a new secret"
    # No expiry given: the refresh keeps the one the link has.
    assert _share(client, SRC + "a.txt")["expires_at"] == again["expires_at"]
    assert _share(client, SRC + "a.txt", expires_days=0)["expires_at"] is None


def test_revoke_and_expiry_take_effect_at_once(env):
    client, files, _, _, db, tmp = env
    files[SRC + "a.md"] = b"hello"
    share = _share(client, SRC + "a.md")
    assert client.get(share["path"]).status_code == 200
    assert client.delete(f"/fd/public-shares/{share['token']}").json() == {"ok": True}
    gone = client.get(share["path"])
    assert gone.status_code == 404 and "isn't available" in gone.text
    assert client.get(share["path"] + "/raw").status_code == 404
    assert not (tmp / "public_shares" / share["token"]).exists()

    share = _share(client, SRC + "a.md", expires_days=1)
    row = asyncio.run(db.get_public_file_share(share["token"]))
    row["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    asyncio.run(db.save_public_file_share(row))
    assert client.get(share["path"]).status_code == 404
    assert asyncio.run(db.get_public_file_share(share["token"])) is None


def test_views_counted_and_links_are_the_owners_alone(env):
    client, files, _, current, *_ = env
    files[SRC + "a.md"] = b"hello"
    share = _share(client, SRC + "a.md")
    client.get(share["path"])
    client.get(share["path"])
    mine = client.get("/fd/public-shares").json()["shares"]
    assert [s["view_count"] for s in mine] == [2]

    current["user"] = OTHER
    assert client.get("/fd/public-shares").json()["shares"] == []
    assert client.get(AGENT, params={"path": SRC + "a.md", "agent_ref": "proc-secret-agent"}
                      ).json()["share"] is None
    assert client.delete(f"/fd/public-shares/{share['token']}").status_code == 404
    assert client.get(share["path"]).status_code == 200


def test_limits_and_bad_input(env, monkeypatch):
    client, files, *_ = env
    monkeypatch.setenv("FD_PUBLIC_FILE_LINK_MAX_MB", "1")
    files[SRC + "big.bin"] = b"x" * (1024 * 1024 + 1)
    r = client.post(AGENT, json={"path": SRC + "big.bin"})
    assert r.status_code == 413
    assert client.post(AGENT, json={"path": SRC + "missing.md"}).status_code == 404
    # A wrong agent token is the agent's 401 — never the caller's session.
    files[SRC + "a.md"] = b"x"
    assert client.post(AGENT, json={"path": SRC + "a.md", "agent_auth": "wrong"}).status_code == 502
    assert client.post(AGENT, json={"path": SRC + "a", "expires_days": 3}).status_code == 400
    assert client.get("/s/short").status_code == 404
    assert client.get("/s/" + "A" * 30).status_code == 404


def test_deck_switch_turns_every_link_off(env, monkeypatch):
    client, files, *_ = env
    files[SRC + "a.md"] = b"hello"
    share = _share(client, SRC + "a.md")
    monkeypatch.setenv("FD_PUBLIC_FILE_LINKS", "off")
    assert client.get("/fd/public-shares/config").json()["enabled"] is False
    assert client.get(share["path"]).status_code == 404
    assert client.post(AGENT, json={"path": SRC + "a.md"}).status_code == 403


def test_owner_route_is_port_addressed_for_the_ownership_guard():
    # server._agent_proxy_port is what makes the hardening middleware refuse
    # another user's agent; the share routes must keep that shape.
    from captain_claw.flight_deck import server

    assert server._agent_proxy_port(AGENT) == 24123
