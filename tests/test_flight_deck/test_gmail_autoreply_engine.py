"""Topic auto-replies for Gmail — the engine (gmail_autoreply).

The whole pipeline runs against a real FlightDeckDB and a scripted Gmail
(httpx.MockTransport: profile, sendAs, history, messages, threads, send). The
classifier, the writer and the judge are faked at their module attributes;
one test runs the real writer against a fake agent ``/api/llm/complete`` and
``run_tool``. Nothing reaches a network, and nothing starts a Flight Deck.
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

import captain_claw.flight_deck.gmail_send_routes as gs
import captain_claw.flight_deck.google_oauth_routes as gr
from captain_claw import gmail_compose, mail_signature
from captain_claw import gmail_inbound as gi
from captain_claw.flight_deck import actions, agent_sharing, auth, autonomy, events, fd_dispatch
from captain_claw.flight_deck import gmail_autoreply as engine
from captain_claw.flight_deck import gmail_autoreply_config as arcfg
from captain_claw.flight_deck import gmail_autoreply_judges as judges
from captain_claw.flight_deck import gmail_autoreply_writer as writer
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.google_oauth import GoogleOAuthTokens

ALICE = "user-alice"
BOB = "user-bob"
SUB = "google-sub-alice"
OWN = "owner.inbox@gmail.com"
ALIAS = "alias@example.org"
SENDER = "alice@example.com"
REF = "process:meetup-bot:0123456789abcdef"
AGENT_TOKEN = "agent-web-token-123"
SIGNATURE = "— Ana's assistant (automatic reply)"
SCOPES = ("openid email https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/gmail.send")
BRIEF = ("What the agent may say: the Zagreb AI Meetup is on 14 November at 18:00 at the Hub, "
         "Ilica 1, Zagreb. Registration is free at the door; snacks & drinks are free too.")
REPLY_TEXT = ("Hi! The Zagreb AI Meetup is on 14 November at 18:00 at the Hub, Ilica 1. "
              "Registration is free at the door, and snacks & drinks are free too.")
CARD = "4111 1111 1111 1111"
_RealAsyncClient = httpx.AsyncClient

AR_PASS = ("mx.google.com; dkim=pass header.i=@example.com header.s=s1 header.b=Ab3dEf9h; "
           "spf=pass (google.com: domain of alice@example.com designates 192.0.2.25 as "
           "permitted sender) smtp.mailfrom=alice@example.com; "
           "dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com")
MX_HOP = ("from mail.example.com (mail.example.com. [192.0.2.25]) by mx.google.com with ESMTPS "
          "id a640c23a62f3a; Tue, 13 Oct 2026 02:14:07 -0700 (PDT)")

# The real writer entry points, before any fixture fakes them.
_REAL_RESOLVE_WRITER = writer.resolve_writer
_REAL_COMPOSE_REPLY = writer.compose_reply

REC = agent_sharing.AgentRecord(
    runtime="process", slug="meetup-bot", instance="0123456789abcdef", owner=ALICE,
    name="Meetup bot", description="", port=24001, web_auth=AGENT_TOKEN, running=True)


def _now() -> datetime:
    return datetime.now(UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _topic(**kw) -> arcfg.Topic:
    """An enabled topic with a passing breadth test for its own wording."""
    t = arcfg.Topic(
        id="t_0a1b2c3d", name="Meetup logistics",
        about="Emails asking when or where the Zagreb AI Meetup on 14 Nov is, or how to register.",
        examples=("Where is the meetup?",), brief=BRIEF, enabled=True)
    t = replace(t, **kw)
    return replace(t, dry_run=arcfg.DryRun(match_hash=t.match_hash, at=_now().isoformat(),
                                           eligible=20, matched=1, rate=0.05))


TOPIC = _topic()


# ── the fake Gmail ──────────────────────────────────────────────────


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def mail(mid: str, *, frm: str = f"Alice <{SENDER}>", to: str = OWN, cc: str = "",
         subject: str | None = "Where is the meetup?",
         body: str = "Hi, where is the meetup and when does it start?",
         labels=("INBOX", "UNREAD"), age_s: float = 60, auth: bool = True,
         headers=(), thread: str = "", ms: int | None = None) -> dict:
    """A received Gmail message, ``format=full``."""
    hs: list[tuple[str, str]] = []
    if auth:
        hs += [("Received", MX_HOP), ("Authentication-Results", AR_PASS)]
    hs += [("From", frm), ("To", to)]
    if cc:
        hs.append(("Cc", cc))
    if subject is not None:
        hs.append(("Subject", subject))
    hs += [("Message-ID", f"<{mid}@example.com>"), *headers]
    return {
        "id": mid, "threadId": thread or f"th-{mid}", "labelIds": list(labels),
        "internalDate": str(ms if ms is not None else _ms(_now()) - int(age_s * 1000)),
        "payload": {"mimeType": "text/plain",
                    "headers": [{"name": n, "value": v} for n, v in hs],
                    "body": {"size": len(body), "data": _b64(body)}},
    }


def sent_mail(mid: str, thread: str, *, auto: bool = False, ms: int | None = None) -> dict:
    """A message this mailbox sent into *thread*."""
    hs = [("From", OWN), ("To", SENDER), ("Subject", "Re: Where is the meetup?")]
    if auto:
        hs.append(("Auto-Submitted", "auto-replied"))
    return {"id": mid, "threadId": thread, "labelIds": ["SENT"],
            "internalDate": str(ms if ms is not None else _ms(_now()) - 3 * 3600_000),
            "payload": {"mimeType": "text/plain",
                        "headers": [{"name": n, "value": v} for n, v in hs],
                        "body": {"size": 5, "data": _b64("Hello")}}}


class FakeGmail:
    """Scripted Gmail API: a mailbox with a history log; records every request."""

    def __init__(self):
        self.history_id = 5000
        self.messages: dict[str, dict] = {}
        self.added: list[tuple[int, str]] = []
        self.expired_below = 0           # startHistoryId under this → 404
        self.page_size: int | None = None
        self.forced: dict[tuple[str, str], int] = {}
        self.gone_metadata: set[str] = set()
        self.send_status = 200
        self.profile_email = OWN
        self.send_as = [{"sendAsEmail": OWN, "isPrimary": True},
                        {"sendAsEmail": ALIAS, "verificationStatus": "accepted"},
                        {"sendAsEmail": "pending@example.org", "verificationStatus": "pending"}]
        self.requests: list[httpx.Request] = []
        self.n_sent = 0
        self.revoked: set[str] = set()     # access tokens Gmail answers 401 for
        self.after: dict[tuple[str, str], object] = {}   # run once, right after answering

    def deliver(self, msg: dict, *, history: bool = True) -> str:
        self.messages[msg["id"]] = msg
        if history:
            self.history_id += 1
            self.added.append((self.history_id, msg["id"]))
        return msg["id"]

    def readd(self, mid: str) -> None:
        self.history_id += 1
        self.added.append((self.history_id, mid))

    def path(self, request: httpx.Request) -> str:
        return request.url.path.split("/gmail/v1/users/me", 1)[-1]

    def token(self, request: httpx.Request) -> str:
        return request.headers.get("Authorization", "").removeprefix("Bearer ")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.token(request) in self.revoked:
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        response = self._answer(request)
        hook = self.after.pop((request.method, self.path(request)), None)
        if hook is not None:
            hook()
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        path, q = self.path(request), request.url.params
        if (request.method, path) in self.forced:
            return httpx.Response(self.forced[(request.method, path)],
                                  json={"error": {"message": "forced"}})
        if request.method == "GET":
            if path == "/profile":
                return httpx.Response(200, json={"emailAddress": self.profile_email,
                                                 "historyId": str(self.history_id)})
            if path == "/settings/sendAs":
                return httpx.Response(200, json={"sendAs": self.send_as})
            if path == "/history":
                return self._history(q)
            if path == "/messages":
                inbox = sorted((m for m in self.messages.values() if "INBOX" in m["labelIds"]),
                               key=lambda m: -int(m["internalDate"]))
                return httpx.Response(200, json={"messages": [
                    {"id": m["id"], "threadId": m["threadId"]}
                    for m in inbox[:int(q.get("maxResults", 100))]]})
            if path.startswith("/messages/"):
                mid = path.rsplit("/", 1)[1]
                if mid in self.messages and not (
                        q.get("format") == "metadata" and mid in self.gone_metadata):
                    return httpx.Response(200, json=self.messages[mid])
            if path.startswith("/threads/"):
                tid = path.rsplit("/", 1)[1]
                msgs = sorted((m for m in self.messages.values() if m["threadId"] == tid),
                              key=lambda m: int(m["internalDate"]))
                if msgs:
                    return httpx.Response(200, json={"id": tid, "messages": msgs})
            if path == "/drafts":
                return httpx.Response(200, json={"drafts": []})
        if request.method == "POST" and path == "/messages/send":
            return self._send(request)
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    def _history(self, q) -> httpx.Response:
        start = int(q["startHistoryId"])
        if start < self.expired_below:
            return httpx.Response(404, json={"error": {"message": "Requested entity was not found."}})
        recs = [(h, mid) for h, mid in self.added if h > start]
        size = self.page_size or int(q.get("maxResults", 100))
        offset = int(q.get("pageToken") or 0)
        body: dict = {"historyId": str(self.history_id), "history": [
            {"id": str(h), "messagesAdded": [{"message": {
                "id": mid, "threadId": self.messages[mid]["threadId"],
                "labelIds": self.messages[mid]["labelIds"]}}]}
            for h, mid in recs[offset:offset + size]]}
        if offset + size < len(recs):
            body["nextPageToken"] = str(offset + size)
        return httpx.Response(200, json=body)

    def _send(self, request: httpx.Request) -> httpx.Response:
        if self.send_status != 200:
            return httpx.Response(self.send_status, json={"error": {"message": "boom"}})
        data = json.loads(request.content)
        msg = email.message_from_bytes(
            base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4)),
            policy=email.policy.default)
        self.n_sent += 1
        sid = f"sent-{self.n_sent}"
        tid = data.get("threadId") or f"th-{sid}"
        self.messages[sid] = {
            "id": sid, "threadId": tid, "labelIds": ["SENT"], "internalDate": str(_ms(_now())),
            "payload": {"mimeType": "text/plain",
                        "headers": [{"name": k, "value": str(v)} for k, v in msg.items()],
                        "body": {"size": 1, "data": _b64("x")}}}
        return httpx.Response(200, json={"id": sid, "threadId": tid})

    def sends(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and self.path(r) == "/messages/send"]

    def wire(self, i: int = -1) -> email.message.EmailMessage:
        data = json.loads(self.sends()[i].content)
        raw = data["raw"]
        return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                                        policy=email.policy.default)

    def message_gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"
                and self.path(r).startswith("/messages/")]


# ── the fake checkers and writer ────────────────────────────────────


class Fakes:
    """judges.classify / classify_single / judge and writer.resolve_writer /
    compose_reply, recording every call."""

    def __init__(self):
        self.classify_calls: list[tuple] = []
        self.single_calls: list[tuple] = []
        self.judge_calls: list[tuple] = []
        self.compose_calls: list[tuple] = []
        self.match = lambda inb, topics: judges.Match(
            topic_id=TOPIC.id, source="tier", model="p/m", confidence=0.95, fit="exact")
        self.single = lambda inb, topic: judges.Match(
            topic_id=topic.id, source="tier", model="p/m", confidence=0.95, fit="exact")
        self.verdict = judges.JudgeResult(passed=True, source="tier", model="p/m")
        self.writer: object = (REC, False)
        self.result = lambda inb, topic: writer.WriterResult(
            writer.REPLY, body=REPLY_TEXT, canary="CC-0123456789ab", shareable=(topic.brief,),
            inbound=(inb.subject, inb.text, gi.sender_address(inb)),
            sources_used=("playbooks",),
            usage=({"stage": "compose", "model": "m-1", "tokens": 42},))
        self.on_judge = None

    async def classify(self, owner, inb, topics, *, db=None, stage="match"):
        topics = list(topics)
        self.classify_calls.append((owner, inb.message_id, tuple(t.id for t in topics), stage))
        return self.match(inb, topics)

    async def classify_single(self, owner, inb, topic, *, db=None, stage="dry_run"):
        self.single_calls.append((owner, inb.message_id, topic.id, stage))
        return self.single(inb, topic)

    async def judge(self, owner, topic, shareable, background, inb, reply, *, thread=(),
                    db=None, stage="judge"):
        self.judge_calls.append((owner, topic.id, tuple(shareable), reply, stage))
        if self.on_judge is not None:
            await self.on_judge()
        return self.verdict

    async def resolve_writer(self, db, owner, ref):
        return self.writer

    async def compose_reply(self, db, owner, cfg, topic, inb, thread_msgs, rec, shared, *,
                            own_addresses=(), now=None):
        self.compose_calls.append((owner, topic.id, inb.message_id, len(list(thread_msgs)),
                                   tuple(own_addresses)))
        return self.result(inb, topic)


def _reply(body: str, background: tuple[str, ...] = ()):
    return lambda inb, topic: writer.WriterResult(
        writer.REPLY, body=body, canary="CC-0123456789ab", shareable=(topic.brief,),
        inbound=(inb.subject, inb.text, gi.sender_address(inb)), background=background)


# ── fixtures ─────────────────────────────────────────────────────────


def _tokens(scope: str = SCOPES) -> GoogleOAuthTokens:
    return GoogleOAuthTokens(access_token="alice-access", refresh_token="alice-refresh",
                             token_type="Bearer", expires_at=time.time() + 3600, scope=scope)


async def _enable(db, gmail: FakeGmail, *, enabled_at: datetime | None = None, **cfg_kw):
    """Turn auto-replies on directly (config + state row), as the enabling PUT
    leaves them — two hours ago by default, so fresh mail passes check 2."""
    at = enabled_at or _now() - timedelta(hours=2)
    cfg = arcfg.AutoReplyConfig(**{"agent_ref": REF, "topics": (TOPIC,), **cfg_kw})
    cfg = arcfg.enable(cfg, google_sub=SUB, account_email=OWN, now=at)
    await arcfg.save_autoreply_config(db, ALICE, cfg)
    await db.set_autoreply_state(ALICE, history_id=str(gmail.history_id), google_sub=SUB,
                                 account_email=OWN, enabled_at=cfg.enabled_at,
                                 own_addresses=[OWN], own_addresses_at=_now().isoformat())
    return cfg


@pytest.fixture()
async def env(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("FD_DATA_DIR", str(tmp_path / "fd-data"))
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    for var in ("FD_LOCKDOWN", "FD_GMAIL_SEND", "FD_GMAIL_AUTOREPLY",
                "FD_GMAIL_REPEAT_CHECK_DAYS", "FD_GMAIL_AUTOREPLY_CLASSIFY_PER_DAY",
                "FD_GMAIL_AUTOREPLY_POLL_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(events, "_STORE", None)
    monkeypatch.setattr(autonomy, "_STORE", None)
    monkeypatch.setattr(engine, "_RUNTIME", None)
    d = FlightDeckDB(str(tmp_path / "fd.db"))
    await d.init()
    prev_db = auth._db
    auth.set_auth_db(d)
    stamp = "2026-01-01T00:00:00Z"
    for uid, mail_, role in [(ALICE, "alice@x.co", "admin"), (BOB, "bob@x.co", "user")]:
        await d._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, mail_, "h", uid.title(), role, stamp, stamp))
    await d._db.commit()
    await d.set_system_setting(gr._K_CLIENT_ID, "cid")
    await d.set_system_setting(gr._K_CLIENT_SECRET, "csecret")
    gr._primary_owner_cache.update(id=ALICE, at=time.time())
    gs._policy_locks.clear()
    gs._send_locks.clear()
    await gr._store_tokens(d, ALICE, _tokens())
    await gr._store_user(d, ALICE, {"sub": SUB, "email": OWN})
    await d.set_settings(ALICE, {mail_signature.GOOGLE_KEY: SIGNATURE})

    gmail = FakeGmail()
    agent_requests: list[httpx.Request] = []

    def router(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gmail.googleapis.com":
            return gmail(request)
        agent_requests.append(request)
        return httpx.Response(404, json={"ok": False})

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(router), **kw))
    fakes = Fakes()
    monkeypatch.setattr(judges, "classify", fakes.classify)
    monkeypatch.setattr(judges, "classify_single", fakes.classify_single)
    monkeypatch.setattr(judges, "judge", fakes.judge)
    monkeypatch.setattr(writer, "resolve_writer", fakes.resolve_writer)
    monkeypatch.setattr(writer, "compose_reply", fakes.compose_reply)
    monkeypatch.setattr(agent_sharing, "resolve_agent_record", lambda ref, strict=False: REC)
    try:
        yield SimpleNamespace(db=d, gmail=gmail, fakes=fakes, agent_requests=agent_requests,
                              tmp=tmp_path)
    finally:
        gr._primary_owner_cache.update(id=None, at=0.0)
        gs._policy_locks.clear()
        gs._send_locks.clear()
        auth.set_auth_db(prev_db)
        store = events._STORE
        if store is not None and store._conn is not None:
            store._conn.close()
        await d.close()


@pytest.fixture()
async def on(env):
    """``env`` with Alice's auto-replies on."""
    env.cfg = await _enable(env.db, env.gmail)
    return env


