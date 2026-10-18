"""The agent-side ``public_link`` tool — "share that file".

Pins who may publish (a person's turn; an automated turn only when its own
text asks for a link), what may be published (no secrets, files found the way
the read tool finds them), and that the call goes to Flight Deck's agent route
with the agent's own identity headers, the URL coming back for the user.

No FD, no network: httpx is routed to a MockTransport that records requests.
"""

from __future__ import annotations

import json

import httpx
import pytest

from captain_claw import fd_client, mail_authority
from captain_claw.tools import public_link as pl
from captain_claw.tools.public_link import PublicLinkTool, link_intent

FD = "http://fd.test"
URL = "https://deck.example.com/s/TOKEN123"


@pytest.fixture()
def fd(monkeypatch, tmp_path):
    seen: list[httpx.Request] = []
    answer = {"status": 200, "body": {"share": {"token": "TOKEN123", "url": URL, "filename": "report.md",
                                                 "expires_at": None, "local_only": False}}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(answer["status"], json=answer["body"])

    real = httpx.AsyncClient
    monkeypatch.setattr(pl.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(fd_client, "flight_deck_base", lambda: FD)
    monkeypatch.setattr(fd_client, "flight_deck_headers",
                        lambda: {"X-Agent-Auth": "my-web-auth", "X-Agent-Secret": "s"})
    saved = tmp_path / "saved"
    (saved / "downloads" / "sess-1").mkdir(parents=True)
    report = saved / "downloads" / "sess-1" / "report.md"
    report.write_text("# Report")
    (saved / "downloads" / "sess-1" / ".env").write_text("KEY=1")
    runtime = {"_saved_base_path": str(saved), "_runtime_base_path": str(tmp_path)}
    return seen, answer, runtime, report


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_share_posts_the_resolved_file_and_returns_the_link(fd):
    seen, _, runtime, report = fd
    res = _run(PublicLinkTool().execute(action="share", path="report.md", expires_days=7, **runtime))
    assert res.success, res.error
    assert URL in res.content and "anyone with the link can open" in res.content
    assert "again" not in res.content     # nothing that reads like "call me again"
    req = seen[-1]
    assert str(req.url) == f"{FD}/fd/public-shares/agent/share"
    assert req.headers["X-Agent-Auth"] == "my-web-auth"
    body = json.loads(req.content)
    assert body == {"path": str(report.resolve()), "expires_days": 7}


def test_relative_saved_path_resolves_too(fd):
    seen, _, runtime, report = fd
    res = _run(PublicLinkTool().execute(action="share", path="saved/downloads/sess-1/report.md", **runtime))
    assert res.success, res.error
    assert json.loads(seen[-1].content) == {"path": str(report.resolve())}


def test_secrets_and_missing_files_are_never_sent(fd):
    seen, _, runtime, _ = fd
    res = _run(PublicLinkTool().execute(action="share", path="saved/downloads/sess-1/.env", **runtime))
    assert not res.success and "secrets" in res.error
    res = _run(PublicLinkTool().execute(action="share", path="nope.pdf", **runtime))
    assert not res.success and "No file" in res.error
    assert seen == []


def test_automated_turn_needs_its_own_text_to_ask(fd):
    seen, _, runtime, _ = fd
    tok = mail_authority.bind(mail_authority.automated("cron", "Summarize today's inbox"))
    try:
        res = _run(PublicLinkTool().execute(action="share", path="report.md", **runtime))
    finally:
        mail_authority.reset(tok)
    assert not res.success and res.error.startswith(pl.REFUSAL_TAG)
    assert seen == []

    tok = mail_authority.bind(mail_authority.automated(
        "cron", "Write the weekly report and send me a public link to it"))
    try:
        res = _run(PublicLinkTool().execute(action="share", path="report.md", **runtime))
    finally:
        mail_authority.reset(tok)
    assert res.success, res.error


def test_link_intent_detector():
    assert link_intent("share the report with the team")
    assert link_intent("give me a link to the summary")
    assert link_intent("napravi javni link za izvještaj")
    assert link_intent("podijeli dokument")
    assert not link_intent("Summarize today's inbox and draft nothing")
    assert not link_intent("check the LinkedIn notifications")


def test_flight_deck_refusals_come_back_readable(fd):
    seen, answer, runtime, _ = fd
    answer.update(status=403, body={"detail": "The agent refused the file (403)"})
    res = _run(PublicLinkTool().execute(action="share", path="report.md", **runtime))
    assert not res.success and "Files list" in res.error


def test_local_only_link_is_flagged(fd):
    _, answer, runtime, _ = fd
    answer["body"]["share"].update(url="http://127.0.0.1:25080/s/T", local_only=True)
    res = _run(PublicLinkTool().execute(action="share", path="report.md", **runtime))
    assert res.success and "only works on this computer or network" in res.content


def test_unshare_and_list(fd):
    seen, answer, runtime, report = fd
    answer["body"] = {"ok": True, "filename": "report.md"}
    res = _run(PublicLinkTool().execute(action="unshare", path="report.md", **runtime))
    assert res.success and "no longer opens" in res.content
    assert json.loads(seen[-1].content) == {"token": "", "path": str(report.resolve())}
    answer["body"] = {"shares": [{"filename": "report.md", "url": URL, "view_count": 3,
                                  "expires_at": None, "token": "TOKEN123"}]}
    res = _run(PublicLinkTool().execute(action="list"))
    assert res.success and URL in res.content and "3 views" in res.content


def test_without_flight_deck(monkeypatch):
    monkeypatch.setattr(fd_client, "flight_deck_base", lambda: "")
    res = _run(PublicLinkTool().execute(action="share", path="x.md"))
    assert not res.success and "Flight Deck" in res.error