async def tick(e) -> None:
    await engine.run_once(e.db)


async def rows(db, owner: str = ALICE) -> list[dict]:
    return await db.list_autoreplies(owner, limit=500, include_quiet=True)


async def row_for(db, mid: str) -> dict:
    found = [r for r in await rows(db) if r["inbound_message_id"] == mid]
    assert len(found) == 1, found
    return found[0]


async def bells(db, kind: str | None = None) -> list[dict]:
    out = await db.list_notifications(ALICE, limit=200)
    return [b for b in out if kind is None or b["type"] == kind]


async def ledger_row(db, mid: str, *, state: str = "sent", thread: str = "th-other",
                     sender: str = SENDER, age_h: float = 0, **fields) -> str:
    """A ledger row as an earlier run would have left it."""
    rid = await db.claim_autoreply(ALICE, mid, thread)
    await db.update_autoreply(rid, ALICE, from_addr=sender, thread_id=thread, **fields)
    if state != "new":
        assert await db.cas_autoreply(rid, ALICE, "new", state)
    if age_h:
        ts = (_now() - timedelta(hours=age_h)).isoformat()
        await db._db.execute("UPDATE gmail_autoreplies SET created_at = ?, updated_at = ?"
                             " WHERE id = ?", (ts, ts, rid))
        await db._db.commit()
    return rid


async def state(db) -> dict | None:
    return await db.get_autoreply_state(ALICE)


async def rotate_token(e, new: str = "alice-access-2") -> None:
    """The tick's access token expires (Gmail answers 401 for it) and the
    deck holds a freshly refreshed one."""
    e.gmail.revoked.add("alice-access")
    await gr._store_tokens(e.db, ALICE, GoogleOAuthTokens(
        access_token=new, refresh_token="alice-refresh", token_type="Bearer",
        expires_at=time.time() + 3600, scope=SCOPES))


async def age_rows(db, hours: float) -> None:
    """Every ledger row as if it had been claimed and last changed *hours* earlier."""
    for r in await rows(db):
        stamps = [(datetime.fromisoformat(r[k]) - timedelta(hours=hours)).isoformat()
                  for k in ("created_at", "updated_at")]
        await db._db.execute("UPDATE gmail_autoreplies SET created_at = ?, updated_at = ?"
                             " WHERE id = ?", (*stamps, r["id"]))
    await db._db.commit()


# ── baseline and polling ────────────────────────────────────────────


class TestBaselineAndPolling:
    async def test_baseline_takes_the_cursor_and_verified_aliases(self, env):
        env.gmail.deliver(mail("old1"))
        cfg = arcfg.AutoReplyConfig(agent_ref=REF, topics=(TOPIC,))
        async with engine.config_lock(ALICE):
            stored = await engine.baseline_and_enable(env.db, ALICE, cfg)
        assert stored.enabled and stored.google_sub == SUB and stored.account_email == OWN
        st = await state(env.db)
        assert st["history_id"] == str(env.gmail.history_id)
        assert st["own_addresses"] == [OWN, ALIAS]          # the pending alias isn't own
        assert st["enabled_at"] == stored.enabled_at
        assert (await arcfg.load_autoreply_config(env.db, ALICE)).enabled

    async def test_pre_existing_mail_is_never_answered(self, env):
        env.gmail.deliver(mail("old1", age_s=30))
        cfg = arcfg.AutoReplyConfig(agent_ref=REF, topics=(TOPIC,))
        async with engine.config_lock(ALICE):
            await engine.baseline_and_enable(env.db, ALICE, cfg)
        await tick(env)
        assert await rows(env.db) == []                       # behind the cursor
        # Even replayed into the history, mail from before enabling is skipped.
        env.gmail.readd("old1")
        env.gmail.deliver(mail("new1", ms=_ms(_now()) + 1000))
        await tick(env)
        assert (await row_for(env.db, "old1"))["reason"] == "before_enable"
        assert [c[1] for c in env.fakes.classify_calls] == ["new1"]
        assert (await row_for(env.db, "new1"))["state"] == "sent"

    async def test_baseline_refusals_write_nothing(self, env):
        cfg = arcfg.AutoReplyConfig(agent_ref=REF, topics=(TOPIC,))
        env.gmail.profile_email = "someone.else@gmail.com"
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.baseline_and_enable(env.db, ALICE, cfg)
        assert exc.value.status == 409
        env.gmail.profile_email = OWN
        await env.db.set_autoreply_state(BOB, history_id="1", google_sub=SUB,
                                         enabled_at=_now().isoformat())
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.baseline_and_enable(env.db, ALICE, cfg)
        assert exc.value.status == 409 and "Another user" in exc.value.detail
        await env.db.delete_autoreply_state(BOB)
        await gr._store_user(env.db, ALICE, {"email": OWN})    # no sub
        with pytest.raises(engine.AutoReplyError):
            await engine.baseline_and_enable(env.db, ALICE, cfg)
        assert await state(env.db) is None
        assert not (await arcfg.load_autoreply_config(env.db, ALICE)).enabled

    async def test_claims_first_then_the_cursor(self, on):
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"
        st = await state(on.db)
        assert st["history_id"] == str(on.gmail.history_id)
        assert st["last_ok_at"] and st["last_error"] == ""

    async def test_cursor_stays_when_a_claim_fails(self, on, monkeypatch):
        start = (await state(on.db))["history_id"]
        on.gmail.deliver(mail("m1"))
        on.gmail.deliver(mail("m2"))
        real = on.db.claim_autoreply
        calls = {"n": 0}

        async def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk full")
            return await real(*a, **kw)

        monkeypatch.setattr(on.db, "claim_autoreply", flaky)
        await tick(on)
        assert (await state(on.db))["history_id"] == start      # not moved past m2
        monkeypatch.setattr(on.db, "claim_autoreply", real)
        await tick(on)
        assert {r["inbound_message_id"] for r in await rows(on.db)} == {"m1", "m2"}
        assert (await state(on.db))["history_id"] == str(on.gmail.history_id)

    async def test_same_id_across_pages_and_concurrent_polls_is_one_row(self, on):
        on.gmail.page_size = 1
        on.gmail.deliver(mail("m1"))
        on.gmail.deliver(mail("m2"))
        on.gmail.readd("m1")
        st = await state(on.db)
        async with httpx.AsyncClient() as client:
            await asyncio.gather(engine._poll(on.db, ALICE, client, "tok", st),
                                 engine._poll(on.db, ALICE, client, "tok", st))
        got = [r["inbound_message_id"] for r in await rows(on.db)]
        assert sorted(got) == ["m1", "m2"]

    async def test_burst_works_the_newest_twenty(self, on):
        for i in range(25):
            on.gmail.deliver(mail(f"b{i:02d}"))
        async with httpx.AsyncClient() as client:
            await engine._poll(on.db, ALICE, client, "tok", await state(on.db))
        by_id = {r["inbound_message_id"]: r for r in await rows(on.db)}
        assert len(by_id) == 25
        assert {m for m, r in by_id.items() if r["state"] == "skipped"} == {
            f"b{i:02d}" for i in range(5)}
        assert all(by_id[f"b{i:02d}"]["reason"] == "burst" for i in range(5))
        assert all(by_id[f"b{i:02d}"]["state"] == "new" for i in range(5, 25))

    async def test_more_than_five_pages_claims_the_newest_hour_not_the_oldest_pages(self, on):
        # History pages are oldest first: what five pages hold is the OLDEST
        # mail. Claiming it and jumping the cursor dropped the newest for good.
        on.gmail.page_size = 1
        for i in range(7):
            on.gmail.deliver(mail(f"p{i}"))
        async with httpx.AsyncClient() as client:
            await engine._poll(on.db, ALICE, client, "tok", await state(on.db))
        by_id = {r["inbound_message_id"]: r["state"] for r in await rows(on.db)}
        assert by_id == {f"p{i}": "new" for i in range(7)}     # p5 and p6 too
        st = await state(on.db)
        assert st["history_id"] == str(on.gmail.history_id)
        assert st["last_error"] == "burst_truncated"
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == [
            "burst_truncated"]
        # Pages 1–5 were never claimed on their own: the backfill did it.
        assert any(on.gmail.path(r) == "/messages" for r in on.gmail.requests)

    async def test_expired_cursor_backfills_then_rebaselines(self, on):
        on.gmail.deliver(mail("m1", age_s=120))
        on.gmail.deliver(mail("m2", age_s=60, labels=("INBOX",)), history=False)
        on.gmail.expired_below = 10 ** 9
        await tick(on)
        st = await state(on.db)
        assert st["history_id"] == str(on.gmail.history_id)
        assert st["last_error"] == "history_expired"
        assert {r["inbound_message_id"] for r in await rows(on.db)} == {"m1", "m2"}
        issues = await bells(on.db, engine.BELL_ISSUE)
        assert [b["ref_id"] for b in issues] == ["history_expired"]
        await tick(on)
        assert len(await bells(on.db, engine.BELL_ISSUE)) == 1   # once a day

    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_errors_keep_the_cursor(self, on, status):
        start = (await state(on.db))["history_id"]
        on.gmail.deliver(mail("m1"))
        on.gmail.forced[("GET", "/history")] = status
        await tick(on)
        st = await state(on.db)
        assert st["history_id"] == start and st["last_error"] == f"gmail_{status}"
        assert await rows(on.db) == []


# ── restart safety ──────────────────────────────────────────────────


class TestRecovery:
    async def test_interrupted_send_is_unknown_and_never_resent(self, on):
        on.gmail.deliver(mail("m1"), history=False)
        rid = await ledger_row(on.db, "m1", state="sending", thread="th-m1", agent_name="Meetup bot",
                               subject="Where is the meetup?", reply_text=REPLY_TEXT)
        assert await engine.recover_on_start(on.db) == 1
        row = await on.db.get_autoreply(ALICE, rid)
        assert row["state"] == "unknown" and row["reply_text"] == REPLY_TEXT
        (bell,) = await bells(on.db, engine.BELL_ISSUE)
        assert bell["title"] == "Meetup bot may have auto-replied — check Sent"
        assert bell["ref_id"] == rid
        await tick(on)
        await tick(on)
        assert on.gmail.sends() == []
        assert (await on.db.get_autoreply(ALICE, rid))["state"] == "unknown"
        assert events.get_store().add_event(ALICE, source="gmail", dedup_key="gmail:m1") is None

    async def test_interrupted_work_is_requeued_then_failed(self, on):
        fresh = await ledger_row(on.db, "w1", state="writing")
        tired = await ledger_row(on.db, "w2", state="writing", attempts=2)
        await engine.recover_on_start(on.db)
        a, b = (await on.db.get_autoreply(ALICE, fresh), await on.db.get_autoreply(ALICE, tired))
        assert (a["state"], a["attempts"]) == ("new", 1)
        assert (b["state"], b["reason"]) == ("failed", "interrupted")


# ── checks 1–11 ─────────────────────────────────────────────────────


PREFILTER_CASES = [
    ("labels", dict(labels=("INBOX", "CATEGORY_PROMOTIONS"))),
    ("before_enable", dict(age_s=3 * 3600)),
    ("stale", dict(age_s=90 * 60)),
    ("from_invalid", dict(frm=f"Alice <{SENDER}>, Bob <bob@example.com>")),
    ("own_mail", dict(frm=f"Me <{OWN}>")),
    ("automated_sender", dict(frm="Robot <noreply@example.com>")),
    ("auto_or_list", dict(headers=(("List-Id", "<news.example.com>"),))),
    ("auto_or_list", dict(subject="Automatic reply: Out of office")),
    ("unauthenticated", dict(auth=False)),
    ("reply_to_divergent", dict(headers=(("Reply-To", "evil@attacker.example"),))),
    ("not_addressed_to_me", dict(to="someone@else.example")),
    ("group_mail", dict(to=", ".join([OWN] + [f"p{i}@else.example" for i in range(5)]))),
    ("empty", dict(body="> only a quote\n> nothing new")),
    ("too_long", dict(body="word " * 5000)),
]


class TestPrefilters:
    @pytest.mark.parametrize("code,kw", PREFILTER_CASES, ids=[c for c, _ in PREFILTER_CASES])
    async def test_every_skip_code_runs_no_model(self, on, code, kw):
        on.gmail.deliver(mail("m1", **kw))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", code)
        assert on.fakes.classify_calls == [] and on.fakes.compose_calls == []
        assert on.gmail.sends() == []
        # A skip is not handled mail: the event spine still sees it.
        assert events.get_store().add_event(ALICE, source="gmail", dedup_key="gmail:m1")

    async def test_the_row_records_sender_subject_and_thread(self, on):
        on.gmail.deliver(mail("m1", labels=("INBOX", "CATEGORY_SOCIAL"), thread="th-z"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["from_addr"], row["from_domain"], row["subject"], row["thread_id"]) == (
            SENDER, "example.com", "Where is the meetup?", "th-z")
        assert row["received_at"]

    async def test_fetch_errors_retry_then_fail(self, on):
        on.gmail.deliver(mail("m1"))
        on.gmail.forced[("GET", "/messages/m1")] = 503
        for _ in range(4):
            await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("failed", "fetch")
        assert len([r for r in on.gmail.message_gets() if on.gmail.path(r) == "/messages/m1"]) == 3
        assert row["attempts"] == 0          # the restart budget is left alone
        assert on.fakes.classify_calls == []
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == ["fetch"]

    async def test_a_gone_message_is_skipped(self, on):
        on.gmail.deliver(mail("m1"))
        on.gmail.forced[("GET", "/messages/m1")] = 404
        await tick(on)
        assert (await row_for(on.db, "m1"))["reason"] == "gone"


CAP_CASES = ["cap_thread", "cap_thread_lifetime", "cap_sender", "cap_flood", "cap_daily",
             "cap_classify"]


class TestCaps:
    @pytest.mark.parametrize("case", CAP_CASES)
    async def test_caps_stop_before_any_model(self, on, case, monkeypatch):
        thread = "th-shared"
        expected = case
        if case == "cap_thread":
            await ledger_row(on.db, "earlier", thread=thread, sender="x@else.example")
        elif case == "cap_thread_lifetime":
            expected = "cap_thread"
            for i in range(3):
                await ledger_row(on.db, f"e{i}", thread=thread, sender="x@else.example",
                                 age_h=48 + i)
        elif case == "cap_sender":
            for i in range(3):
                await ledger_row(on.db, f"e{i}", thread=f"th-{i}")
        elif case == "cap_flood":
            for i in range(11):
                await ledger_row(on.db, f"e{i}", state="skipped", thread=f"th-{i}")
        elif case == "cap_daily":
            await _enable(on.db, on.gmail, daily_limit=2)
            for i in range(2):
                await ledger_row(on.db, f"e{i}", thread=f"th-{i}", sender=f"s{i}@else.example")
        else:
            monkeypatch.setenv("FD_GMAIL_AUTOREPLY_CLASSIFY_PER_DAY", "20")
            await on.db.update_autoreply_state(ALICE, classify_day=_now().date().isoformat(),
                                               classify_count=20)
        on.gmail.deliver(mail("m1", thread=thread))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", expected)
        assert on.fakes.classify_calls == [] and on.gmail.sends() == []
        if case in ("cap_daily", "cap_classify"):
            assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == [case]

    async def test_a_flooding_sender_stays_suppressed_for_a_day(self, on):
        await ledger_row(on.db, "flood", state="skipped", thread="th-f", reason="cap_flood",
                         age_h=5)
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["reason"] == "flood_suppressed"
        assert on.fakes.classify_calls == []

    async def test_the_classification_budget_is_counted(self, on, monkeypatch):
        monkeypatch.setenv("FD_GMAIL_AUTOREPLY_CLASSIFY_PER_DAY", "20")
        on.fakes.match = lambda inb, topics: judges.Match(reason="no_match", source="tier",
                                                          fit="none")
        for i in range(3):
            on.gmail.deliver(mail(f"m{i}", thread=f"th-{i}"))
        await tick(on)
        assert (await state(on.db))["classify_count"] == 3
        assert len(on.fakes.classify_calls) == 3


class TestTakeover:
    async def test_owner_reply_in_the_thread_hands_off(self, on):
        on.gmail.deliver(sent_mail("mine", "th-t"), history=False)
        on.gmail.deliver(mail("m1", thread="th-t"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", "owner_in_thread")
        assert len(on.fakes.classify_calls) == 1 and on.fakes.compose_calls == []

    async def test_a_draft_in_the_thread_hands_off(self, on):
        draft = sent_mail("d1", "th-t")
        draft["labelIds"] = ["DRAFT"]
        on.gmail.deliver(draft, history=False)
        on.gmail.deliver(mail("m1", thread="th-t"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["reason"] == "draft_in_thread"

    async def test_our_own_and_automated_replies_dont_count(self, on):
        # An earlier auto-reply of ours (2 days ago, so the thread cap allows
        # one more) and an Auto-Submitted one from elsewhere.
        await ledger_row(on.db, "old-in", thread="th-t", sender=SENDER, age_h=48,
                         sent_message_id="ours-1")
        on.gmail.deliver(sent_mail("ours-1", "th-t"), history=False)
        on.gmail.deliver(sent_mail("vacation", "th-t", auto=True), history=False)
        on.gmail.deliver(mail("m1", thread="th-t"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"
        assert on.fakes.compose_calls[0][3] == 3              # the writer got the thread

    async def test_an_unreadable_thread_fails_closed(self, on):
        on.gmail.deliver(mail("m1", thread="th-t"))
        on.gmail.forced[("GET", "/threads/th-t")] = 500
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("failed", "thread_unreadable")
        assert on.fakes.compose_calls == [] and on.gmail.sends() == []


# ── the agent ───────────────────────────────────────────────────────


class TestAgentOffline:
    async def test_waits_then_fails_after_an_hour_with_one_bell(self, on, monkeypatch):
        on.fakes.writer = writer.AGENT_STOPPED
        on.gmail.deliver(mail("m1"))
        on.gmail.deliver(mail("m2", thread="th-other"))
        await tick(on)
        await tick(on)
        assert {r["state"] for r in await rows(on.db)} == {"waiting_agent"}
        assert len(on.fakes.classify_calls) == 2            # classified once each
        later = _now() + timedelta(minutes=61)
        monkeypatch.setattr(engine, "_now", lambda: later)
        await tick(on)
        assert {(r["state"], r["reason"]) for r in await rows(on.db)} == {
            ("failed", "agent_offline")}
        (bell,) = await bells(on.db, engine.BELL_ISSUE)
        assert bell["title"] == "Meetup bot is stopped — topic emails are waiting for you"
        assert on.fakes.compose_calls == [] and on.gmail.sends() == []

    async def test_resumes_when_the_agent_is_back(self, on):
        on.fakes.writer = writer.AGENT_STOPPED
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "waiting_agent"
        on.fakes.writer = (REC, False)
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"
        assert len(on.fakes.classify_calls) == 1

    async def test_a_missing_agent_fails_with_a_bell(self, on):
        on.fakes.writer = writer.AGENT_NOT_FOUND
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("failed", "agent_not_found")
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == ["agent_not_found"]


# ── the guard ───────────────────────────────────────────────────────


class TestGuard:
    async def test_l1_block_keeps_no_text_and_no_judge_sees_it(self, on):
        on.fakes.result = _reply(f"Sure — pay with card {CARD}, see you there!")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert row["state"] == "blocked" and row["reason"].startswith("guard:")
        assert "card_number" in row["reason"] and row["reply_text"] == ""
        assert {f["kind"] for f in row["findings"]} >= {"card_number"}
        assert "4111" not in json.dumps(row)
        assert on.fakes.judge_calls == [] and on.gmail.sends() == []
        (bell,) = await bells(on.db, engine.BELL_BLOCKED)
        assert bell["title"] == "Auto-reply blocked: Meetup logistics"
        assert "a card number" in bell["body"] and "4111" not in bell["body"]
        assert events.get_store().add_event(ALICE, source="gmail", dedup_key="gmail:m1") is None

    async def test_l1_hold_keeps_the_text_and_no_judge_sees_it(self, on):
        text = REPLY_TEXT + " Questions? Write to carol@else.example."
        on.fakes.result = _reply(text)
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("held", "guard:email_address")
        assert row["reply_text"] == text
        assert on.fakes.judge_calls == [] and on.gmail.sends() == []
        (bell,) = await bells(on.db, engine.BELL_HELD)
        assert bell["title"] == "Auto-reply waiting for you: Meetup logistics"
        assert bell["body"] == (f"From {SENDER} — Where is the meetup? — "
                                "it mentions an email address")

    async def test_a_judge_flag_holds(self, on):
        on.fakes.verdict = judges.JudgeResult(passed=False, failing=("off_topic",),
                                              scores={"off_topic": True}, source="tier")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("held", "judge:off_topic")
        assert row["judge"]["failing"] == ["off_topic"] and row["reply_text"] == REPLY_TEXT
        assert on.gmail.sends() == []
        shareable = on.fakes.judge_calls[0][2]
        assert SIGNATURE in shareable

    async def test_no_judge_holds(self, on):
        on.fakes.verdict = judges.JudgeResult(unavailable=True, source="tier")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["reason"] == "judge_unavailable"

    async def test_three_leaking_blocks_pause_auto_replies(self, on):
        # A never-disclose term the writer's own material held (a background
        # note the pre-scan missed): leak evidence.
        await _enable(on.db, on.gmail, never_disclose=("Zvijezda",))
        on.fakes.result = _reply("Project Zvijezda is behind the meetup. See you there!",
                                 background=("[insights] Project Zvijezda funds the meetup.",))
        for i in range(3):
            on.gmail.deliver(mail(f"m{i}", thread=f"th-{i}", frm=f"P{i} <p{i}@example.com>",
                                  headers=()))
        # Each sender is distinct but authenticated by example.com's DKIM/DMARC.
        await tick(on)
        assert [r["state"] for r in await rows(on.db)] == ["blocked"] * 3
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        assert (cfg.enabled, cfg.disabled_reason) == (False, "auto_paused")
        assert await state(on.db) is None
        titles = [b["title"] for b in await bells(on.db, engine.BELL_ISSUE)]
        assert titles == ["Auto-replies paused: three replies contained data that must never "
                          "be sent — review them."]

    async def test_echo_blocks_dont_pause(self, on):
        on.fakes.result = _reply(f"You asked about card {CARD}: see you at the meetup!")
        for i in range(3):
            on.gmail.deliver(mail(f"m{i}", thread=f"th-{i}", frm=f"P{i} <p{i}@example.com>",
                                  body=f"Is my card {CARD} fine for the meetup?"))
        await tick(on)
        assert [r["state"] for r in await rows(on.db)] == ["blocked"] * 3
        assert all(f["echo"] for r in await rows(on.db) for f in r["findings"])
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).enabled

    async def test_blocks_from_before_enabling_dont_count(self, on):
        await _enable(on.db, on.gmail, never_disclose=("Zvijezda",))
        leak = [{"kind": "never_disclose", "level": "block", "hint": "never_disclose: Zv…da",
                 "echo": False}]
        for i in range(2):   # blocked before the owner (re-)enabled two hours ago
            await ledger_row(on.db, f"old{i}", state="blocked", thread=f"th-o{i}",
                             findings=leak, age_h=3)
        on.fakes.result = _reply("Project Zvijezda is behind the meetup. See you there!")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "blocked"
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).enabled

    async def test_format_blocks_dont_pause(self, on):
        on.fakes.result = lambda inb, topic: writer.WriterResult(
            writer.BLOCKED, reason=writer.FORMAT, findings=(dict(writer.FORMAT_FINDING),))
        for i in range(3):
            on.gmail.deliver(mail(f"m{i}", thread=f"th-{i}", frm=f"P{i} <p{i}@example.com>"))
        await tick(on)
        assert {(r["state"], r["reason"]) for r in await rows(on.db)} == {("blocked", "format")}
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).enabled
        assert "formatting" in (await bells(on.db, engine.BELL_BLOCKED))[0]["body"]

    async def test_writer_skip_and_failure(self, on):
        on.fakes.result = lambda inb, topic: writer.WriterResult(
            writer.SKIP, reason=writer.WRITER_SKIP, detail="needs the owner")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"], row["detail"]) == (
            "skipped", "writer_skip", "needs the owner")
        on.fakes.result = lambda inb, topic: writer.WriterResult(
            writer.FAILED, reason=writer.WRITER_UNAVAILABLE)
        on.gmail.deliver(mail("m2", thread="th-2"))
        await tick(on)
        row = await row_for(on.db, "m2")
        assert (row["state"], row["reason"]) == ("failed", "writer_unavailable")
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == [
            "writer_unavailable"]

    async def test_a_classifier_failure_retries_then_fails_once(self, on):
        on.fakes.match = lambda inb, topics: judges.Match(reason="classifier_unavailable",
                                                          source="tier", detail="tier 502")
        on.gmail.deliver(mail("m1"))
        for _ in range(4):
            await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("failed", "classifier_unavailable")
        assert row["match_judge"] == ""                     # no classification ran
        assert len(on.fakes.classify_calls) == 3
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == [
            "classifier_unavailable"]

    @pytest.mark.parametrize("reason", ["no_match", "partial_match", "mixed_topic",
                                        "sender_not_in_topic"])
    async def test_match_outcomes_skip_quietly(self, on, reason):
        on.fakes.match = lambda inb, topics: judges.Match(
            reason=reason, candidate=TOPIC.id, source="jev", fit="partial", confidence=0.5)
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"], row["topic_id"]) == ("skipped", reason, "")
        assert (row["match_judge"], row["topic_name"]) == ("jev", TOPIC.name)
        assert await bells(on.db) == [] and on.fakes.compose_calls == []


# ── the send ────────────────────────────────────────────────────────


class TestSendShape:
    async def test_what_goes_on_the_wire(self, on):
        on.gmail.deliver(mail("m1", frm=f"Alice Example <{SENDER.upper()}>",
                              headers=(("Reply-To", f"Alice <{SENDER.upper()}>"),)))
        await tick(on)
        (send,) = on.gmail.sends()
        assert json.loads(send.content)["threadId"] == "th-m1"
        wire = on.gmail.wire()
        assert wire["To"] == SENDER
        assert wire["Cc"] is None and wire["Bcc"] is None
        assert wire["Subject"] == "Re: Where is the meetup?"
        assert wire["In-Reply-To"] == "<m1@example.com>"
        assert "<m1@example.com>" in wire["References"]
        assert wire["Auto-Submitted"] == "auto-replied"
        assert wire["X-Auto-Response-Suppress"] == "All"
        plain = wire.get_body(("plain",)).get_content()
        html = wire.get_body(("html",)).get_content()
        assert plain.count(SIGNATURE) == 1 and plain.startswith(REPLY_TEXT)
        assert "snacks &amp; drinks" in html
        row = await row_for(on.db, "m1")
        assert row["state"] == "sent" and row["sent_message_id"] == "sent-1"
        assert row["reply_text"].count(SIGNATURE) == 1
        (audit,) = await on.db.list_gmail_sends(ALICE)
        assert (audit["origin"], audit["topic_id"], audit["in_reply_to_message_id"],
                audit["agent"], audit["status"]) == (
            "autoreply", TOPIC.id, "m1", "Meetup bot", "sent")
        assert await bells(on.db, "email_sent") == []
        (bell,) = await bells(on.db, engine.BELL_SENT)
        assert bell["title"] == "Meetup bot auto-replied: Meetup logistics"
        assert bell["body"] == f"To {SENDER} — Re: Where is the meetup?"
        assert not any("/drafts" in r.url.path for r in on.gmail.requests if r.method != "GET")

    async def test_a_reply_to_never_redirects_the_reply(self, on, monkeypatch):
        monkeypatch.setattr(gi, "reply_to_ok", lambda inb: True)   # even past check 7
        on.gmail.deliver(mail("m1", headers=(("Reply-To", "evil@attacker.example"),)))
        await tick(on)
        wire = on.gmail.wire()
        assert wire["To"] == SENDER
        assert "attacker" not in str(wire)

    async def test_no_subject_is_re_no_subject(self, on):
        on.gmail.deliver(mail("m1", subject=None))
        await tick(on)
        assert on.gmail.wire()["Subject"] == "Re: (no subject)"

    async def test_usage_is_logged_without_content(self, on):
        on.gmail.deliver(mail("m1"))
        await tick(on)
        logs = await on.db.get_usage_logs(ALICE, event_type="gmail_autoreply")
        details = [json.loads(r["detail"]) for r in logs]
        assert {"stage": "compose", "source": "agent", "model": "m-1", "tokens": 42} in details
        assert all("Meetup" not in r["detail"] for r in logs)

    async def test_held_and_sent_mail_is_claimed_on_the_spine(self, on):
        on.gmail.deliver(mail("m1"))
        await tick(on)
        store = events.get_store()
        assert store.add_event(ALICE, source="gmail", dedup_key="gmail:m1") is None


class TestSwitches:
    async def test_the_send_policy_being_off_doesnt_matter(self, on):
        # Decision 3: auto-replies are their own opt-in.
        await on.db.set_settings(ALICE, {gs._K_GMAIL_SEND: json.dumps(
            {"enabled": False, "allowed_recipients": [], "daily_limit": 50})})
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert len(on.gmail.sends()) == 1

    async def test_a_sender_off_the_allowlist_still_gets_the_reply(self, on):
        # Decision 1: the agents' allowlist doesn't bind topic replies.
        await on.db.set_settings(ALICE, {gs._K_GMAIL_SEND: json.dumps(
            {"enabled": True, "allowed_recipients": ["boss@corp.example"], "daily_limit": 50})})
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert on.gmail.wire()["To"] == SENDER

    @pytest.mark.parametrize("var", ["FD_GMAIL_SEND", "FD_GMAIL_AUTOREPLY"])
    async def test_deck_switches_stop_everything(self, on, monkeypatch, var):
        monkeypatch.setenv(var, "off")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert on.gmail.requests == [] and await rows(on.db) == []

    @pytest.mark.parametrize("how", ["config_off", "topic_off", "agent_changed", "deck_send",
                                     "deck_autoreply_in_lock"])
    async def test_switched_off_mid_flight_sends_nothing(self, on, monkeypatch, how):
        async def flip():
            cfg = await arcfg.load_autoreply_config(on.db, ALICE)
            if how == "config_off":
                await arcfg.save_autoreply_config(on.db, ALICE, arcfg.disable(cfg, "paused_by_user"))
            elif how == "topic_off":
                await arcfg.save_autoreply_config(on.db, ALICE, arcfg.disable_topic(cfg, TOPIC.id))
            elif how == "agent_changed":
                await arcfg.save_autoreply_config(
                    on.db, ALICE, replace(cfg, agent_ref="process:other-bot:fedcba9876543210"))
            elif how == "deck_send":
                monkeypatch.setenv("FD_GMAIL_SEND", "off")

        if how == "deck_autoreply_in_lock":
            # Flipped after every pre-send check, right before the send lock.
            real = gmail_compose.find_repeats

            async def find_repeats(*a, **kw):
                monkeypatch.setenv("FD_GMAIL_AUTOREPLY", "off")
                return await real(*a, **kw)

            monkeypatch.setattr(gs.gmail_compose, "find_repeats", find_repeats)
        else:
            on.fakes.on_judge = flip
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", "turned_off")
        assert on.gmail.sends() == []
        assert await on.db.list_gmail_sends(ALICE) == []

    @pytest.mark.parametrize("cap", ["cap_thread", "cap_sender", "cap_daily"])
    async def test_caps_are_rechecked_in_the_send_lock(self, on, cap):
        async def race():
            # Another reply went out while this one was being written.
            if cap == "cap_thread":
                await ledger_row(on.db, "racer", thread="th-m1", sender="x@else.example")
            elif cap == "cap_sender":
                for i in range(3):
                    await ledger_row(on.db, f"racer{i}", thread=f"th-r{i}")
            else:
                for i in range(on.cfg.daily_limit):
                    await ledger_row(on.db, f"racer{i}", thread=f"th-r{i}",
                                     sender=f"r{i}@else.example")

        on.fakes.on_judge = race
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", cap)
        assert on.gmail.sends() == []


class TestSendOutcomes:
    async def test_a_repeat_is_already_answered(self, on, monkeypatch):
        async def repeats(*a, **kw):
            return [{"kind": "sent", "draft_id": "", "message_id": "x", "thread_id": "th-m1",
                     "to": SENDER, "cc": "", "bcc": "", "subject": "Re: Where?", "date": ""}]

        monkeypatch.setattr(gs.gmail_compose, "find_repeats", repeats)
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["reason"] == "already_answered"
        assert on.gmail.sends() == []

    async def test_the_send_cores_daily_limit_is_cap_daily(self, on):
        for _ in range(on.cfg.daily_limit):
            await on.db.add_gmail_send(ALICE, origin="autoreply", to_addrs="x@else.example")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["reason"] == "cap_daily"
        assert on.gmail.sends() == []
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == ["cap_daily"]

    @pytest.mark.parametrize("status", [403, 429])
    async def test_gmail_refusing_is_a_send_error_not_a_limit_or_a_switch(self, on, status):
        # Gmail's own 403 / rate-limit 429 is no limit or switch of the owner's:
        # not cap_daily (and its false "daily limit reached" bell), not a quiet
        # turned_off — a send error with its own bell.
        on.gmail.send_status = status
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("failed", "send_error")
        # ... so a genuine daily-limit bell later that day isn't deduplicated away.
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == ["send_error"]

    async def test_an_inbound_gone_at_send_time_is_gone(self, on):
        on.gmail.gone_metadata.add("m1")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["reason"] == "gone"
        assert on.gmail.sends() == []

    async def test_an_unknown_outcome_is_never_retried(self, on):
        on.gmail.send_status = 500
        on.gmail.deliver(mail("m1"))
        await tick(on)
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("unknown", "send_unconfirmed")
        assert len(on.gmail.sends()) == 1
        (audit,) = await on.db.list_gmail_sends(ALICE)
        assert (audit["status"], audit["origin"]) == ("unknown", "autoreply")
        (bell,) = await bells(on.db, engine.BELL_ISSUE)
        assert bell["title"] == "Meetup bot may have auto-replied — check Sent"
        assert await bells(on.db, "email_sent") == []

    @pytest.mark.parametrize("status,committed,expected", [
        (409, False, ("skipped", "already_answered")),
        ("limit", False, ("skipped", "cap_daily")),
        (429, False, ("failed", "send_error")),
        (429, True, ("failed", "send_error")),
        ("deck", False, ("skipped", "turned_off")),
        (403, False, ("failed", "send_error")),
        (404, False, ("skipped", "gone")),
        (400, False, ("failed", "build_error")),
        (502, False, ("failed", "send_error")),
        (502, True, ("failed", "send_error")),
        ("unknown", True, ("unknown", "send_unconfirmed")),
        ("crash", True, ("unknown", "send_unconfirmed")),
        ("crash", False, ("failed", "send_error")),
    ])
    async def test_exception_mapping(self, on, monkeypatch, status, committed, expected):
        async def send_autoreply(db, owner, *, before_send, **kw):
            if committed:
                await before_send()
            if status == "crash":
                raise ValueError("malformed 2xx answer")
            if status == "unknown":
                raise HTTPException(502, "maybe", headers={gmail_compose.SEND_OUTCOME_HEADER:
                                                           "unknown"})
            if status == "limit":      # the send core's own daily limit
                raise HTTPException(429, gs._limit_refusal(10, 10, "autoreply"))
            if status == "deck":       # the deck's FD_GMAIL_SEND switch
                raise gs._refused("Sending is off on this deck.", "deck-disabled")
            raise HTTPException(status, "Gmail refused: quota.")

        monkeypatch.setattr(gs, "send_autoreply", send_autoreply)
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == expected


# ── account binding ─────────────────────────────────────────────────


class TestAccount:
    async def test_another_google_account_switches_off(self, on):
        await gr._store_user(on.db, ALICE, {"sub": "someone-else", "email": OWN})
        on.gmail.deliver(mail("m1"))
        await tick(on)
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        assert (cfg.enabled, cfg.disabled_reason) == (False, "google_account_changed")
        assert await state(on.db) is None and on.gmail.requests == []
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == [
            "google_account_changed"]

    async def test_disconnecting_google_switches_off(self, on):
        await gr._clear_oauth_state(on.db, ALICE)
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        assert (cfg.enabled, cfg.disabled_reason) == (False, "google_disconnected")
        assert await state(on.db) is None
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == [
            "google_disconnected"]

    async def test_disconnecting_everyone_switches_off_and_is_a_no_op_for_others(self, on):
        await gr._clear_all_oauth_state(on.db)
        assert not (await arcfg.load_autoreply_config(on.db, ALICE)).enabled
        assert await on.db.get_setting(BOB, arcfg.AUTOREPLY_KEY) is None
        assert await on.db.list_notifications(BOB) == []

    async def test_a_missing_scope_pauses_with_one_bell(self, on):
        await gr._store_tokens(on.db, ALICE, _tokens(
            "openid email https://www.googleapis.com/auth/gmail.send"))
        on.gmail.deliver(mail("m1"))
        await tick(on)
        await tick(on)
        assert (await state(on.db))["last_error"] == "missing_scopes:gmail.readonly"
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == ["missing_scopes"]
        assert await rows(on.db) == [] and on.gmail.requests == []

    async def test_no_token_pauses_with_one_bell(self, on, monkeypatch):
        async def none(uid):
            return None

        monkeypatch.setattr(gr, "get_valid_google_access_token", none)
        await tick(on)
        assert (await state(on.db))["last_error"] == "google_not_connected"
        assert len(await bells(on.db, engine.BELL_ISSUE)) == 1

    async def test_a_disabled_config_drops_the_state_row(self, on):
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        await arcfg.save_autoreply_config(on.db, ALICE, arcfg.disable(cfg, "paused_by_user"))
        await tick(on)
        assert await state(on.db) is None and on.gmail.requests == []

    async def test_pause_switches_off_and_drops_the_state_row(self, on):
        cfg = await engine.disable(ALICE, "paused_by_user", db=on.db)
        assert (cfg.enabled, cfg.disabled_reason) == (False, "paused_by_user")
        assert await state(on.db) is None
        await tick(on)
        assert on.gmail.requests == []

    async def test_a_tick_never_drops_an_enable_in_progress(self, env):
        at = _now() - timedelta(minutes=1)
        lock = engine.config_lock(ALICE)
        async with lock:
            # The enabling PUT: the state row first, then the config.
            await env.db.set_autoreply_state(ALICE, history_id=str(env.gmail.history_id),
                                             google_sub=SUB, account_email=OWN,
                                             enabled_at=at.isoformat(), own_addresses=[OWN])
            running = asyncio.create_task(tick(env))
            for _ in range(200):                 # the tick read "off" and waits for the lock
                if lock._waiters:
                    break
                await asyncio.sleep(0.01)
            assert lock._waiters
            cfg = arcfg.enable(arcfg.AutoReplyConfig(agent_ref=REF, topics=(TOPIC,)),
                               google_sub=SUB, account_email=OWN, now=at)
            await arcfg.save_autoreply_config(env.db, ALICE, cfg)
        await running
        assert await state(env.db) is not None

    async def test_a_switch_off_spares_a_newer_enablement(self, on):
        assert not await engine._switch_off(on.db, ALICE, "google_account_changed",
                                            enabled_at="2020-01-01T00:00:00+00:00")
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).enabled
        assert await state(on.db) is not None

    async def test_no_topic_on_means_no_poll(self, on):
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        await arcfg.save_autoreply_config(on.db, ALICE, arcfg.disable_topic(cfg, TOPIC.id))
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await state(on.db))["last_error"] == "no_topics"
        assert on.gmail.requests == []


# ── isolation (PR #56) ──────────────────────────────────────────────


class TestIsolation:
    async def test_no_autonomy_ledger_and_no_dispatch(self, on, monkeypatch):
        called: list[str] = []

        def boom(name):
            def f(*a, **kw):
                called.append(name)
                raise AssertionError(name)
            return f

        monkeypatch.setattr(fd_dispatch, "dispatch_action", boom("dispatch"))
        monkeypatch.setattr(fd_dispatch, "should_auto_dispatch", boom("should"))
        on.gmail.deliver(mail("m1"))
        on.fakes.verdict = judges.JudgeResult(passed=True, source="tier")
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"
        assert called == []
        assert autonomy._STORE is None                      # never even opened
        store = autonomy.AutonomyStore(on.tmp / "fd-data" / "autonomy.db")
        assert store.list_actions(ALICE) == []


# ── owner actions on held cards ─────────────────────────────────────


async def _held(e, text: str = REPLY_TEXT + " Write to carol@else.example.") -> str:
    e.fakes.result = _reply(text)
    e.gmail.deliver(mail("m1"))
    await tick(e)
    row = await row_for(e.db, "m1")
    assert row["state"] == "held"
    return row["id"]


class TestHeldCards:
    async def test_send_is_uncapped_owner_approved_and_once(self, on):
        rid = await _held(on)
        for _ in range(on.cfg.daily_limit):
            await on.db.add_gmail_send(ALICE, origin="autoreply", to_addrs="x@else.example")
        out = await engine.held_send(on.db, ALICE, rid, ALICE)
        assert out["ok"] and out["to"] == SENDER
        row = await on.db.get_autoreply(ALICE, rid)
        assert (row["state"], row["approved_by"]) == ("sent", ALICE)
        audit = (await on.db.list_gmail_sends(ALICE, origin="autoreply_approved"))[0]
        assert audit["in_reply_to_message_id"] == "m1"
        wire = on.gmail.wire()
        assert wire["To"] == SENDER and wire["Auto-Submitted"] == "auto-replied"
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.held_send(on.db, ALICE, rid, ALICE)
        assert exc.value.status == 409
        assert len(on.gmail.sends()) == 1

    async def test_edit_and_send_uses_the_edited_text(self, on):
        rid = await _held(on)
        await engine.held_send(on.db, ALICE, rid, ALICE, "See you at the Hub on 14 November!")
        plain = on.gmail.wire().get_body(("plain",)).get_content()
        assert plain.startswith("See you at the Hub on 14 November!")

    async def test_an_edit_with_a_secret_is_422_and_stays_held(self, on):
        rid = await _held(on)
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.held_send(on.db, ALICE, rid, ALICE,
                                   "Use key sk-ant-" + "api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 ok")
        assert exc.value.status == 422
        assert "secret_key" in [f["kind"] for f in exc.value.findings]
        assert all(f["hint"] == "" for f in exc.value.findings)          # secrets: no hint
        assert (await on.db.get_autoreply(ALICE, rid))["state"] == "held"
        assert on.gmail.sends() == []

    async def test_owner_already_replied_is_409(self, on):
        rid = await _held(on)
        on.gmail.deliver(sent_mail("mine", "th-m1", ms=_ms(_now())), history=False)
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.held_send(on.db, ALICE, rid, ALICE)
        assert exc.value.status == 409
        assert (await on.db.get_autoreply(ALICE, rid))["state"] == "held"

    async def test_a_refused_send_goes_back_to_held(self, on):
        rid = await _held(on)
        on.gmail.send_status = 403
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.held_send(on.db, ALICE, rid, ALICE)
        assert exc.value.status == 403
        row = await on.db.get_autoreply(ALICE, rid)
        assert (row["state"], row["approved_by"]) == ("held", "")

    async def test_the_deck_switch_refuses(self, on, monkeypatch):
        rid = await _held(on)
        monkeypatch.setenv("FD_GMAIL_SEND", "off")
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.held_send(on.db, ALICE, rid, ALICE)
        assert exc.value.status == 403

    async def test_dismiss_and_expiry(self, on):
        rid = await _held(on)
        assert (await engine.dismiss(on.db, ALICE, rid))["state"] == "dismissed"
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.dismiss(on.db, ALICE, rid)
        assert exc.value.status == 409
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.dismiss(on.db, BOB, rid)            # not Bob's
        assert exc.value.status == 404
        old = await ledger_row(on.db, "m-old", state="held", thread="th-old",
                               reply_text=REPLY_TEXT, received_at=(
                                   _now() - timedelta(days=8)).isoformat())
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.held_send(on.db, ALICE, old, ALICE)
        assert exc.value.status == 409
        row = await on.db.get_autoreply(ALICE, old)
        assert (row["state"], row["reason"]) == ("dismissed", "expired")


# ── breadth test and "Try it" ───────────────────────────────────────


class TestDryRunAndTest:
    async def test_dry_run_classifies_eligible_mail_against_the_topic_alone(self, env):
        for i in range(5):
            env.gmail.deliver(mail(f"e{i}", age_s=86400 * (i + 1)))
        env.gmail.deliver(mail("list", headers=(("List-Id", "<x.example>"),)))
        env.fakes.single = lambda inb, topic: (
            judges.Match(topic_id=topic.id, source="tier", fit="exact", confidence=0.9)
            if inb.message_id in ("e0", "e1")
            else judges.Match(reason="no_match", source="tier", fit="none"))
        out = await engine.run_dry_run(env.db, ALICE, replace(TOPIC, enabled=False))
        assert (out["eligible"], out["matched"], out["rate"]) == (5, 2, 0.4)
        assert out["match_hash"] == TOPIC.match_hash
        assert {c[2] for c in env.fakes.single_calls} == {TOPIC.id}
        assert {r["verdict"] for r in out["rows"]} == {"match", "no_match",
                                                        "not eligible: auto_or_list"}
        assert await rows(env.db) == [] and env.gmail.sends() == []
        env.fakes.single = lambda inb, topic: judges.Match(reason="classifier_unavailable")
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.run_dry_run(env.db, ALICE, TOPIC)
        assert exc.value.status == 502

    async def test_try_it_never_sends_reads_gmail_or_writes_the_ledger(self, on):
        out = await engine.run_test(on.db, ALICE, {"from": SENDER, "subject": "Where?",
                                                   "body": "Where is the meetup?"})
        assert out["outcome"] == "would_send"
        assert out["writer"]["reply"] == REPLY_TEXT and out["judge"]["passed"]
        assert on.gmail.requests == [] and await rows(on.db) == []
        on.fakes.result = _reply(f"Pay with {CARD} please, see you at the meetup!")
        out = await engine.run_test(on.db, ALICE, {"from": SENDER, "subject": "Where?",
                                                   "body": "Where is the meetup?",
                                                   "topic_id": TOPIC.id})
        assert out["outcome"] == "blocked" and out["writer"]["reply"] == ""
        assert "4111" not in json.dumps(out)
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.run_test(on.db, ALICE, {"subject": "x", "body": ""})
        assert exc.value.status == 400
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.run_test(on.db, ALICE, {"body": "hi", "topic_id": "t_ffffffff"})
        assert exc.value.status == 404


# ── drift breaker and retention ─────────────────────────────────────


class TestDrift:
    async def test_a_topic_matching_most_mail_is_switched_off(self, on):
        for i in range(20):
            await ledger_row(on.db, f"c{i}", state="skipped", thread=f"th-{i}",
                             sender=f"s{i}@else.example", match_judge="tier",
                             topic_id=TOPIC.id if i < 11 else "")
        assert await engine.drift_check(on.db, ALICE) == [TOPIC.id]
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        assert not cfg.topic(TOPIC.id).enabled
        (bell,) = await bells(on.db, engine.BELL_ISSUE)
        assert bell["title"] == ('Topic "Meetup logistics" matches most of your mail — '
                                 "it isn't narrow.")

    async def test_too_few_samples_or_a_narrow_topic_stays_on(self, on):
        for i in range(19):
            await ledger_row(on.db, f"c{i}", state="skipped", thread=f"th-{i}",
                             sender=f"s{i}@else.example", match_judge="tier", topic_id=TOPIC.id)
        assert await engine.drift_check(on.db, ALICE) == []
        await ledger_row(on.db, "c19", state="skipped", thread="th-19", match_judge="tier")
        for i in range(20, 40):
            await ledger_row(on.db, f"c{i}", state="skipped", thread=f"th-{i}",
                             sender=f"s{i}@else.example", match_judge="tier")
        assert await engine.drift_check(on.db, ALICE) == []
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).topic(TOPIC.id).enabled

    async def test_purge_never_raises(self, on, monkeypatch):
        async def broken(*a, **kw):
            raise RuntimeError("db gone")

        monkeypatch.setattr(on.db, "purge_autoreplies", broken)
        assert await engine.purge(on.db) == {}


# ── the real writer, end to end ─────────────────────────────────────


class TestEndToEnd:
    async def test_real_writer_through_the_agents_model(self, on, monkeypatch):
        import re

        monkeypatch.setattr(writer, "resolve_writer", _REAL_RESOLVE_WRITER)
        monkeypatch.setattr(writer, "compose_reply", _REAL_COMPOSE_REPLY)
        tool_calls: list[tuple] = []

        async def run_tool(agent, tool, args, timeout=60.0, *, automation=None):
            tool_calls.append((agent, tool, args, automation))
            return {"ok": True,
                    "content": "#1 [howto] Answer meetup venue questions (0.9)  id=12345678"}

        monkeypatch.setattr(actions, "run_tool_on_agent", run_tool)
        agent_bodies: list[dict] = []

        def agent(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            agent_bodies.append(body)
            assert request.url.params["token"] == AGENT_TOKEN
            system = body["messages"][0]["content"]
            if system == writer.LOOKUP_SYSTEM_V1:
                content = '{"playbooks": ["meetup venue"]}'
            else:
                nonce = re.search(r"<<<REPLY ([0-9a-f]{16})>>>", system).group(1)
                content = f"<<<REPLY {nonce}>>>\n{REPLY_TEXT}\n<<<END {nonce}>>>"
            return httpx.Response(200, json={"ok": True, "content": content, "model": "m-1",
                                             "usage": {"total_tokens": 42},
                                             "finish_reason": "stop"})

        def router(request: httpx.Request) -> httpx.Response:
            if request.url.host == "gmail.googleapis.com":
                return on.gmail(request)
            assert (request.url.host, request.url.port, request.url.path) == (
                "localhost", 24001, "/api/llm/complete")
            return agent(request)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _RealAsyncClient(
            transport=httpx.MockTransport(router), **kw))
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert row["state"] == "sent", row
        assert row["agent_ref"] == REF and row["sources_used"] == ["playbooks"]
        assert len(agent_bodies) == 2 and all("temperature" not in b for b in agent_bodies)
        assert tool_calls and all(a == writer.LOOKUP_MARKER for *_r, a in tool_calls)
        assert {t for _a, t, _args, _m in tool_calls} == {"playbooks"}
        assert on.gmail.wire()["To"] == SENDER


# ── review regressions ──────────────────────────────────────────────


class TestFreshTokens:
    """Findings 5 / 8: a row's Gmail calls come minutes after the tick read
    its token (classifier, writer, judge) — each reads one again."""

    async def test_the_send_reads_a_fresh_token_after_the_model_work(self, on):
        async def expire():
            await rotate_token(on)

        on.fakes.on_judge = expire
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert row["state"] == "sent", row
        (send,) = on.gmail.sends()
        assert on.gmail.token(send) == "alice-access-2"

    async def test_the_thread_read_uses_a_token_read_after_the_classifier(self, on,
                                                                          monkeypatch):
        async def classify(owner, inb, topics, *, db=None, stage="match"):
            await rotate_token(on)
            return await on.fakes.classify(owner, inb, topics, db=db, stage=stage)

        monkeypatch.setattr(judges, "classify", classify)
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"
        reads = [r for r in on.gmail.requests if on.gmail.path(r).startswith("/threads/")]
        assert reads and {on.gmail.token(r) for r in reads} == {"alice-access-2"}

    async def test_no_token_at_send_time_leaves_the_row_retryable(self, on, monkeypatch):
        real = gr.get_valid_google_access_token

        async def gone(uid):
            return None

        async def lose():
            monkeypatch.setattr(gr, "get_valid_google_access_token", gone)

        on.fakes.on_judge = lose
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reply_text"]) == ("new", "")       # not failed
        assert on.gmail.sends() == []
        assert (await state(on.db))["last_error"] == "google_not_connected"
        on.fakes.on_judge = None
        monkeypatch.setattr(gr, "get_valid_google_access_token", real)
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"


class TestBoundMailbox:
    """Finding 24: the stored userinfo can be stale (a reconnect whose
    userinfo call failed) — the mailbox the token reads is checked."""

    async def test_a_token_for_another_mailbox_switches_off_before_any_poll(self, on):
        on.gmail.profile_email = "someone.else@gmail.com"
        on.gmail.deliver(mail("m1"))
        await tick(on)
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        assert (cfg.enabled, cfg.disabled_reason) == (False, "google_account_changed")
        assert await rows(on.db) == [] and on.gmail.sends() == []
        assert [on.gmail.path(r) for r in on.gmail.requests] == ["/profile"]
        assert [b["ref_id"] for b in await bells(on.db, engine.BELL_ISSUE)] == [
            "google_account_changed"]

    async def test_the_address_refresh_and_the_rebaseline_check_the_account(self, on):
        st = await state(on.db)
        on.gmail.profile_email = "someone.else@gmail.com"
        on.gmail.expired_below = 10 ** 9
        on.gmail.deliver(mail("m1"))
        stale = {**st, "own_addresses_at": (_now() - timedelta(hours=25)).isoformat()}
        async with httpx.AsyncClient() as client:
            with pytest.raises(engine._TickStopError) as exc:
                await engine._refresh_own_addresses(on.db, ALICE, client, "tok", stale)
            assert exc.value.reason == "google_account_changed"
            with pytest.raises(engine._TickStopError):
                await engine._poll(on.db, ALICE, client, "tok", st)
        after = await state(on.db)
        assert after["own_addresses"] == [OWN] and after["history_id"] == st["history_id"]
        assert await rows(on.db) == []

    async def test_a_failed_alias_lookup_keeps_the_known_aliases(self, on):
        # Finding 33.
        stale = (_now() - timedelta(hours=25)).isoformat()
        await on.db.update_autoreply_state(ALICE, own_addresses=[OWN, ALIAS],
                                           own_addresses_at=stale)
        on.gmail.forced[("GET", "/settings/sendAs")] = 503
        await tick(on)
        st = await state(on.db)
        assert st["own_addresses"] == [OWN, ALIAS] and st["own_addresses_at"] == stale
        del on.gmail.forced[("GET", "/settings/sendAs")]
        await tick(on)                                     # retried, not a day later
        st = await state(on.db)
        assert st["own_addresses"] == [OWN, ALIAS] and st["own_addresses_at"] != stale


class TestPollGaps:
    async def test_mail_arriving_during_a_rebaseline_is_claimed(self, on):
        # Finding 35: the cursor is read BEFORE the backfill list.
        on.gmail.deliver(mail("m1", age_s=120))
        on.gmail.expired_below = 10 ** 9
        on.gmail.after[("GET", "/messages")] = lambda: on.gmail.deliver(mail("race"))
        await tick(on)
        on.gmail.expired_below = 0
        await tick(on)
        assert {r["inbound_message_id"] for r in await rows(on.db)} == {"m1", "race"}
        assert (await row_for(on.db, "race"))["state"] == "sent"


class TestQueueLifecycle:
    async def test_a_turn_off_mid_tick_is_not_a_spent_budget(self, on, monkeypatch):
        # Finding 25.
        real = on.db.incr_classify_budget

        async def off_first(*a, **kw):
            await on.db.delete_autoreply_state(ALICE)    # PUT enabled:false, mid-tick
            return await real(*a, **kw)

        monkeypatch.setattr(on.db, "incr_classify_budget", off_first)
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", "turned_off")
        assert await bells(on.db) == [] and on.fakes.classify_calls == []

    async def test_an_old_waiting_row_rings_the_stopped_bell_only_for_a_stopped_agent(self, on):
        # Finding 36: the agent runs (the fixture's writer); the rows waited
        # on something else, or are from before this enablement.
        after = (_now() - timedelta(minutes=70)).isoformat()      # enabled 2 h ago
        before = (_now() - timedelta(hours=3)).isoformat()
        a = await ledger_row(on.db, "w1", state="waiting_agent", thread="th-w1",
                             topic_id=TOPIC.id, received_at=after)
        b = await ledger_row(on.db, "w2", state="waiting_agent", thread="th-w2",
                             topic_id=TOPIC.id, received_at=before)
        await tick(on)
        ra, rb = await on.db.get_autoreply(ALICE, a), await on.db.get_autoreply(ALICE, b)
        assert (ra["state"], ra["reason"]) == ("skipped", "stale")
        assert (rb["state"], rb["reason"]) == ("skipped", "before_enable")
        assert await bells(on.db) == [] and on.gmail.sends() == []

    async def test_switching_off_skips_the_queue(self, on):
        # Finding 37.
        n = await ledger_row(on.db, "q1", state="new", thread="th-q1")
        w = await ledger_row(on.db, "q2", state="waiting_agent", thread="th-q2")
        h = await ledger_row(on.db, "q3", state="held", thread="th-q3", reply_text=REPLY_TEXT)
        await engine.disable(ALICE, "paused_by_user", db=on.db)
        for rid in (n, w):
            row = await on.db.get_autoreply(ALICE, rid)
            assert (row["state"], row["reason"]) == ("skipped", "turned_off")
        assert (await on.db.get_autoreply(ALICE, h))["state"] == "held"

    async def test_a_fresh_baseline_skips_what_an_earlier_enablement_left(self, env):
        # The PUT enabled:false only drops the state row: the re-enable clears the rest.
        n = await ledger_row(env.db, "q1", state="new", thread="th-q1")
        w = await ledger_row(env.db, "q2", state="waiting_agent", thread="th-q2")
        async with engine.config_lock(ALICE):
            await engine.baseline_and_enable(
                env.db, ALICE, arcfg.AutoReplyConfig(agent_ref=REF, topics=(TOPIC,)))
        for rid in (n, w):
            assert (await env.db.get_autoreply(ALICE, rid))["reason"] == "turned_off"

    async def test_fetch_blips_dont_use_up_the_classifier_retries(self, on):
        # Finding 40.
        on.fakes.match = lambda inb, topics: judges.Match(reason="classifier_unavailable",
                                                          source="tier", detail="tier 502")
        on.gmail.deliver(mail("m1"))
        on.gmail.forced[("GET", "/messages/m1")] = 503
        await tick(on)
        await tick(on)                                   # two fetch blips
        del on.gmail.forced[("GET", "/messages/m1")]
        await tick(on)                                   # one classifier blip
        assert (await row_for(on.db, "m1"))["state"] == "new"
        for _ in range(3):
            await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("failed", "classifier_unavailable")
        assert len(on.fakes.classify_calls) == 3

    async def test_restart_requeues_dont_use_up_the_fetch_retries(self, on):
        await ledger_row(on.db, "m1", state="new", thread="th-m1", attempts=2)
        on.gmail.deliver(mail("m1"), history=False)
        on.gmail.forced[("GET", "/messages/m1")] = 503
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "new"     # one blip, not failed
        del on.gmail.forced[("GET", "/messages/m1")]
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"


class TestFlood:
    async def test_forged_from_claims_dont_count_toward_a_flood(self, on):
        # Finding 21: mail that never proved its From (checks 1–6) isn't that sender's.
        for i in range(11):
            await ledger_row(on.db, f"spoof{i}", state="skipped", thread=f"th-s{i}",
                             reason="unauthenticated" if i % 2 else "auto_or_list")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"

    async def test_flood_suppression_ends_a_day_after_the_flood(self, on):
        # Finding 22: measured from the flood, not renewed by each suppressed email.
        for i in range(10):
            await ledger_row(on.db, f"f{i}", state="skipped", thread=f"th-f{i}",
                             reason="no_match", age_h=0.5)
        on.gmail.deliver(mail("m1", thread="th-1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["reason"] == "cap_flood"
        await age_rows(on.db, 23)
        on.gmail.deliver(mail("m2", thread="th-2"))
        await tick(on)
        assert (await row_for(on.db, "m2"))["reason"] == "flood_suppressed"
        await age_rows(on.db, 2)          # the flood is 25 h old, m2's skip 2 h
        on.gmail.deliver(mail("m3", thread="th-3"))
        await tick(on)
        assert (await row_for(on.db, "m3"))["state"] == "sent"


class TestSendGate:
    async def test_pause_and_quick_re_enable_mid_flight_sends_nothing(self, on):
        # Finding 13: a re-enable is a fresh baseline; the in-flight reply isn't its.
        async def pause_and_resume():
            await engine.disable(ALICE, "paused_by_user", db=on.db)
            await _enable(on.db, on.gmail, enabled_at=_now())

        on.fakes.on_judge = pause_and_resume
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", "turned_off")
        assert on.gmail.sends() == [] and await on.db.list_gmail_sends(ALICE) == []

    async def test_a_never_disclose_term_added_mid_flight_blocks_the_send(self, on):
        # Finding 23.
        async def forbid():
            cfg = await arcfg.load_autoreply_config(on.db, ALICE)
            await arcfg.save_autoreply_config(on.db, ALICE,
                                              replace(cfg, never_disclose=("Ilica",)))

        on.fakes.on_judge = forbid
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert row["state"] == "blocked" and "never_disclose" in row["reason"]
        assert row["reply_text"] == "" and on.gmail.sends() == []
        assert [b["type"] for b in await bells(on.db)] == [engine.BELL_BLOCKED]

    async def test_a_brief_edit_mid_flight_holds_the_reply(self, on):
        async def edit():
            cfg = await arcfg.load_autoreply_config(on.db, ALICE)
            topic = replace(cfg.topic(TOPIC.id), brief=BRIEF.replace("18:00", "19:00"))
            await arcfg.save_autoreply_config(on.db, ALICE, replace(cfg, topics=(topic,)))

        on.fakes.on_judge = edit
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"], row["reply_text"]) == (
            "held", "brief_changed", REPLY_TEXT)
        assert on.gmail.sends() == []
        (bell,) = await bells(on.db, engine.BELL_HELD)
        assert bell["body"].endswith("the topic's brief changed while the reply was being written")


class TestSpineClaim:
    async def test_the_spine_is_claimed_as_soon_as_a_topic_matches(self, on):
        # Finding 14: the spine polled first; the arbiter must not find it
        # still "new" (and propose a reply) while the auto-reply is written.
        store = events.get_store()
        ev = store.add_event(ALICE, source="gmail", dedup_key="gmail:m1")
        seen: list[str] = []

        async def during():
            seen.append(store.get_event(ev["id"])["status"])

        on.fakes.on_judge = during
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert seen == ["acted"]
        assert (await row_for(on.db, "m1"))["state"] == "sent"

    async def test_unmatched_mail_stays_on_the_spine(self, on):
        on.fakes.match = lambda inb, topics: judges.Match(reason="no_match", source="tier",
                                                          fit="none")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert events.get_store().add_event(ALICE, source="gmail", dedup_key="gmail:m1")


class TestAutoPauseEvidence:
    async def test_blocks_a_sender_can_make_the_writer_synthesise_dont_pause(self, on):
        # Finding 20: a card number, an internal host — echo=False, but
        # nothing only the owner's material holds.
        bodies = {"m0": f"Here is a card: {CARD}. See you at the meetup!",
                  "m1": "Register at http://localhost:8080/signup — see you at the meetup!",
                  "m2": f"Pay with {CARD} at the door, see you at the meetup!"}
        on.fakes.result = lambda inb, topic: _reply(bodies[inb.message_id])(inb, topic)
        for i in range(3):
            on.gmail.deliver(mail(f"m{i}", thread=f"th-{i}", frm=f"P{i} <p{i}@example.com>"))
        await tick(on)
        got = await rows(on.db)
        assert [r["state"] for r in got] == ["blocked"] * 3
        assert not any(f["echo"] for r in got for f in r["findings"])
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).enabled


class TestHeldCardProvenance:
    async def test_held_send_counts_the_topic_folder_shareable_as_the_writer_did(
            self, on, monkeypatch):
        # Finding 27: a profile sentence that is also in the topic's folder.
        quote = "Our venue team answers every accessibility question within one working day"

        async def private(db, owner):
            return (f"About me: {quote}.",)

        index = writer.FolderIndex(folder="vfs:meetup/", files=(writer.FolderFile(
            path="faq.md", heading="FAQ", segments=(f"FAQ: {quote}.",)),))

        async def load_folder_index(owner, folder, *, never_disclose=()):
            return index if folder == "vfs:meetup/" else None

        monkeypatch.setattr(writer, "load_private_corpus", private)
        monkeypatch.setattr(writer, "load_folder_index", load_folder_index)
        topic = replace(TOPIC, sources=arcfg.TopicSources(folder="vfs:meetup/"))
        await _enable(on.db, on.gmail, topics=(topic,))
        on.fakes.result = lambda inb, t: writer.WriterResult(
            writer.REPLY, body=f"{quote}. See you at the meetup!", canary="CC-0123456789ab",
            shareable=(t.brief, f"FAQ: {quote}."),
            inbound=(inb.subject, inb.text, gi.sender_address(inb)))
        on.fakes.verdict = judges.JudgeResult(passed=False, failing=("off_topic",),
                                              scores={"off_topic": True}, source="tier")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("held", "judge:off_topic")   # L1 passed
        out = await engine.held_send(on.db, ALICE, row["id"], ALICE)
        assert out["ok"] and len(on.gmail.sends()) == 1


class TestDryRunFailsClosed:
    @pytest.mark.parametrize("path", ["/profile", "/settings/sendAs"])
    async def test_no_own_addresses_no_test(self, env, path):
        # Finding 2: without them every email reads "not addressed to me" —
        # 0 eligible, "too little mail", and the gate let a broad topic on.
        for i in range(12):
            env.gmail.deliver(mail(f"e{i}", age_s=3600 * (i + 1)))
        env.gmail.forced[("GET", path)] = 503
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.run_dry_run(env.db, ALICE, TOPIC)
        assert exc.value.status == 502
        assert env.fakes.single_calls == []

    async def test_an_unreadable_email_fails_the_test(self, env):
        for i in range(12):
            env.gmail.deliver(mail(f"e{i}", age_s=3600 * (i + 1)))
        env.gmail.forced[("GET", "/messages/e3")] = 503
        with pytest.raises(engine.AutoReplyError) as exc:
            await engine.run_dry_run(env.db, ALICE, TOPIC)
        assert exc.value.status == 502
        env.gmail.forced[("GET", "/messages/e3")] = 404     # deleted since the list: left out
        out = await engine.run_dry_run(env.db, ALICE, TOPIC)
        assert out["eligible"] == 11

    async def test_a_breadth_test_never_holds_up_live_mail(self, on, monkeypatch):
        # Finding 39: its classifications have their own slots.
        gate = asyncio.Event()
        started: list[str] = []

        async def slow(owner, inb, topic, *, db=None, stage="dry_run"):
            started.append(inb.message_id)
            await gate.wait()
            return judges.Match(reason="no_match", source="tier", fit="none")

        monkeypatch.setattr(judges, "classify_single", slow)
        for i in range(6):
            on.gmail.deliver(mail(f"e{i}", age_s=86400 * (i + 1)), history=False)
        test = asyncio.create_task(engine.run_dry_run(on.db, ALICE, TOPIC))
        for _ in range(200):
            if len(started) >= engine.TEST_LLM_CONCURRENCY:
                break
            await asyncio.sleep(0.01)
        assert len(started) >= engine.LLM_CONCURRENCY
        on.gmail.deliver(mail("m1"))
        try:
            await asyncio.wait_for(tick(on), timeout=5)
            assert (await row_for(on.db, "m1"))["state"] == "sent"
        finally:
            gate.set()
            out = await test
        assert out["eligible"] == 6

    async def test_try_it_without_a_from_writes_a_reply(self, on, monkeypatch):
        # Finding 31: the real writer refuses an email with no sender.
        async def compose(db, owner, cfg, topic, inb, thread_msgs, rec, shared, *,
                          own_addresses=(), now=None):
            if not gi.sender_address(inb):
                return await _REAL_COMPOSE_REPLY(db, owner, cfg, topic, inb, thread_msgs, rec,
                                                 shared, own_addresses=own_addresses, now=now)
            return await on.fakes.compose_reply(db, owner, cfg, topic, inb, thread_msgs, rec,
                                                shared, own_addresses=own_addresses, now=now)

        monkeypatch.setattr(writer, "compose_reply", compose)
        out = await engine.run_test(on.db, ALICE, {"subject": "Where?",
                                                   "body": "Where is the meetup?"})
        assert out["outcome"] == "would_send", out
        assert out["writer"]["reply"] == REPLY_TEXT
        assert on.gmail.requests == [] and await rows(on.db) == []


class TestDriftRetest:
    async def test_matches_from_before_the_topics_breadth_test_dont_count(self, on):
        # Finding 6: narrowed (same id), re-tested an hour ago, switched back on.
        for i in range(25):
            await ledger_row(on.db, f"c{i}", state="skipped", thread=f"th-{i}",
                             sender=f"s{i}@else.example", match_judge="tier",
                             topic_id=TOPIC.id if i < 15 else "", age_h=3)
        narrow = _topic(about=TOPIC.about + " Only the venue and the start time.")
        narrow = replace(narrow, dry_run=replace(
            narrow.dry_run, at=(_now() - timedelta(hours=1)).isoformat()))
        await _enable(on.db, on.gmail, topics=(narrow,))
        for i in range(4):
            await ledger_row(on.db, f"n{i}", state="skipped", thread=f"th-n{i}",
                             sender=f"n{i}@else.example", match_judge="tier")
        assert await engine.drift_check(on.db, ALICE) == []
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).topic(TOPIC.id).enabled
        assert await bells(on.db) == []

    async def test_a_drift_switch_off_needs_a_new_breadth_test(self, on):
        # Findings 15 / 18: the switch-off drops the breadth test, so neither
        # the On switch nor a form saved before it puts the broad topic back.
        for i in range(20):
            await ledger_row(on.db, f"c{i}", state="skipped", thread=f"th-{i}",
                             sender=f"s{i}@else.example", match_judge="tier",
                             topic_id=TOPIC.id if i < 11 else "")
        assert await engine.drift_check(on.db, ALICE) == [TOPIC.id]
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        topic = cfg.topic(TOPIC.id)
        assert not topic.enabled and topic.dry_run == arcfg.DryRun()
        assert not arcfg.dry_run_gate(topic).ok
        stale_view = [{**TOPIC.to_json(), "enabled": True}]     # dry_run is ignored on PUT
        with pytest.raises(arcfg.ConfigError) as exc:
            arcfg.apply_patch(cfg, {"topics": stale_view})
        assert exc.value.status == 409


# ── the loop ────────────────────────────────────────────────────────


class TestLoop:
    async def test_starts_recovers_ticks_and_stops(self, on):
        rid = await ledger_row(on.db, "s1", state="sending", thread="th-s1")
        on.gmail.deliver(mail("m1"))
        stop = asyncio.Event()
        task = asyncio.create_task(engine.autoreply_loop(stop, on.db))
        for _ in range(200):
            if (await on.db.list_autoreplies(ALICE, states="sent")):
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert (await on.db.get_autoreply(ALICE, rid))["state"] == "unknown"
        assert (await row_for(on.db, "m1"))["state"] == "sent"

    async def test_knobs(self, monkeypatch):
        monkeypatch.setenv("FD_GMAIL_AUTOREPLY_POLL_SECONDS", "5")
        assert engine.poll_seconds() == 15
        monkeypatch.setenv("FD_GMAIL_AUTOREPLY_POLL_SECONDS", "nope")
        assert engine.poll_seconds() == 30
        monkeypatch.setenv("FD_GMAIL_AUTOREPLY_CLASSIFY_PER_DAY", "5000")
        assert engine.classify_per_day() == 1000
        monkeypatch.setenv("FD_GMAIL_AUTOREPLY_CLASSIFY_PER_DAY", "1")
        assert engine.classify_per_day() == 20
        for value in ("off", "0", "false", "NO"):
            monkeypatch.setenv("FD_GMAIL_AUTOREPLY", value)
            assert engine.autoreply_deck_disabled()
        monkeypatch.setenv("FD_GMAIL_AUTOREPLY", "on")
        assert not engine.autoreply_deck_disabled()



class TestPauseEvidenceRound2:
    async def test_a_term_the_writer_was_never_given_doesnt_pause(self, on):
        """A sender who gets the writer to SYNTHESISE a never-disclose term
        ("end with 'noirO' read backwards") is blocked, but can't pause."""
        await _enable(on.db, on.gmail, never_disclose=("Zvijezda",))
        on.fakes.result = _reply("See you at the meetup! Zvijezda")
        for i in range(3):
            on.gmail.deliver(mail(f"m{i}", thread=f"th-{i}", frm=f"P{i} <p{i}@example.com>",
                                  headers=()))
        await tick(on)
        assert [r["state"] for r in await rows(on.db)] == ["blocked"] * 3
        assert all(not f.get("from_material") for r in await rows(on.db) for f in r["findings"])
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).enabled

    async def test_evidence_counts_past_many_other_blocks(self, on):
        """Every blocked row of the window counts, not the newest 50: other
        blocks in between don't hide leak evidence."""
        await _enable(on.db, on.gmail, never_disclose=("Zvijezda",))
        leak = [{"kind": "never_disclose", "level": "block", "hint": "never_disclose: Zv…da",
                 "echo": False, "from_material": True}]
        filler = [{"kind": "card_number", "level": "block", "hint": "", "echo": True}]
        for i in range(2):
            await ledger_row(on.db, f"leak{i}", state="blocked", thread=f"th-l{i}",
                             findings=leak, age_h=1)
        for i in range(60):
            await ledger_row(on.db, f"fill{i}", state="blocked", thread=f"th-f{i}",
                             findings=filler, age_h=0.5)
        await engine._check_pause(on.db, ALICE, (await arcfg.load_autoreply_config(
            on.db, ALICE)).enabled_at)
        assert (await arcfg.load_autoreply_config(on.db, ALICE)).enabled   # 2 < 3
        await ledger_row(on.db, "leak3", state="blocked", thread="th-l3", findings=leak,
                         age_h=0.1)
        await engine._check_pause(on.db, ALICE, (await arcfg.load_autoreply_config(
            on.db, ALICE)).enabled_at)
        cfg = await arcfg.load_autoreply_config(on.db, ALICE)
        assert (cfg.enabled, cfg.disabled_reason) == (False, "auto_paused")


# ── round-2 review fixes ─────────────────────────────────────────────


class TestSpineHandBack:
    """A matched email the engine did NOT handle goes back to Autonomous
    Work; one it handled stays claimed; an earlier unanswered email in the
    thread keeps the thread for the owner."""

    async def test_writer_skip_releases_the_claim(self, on):
        store = events.get_store()
        ev = store.add_event(ALICE, source="gmail", summary="Email", dedup_key="gmail:m1")
        on.fakes.result = lambda inb, topic: writer.WriterResult(
            writer.SKIP, reason=writer.WRITER_SKIP, detail="needs the owner")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "skipped"
        assert store.get_event(ev["id"])["status"] == "new"      # the arbiter sees it

    async def test_a_failure_after_the_match_releases_a_placeholder(self, on):
        on.fakes.result = lambda inb, topic: writer.WriterResult(
            writer.FAILED, reason=writer.WRITER_UNAVAILABLE)
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "failed"
        # No placeholder left: the poller's later add_event ingests it as new.
        assert events.get_store().add_event(ALICE, source="gmail", summary="Email",
                                            dedup_key="gmail:m1")

    async def test_a_sent_reply_keeps_the_claim(self, on):
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"
        assert events.get_store().add_event(ALICE, source="gmail", summary="Email",
                                            dedup_key="gmail:m1") is None

    async def test_an_earlier_unanswered_email_keeps_the_thread_for_the_owner(self, on):
        on.fakes.match = lambda inb, topics: (
            judges.Match(reason="no_match", source="tier", fit="none") if inb.message_id == "m1"
            else judges.Match(topic_id=topics[0].id, fit="exact", confidence=0.95, source="tier"))
        on.gmail.deliver(mail("m1", thread="th-x", body="Can we meet Tuesday?", age_s=300))
        on.gmail.deliver(mail("m2", thread="th-x", body="Also, where is the meetup?", age_s=60))
        await tick(on)
        assert (await row_for(on.db, "m2"))["reason"] == engine.THREAD_HAS_UNANSWERED
        assert on.gmail.n_sent == 0
        # Neither is claimed: the poller ingests the thread for Autonomous Work.
        assert events.get_store().add_event(ALICE, source="gmail", summary="Email",
                                            dedup_key="gmail:m2")


class TestArbiterRespectsClaims:
    def test_mark_never_moves_a_claimed_event(self, env):
        store = events.get_store()
        ev = store.add_event(ALICE, source="gmail", summary="Email", dedup_key="gmail:m1")
        assert store.claim_handled(ALICE, "gmail", "gmail:m1")
        store.mark([ev["id"]], "surfaced")
        assert store.get_event(ev["id"])["status"] == "acted"
        assert store.release_claim(ALICE, "gmail", "gmail:m1")
        assert store.get_event(ev["id"])["status"] == "new"


class TestFloodCountsArrivals:
    async def test_dismissed_old_cards_dont_make_a_flood(self, on):
        for i in range(11):     # claimed three days ago, dismissed just now
            rid = await ledger_row(on.db, f"old{i}", state="held", thread=f"th-o{i}", age_h=72)
            assert await on.db.cas_autoreply(rid, ALICE, "held", "dismissed")
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"


class TestQueuesOfOwnersWhoAreOff:
    async def test_recovery_closes_the_queue_of_an_owner_who_is_off(self, on):
        rid = await ledger_row(on.db, "m1", state="writing", thread="th-1")
        await engine.disable(ALICE, "paused_by_user", db=on.db)
        assert (await row_for(on.db, "m1"))["state"] == "writing"
        await engine.recover_on_start(on.db)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", engine.TURNED_OFF)
        assert row["id"] == rid

    async def test_a_poll_that_outlives_its_enablement_queues_nothing(self, on):
        on.gmail.deliver(mail("m1"))
        await on.db.delete_autoreply_state(ALICE)     # switched off mid-poll
        await engine._claim_all(on.db, ALICE, [("m1", "th-m1")])
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", engine.TURNED_OFF)


class TestRound2Pass4Engine:
    async def test_mail_trashed_while_the_reply_was_written_isnt_answered(self, on):
        async def trash():
            on.gmail.messages["m1"]["labelIds"] = ["TRASH"]

        on.fakes.on_judge = trash
        on.gmail.deliver(mail("m1"))
        await tick(on)
        row = await row_for(on.db, "m1")
        assert (row["state"], row["reason"]) == ("skipped", "labels")
        assert on.gmail.n_sent == 0

    async def test_replayed_mail_not_addressed_to_me_doesnt_make_a_flood(self, on):
        for i in range(11):
            on.gmail.deliver(mail(f"r{i}", thread=f"th-r{i}", to="someone@example.com"))
        for _ in range(4):          # a tick works at most PROCESS_BUDGET rows
            await tick(on)
        assert {r["reason"] for r in await rows(on.db)} == {"not_addressed_to_me"}
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "sent"

    async def test_one_runner_per_database(self, on):
        now = _now()
        stale = (now - timedelta(seconds=engine.RUNNER_STALE_S)).isoformat()
        assert await on.db.claim_autoreply_runner(1, "deck-a", now.isoformat(), stale)
        assert not await on.db.claim_autoreply_runner(2, "deck-b", now.isoformat(), stale)
        assert await on.db.claim_autoreply_runner(1, "deck-a", now.isoformat(), stale)
        later = now + timedelta(seconds=engine.RUNNER_STALE_S + 5)
        assert await on.db.claim_autoreply_runner(
            2, "deck-b", later.isoformat(),
            (later - timedelta(seconds=engine.RUNNER_STALE_S)).isoformat())


class TestWaitingKeepsTheClaim:
    async def test_a_waiting_row_keeps_its_claim_until_it_expires(self, on, monkeypatch):
        """Released while the engine still means to answer, the arbiter would
        propose a reply next to the auto-reply that follows."""
        store = events.get_store()
        ev = store.add_event(ALICE, source="gmail", summary="Email", dedup_key="gmail:m1")
        on.fakes.writer = writer.AGENT_STOPPED
        on.gmail.deliver(mail("m1"))
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "waiting_agent"
        assert store.get_event(ev["id"])["status"] == "acted"
        later = _now() + timedelta(minutes=61)
        monkeypatch.setattr(engine, "_now", lambda: later)
        await tick(on)
        assert (await row_for(on.db, "m1"))["state"] == "failed"
        assert store.get_event(ev["id"])["status"] == "new"      # back to Autonomous Work

    async def test_switching_off_gives_a_waiting_email_back(self, on):
        store = events.get_store()
        ev = store.add_event(ALICE, source="gmail", summary="Email", dedup_key="gmail:m1")
        on.fakes.writer = writer.AGENT_STOPPED
        on.gmail.deliver(mail("m1"))
        await tick(on)
        await engine.disable(ALICE, "paused_by_user", db=on.db)
        assert (await row_for(on.db, "m1"))["reason"] == engine.TURNED_OFF
        assert store.get_event(ev["id"])["status"] == "new"


class TestPauseEvidencePerValue:
    def test_a_brief_that_restates_the_profile_isnt_leak_evidence(self):
        about = ("I organise the Zagreb AI Meetup every month at the Hub in Ilica 1. I live in "
                 "Trnje with my two cats named Pixel and Byte since last spring.")
        reply = "She lives in Trnje with her two cats named Pixel and Byte since last spring."
        # The brief (SHAREABLE) restates the profile; the background doesn't.
        assert engine._from_material(reply, ("[insights] The meetup is free.",), (), [about]) \
            == frozenset()
        # The background holds the profile text the reply copies: evidence.
        assert engine._from_material(reply, (f"[insights] {about}",), (), [about]) \
            == frozenset({"private_copy"})

    def test_a_term_only_counts_when_the_background_held_it(self):
        assert engine._from_material("Zvijezda!", ("[insights] nothing here",), ("Zvijezda",),
                                     []) == frozenset()
        assert engine._from_material("Zvijezda!", ("[insights] Project Zvijezda",),
                                     ("Zvijezda",), []) == frozenset({"never_disclose"})


async def test_the_runner_claim_outlives_a_slow_poll(env, monkeypatch):
    """With a 300 s poll the heartbeat moves every 300 s: another deck must not
    see the claim as stale after 180 s and take over mid-tick."""
    monkeypatch.setattr(engine, "poll_seconds", lambda: 300)
    assert await engine._hold_runner(env.db)
    later = _now() + timedelta(seconds=200)
    stale = (later - timedelta(seconds=max(engine.RUNNER_STALE_S, 3 * 300 + 60))).isoformat()
    assert not await env.db.claim_autoreply_runner(99999, "other-deck", later.isoformat(), stale)
