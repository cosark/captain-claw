"""Topic auto-replies for Gmail — the owner's routes (/fd/google/gmail-autoreply).

The config and its status, the ten requirements for turning on (each a 409
listing what is missing, nothing written), exclusive Google-account binding,
the agent picker, the breadth test, "Try it", the activity list, held-card
Send / Edit & send / Dismiss and Pause — always as the signed-in user, never
another user's (an admin's X-FD-Act-As is not honoured).

Everything runs against a real FlightDeckDB and the real engine; Gmail answers
through httpx.MockTransport, the agent registry is faked at the server's
helpers, and the classifier, writer and judge are faked at their module
attributes. Nothing reaches a network, and nothing starts a Flight Deck.
"""

from __future__ import annotations

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
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.gmail_send_routes as gs
import captain_claw.flight_deck.google_oauth_routes as gr
import captain_claw.flight_deck.server as srv
from captain_claw import gmail_inbound as gi
from captain_claw import mail_signature, vfs
from captain_claw.flight_deck import agent_sharing, auth, events
from captain_claw.flight_deck import gmail_autoreply as engine
from captain_claw.flight_deck import gmail_autoreply_config as arcfg
from captain_claw.flight_deck import gmail_autoreply_judges as judges
from captain_claw.flight_deck import gmail_autoreply_routes as routes
from captain_claw.flight_deck import gmail_autoreply_writer as writer
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.google_oauth import GoogleOAuthTokens

ALICE = "user-alice"
BOB = "user-bob"
SUB = "google-sub-alice"
OWN = "owner.inbox@gmail.com"
SENDER = "alice@example.com"
REF = "process:meetup-bot:0123456789abcdef"
SLEEPY_REF = "process:sleepy:1111111111111111"
BOB_REF = "process:bobs-bot:2222222222222222"
MANAGED_REF = "process:vatra-0123abcd-worker:3333333333333333"
SIGNATURE = "— Ana's assistant (automatic reply)"
SCOPES = ("openid email https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/gmail.send")
ABOUT = "Emails asking when or where the Zagreb AI Meetup on 14 Nov is, or how to register."
BRIEF = ("What the agent may say: the Zagreb AI Meetup is on 14 November at 18:00 at the Hub, "
         "Ilica 1, Zagreb. Registration is free at the door; snacks & drinks are free too.")
REPLY_TEXT = ("Hi! The Zagreb AI Meetup is on 14 November at 18:00 at the Hub, Ilica 1. "
              "Registration is free at the door, and snacks & drinks are free too.")
SECRET = "sk-ant-" + "api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"   # fake; split so no scanner reads a key
_RealAsyncClient = httpx.AsyncClient

AR_PASS = ("mx.google.com; dkim=pass header.i=@example.com header.s=s1 header.b=Ab3dEf9h; "
           "spf=pass (google.com: domain of alice@example.com designates 192.0.2.25 as "
           "permitted sender) smtp.mailfrom=alice@example.com; "
           "dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com")
MX_HOP = ("from mail.example.com (mail.example.com. [192.0.2.25]) by mx.google.com with ESMTPS "
          "id a640c23a62f3a; Tue, 13 Oct 2026 02:14:07 -0700 (PDT)")

REC = agent_sharing.AgentRecord(
    runtime="process", slug="meetup-bot", instance="0123456789abcdef", owner=ALICE,
    name="Meetup bot", description="", port=24001, web_auth="t1", running=True)

REGISTRY = {
    "meetup-bot": {"owner": ALICE, "name": "Meetup bot", "web_auth": "t1", "web_port": 24001,
                   "instance_id": "0123456789abcdef"},
    "sleepy": {"owner": ALICE, "name": "Sleepy", "web_auth": "t2", "web_port": 24002,
               "instance_id": "1111111111111111"},
    "bobs-bot": {"owner": BOB, "name": "Bob's bot", "web_auth": "t4", "web_port": 24004,
                 "instance_id": "2222222222222222"},
    "vatra-0123abcd-worker": {"owner": ALICE, "name": "Vatra worker", "web_auth": "t5",
                              "web_port": 24005, "instance_id": "3333333333333333"},
}
ALIVE = {"meetup-bot": True, "bobs-bot": True, "vatra-0123abcd-worker": True}

TIER_BALANCED = judges.Resolved(source="tier", model="m-balanced", provider="anthropic",
                                tier="balanced")
TIER_REASON = judges.Resolved(source="tier", model="m-reason", provider="anthropic",
                              tier="reason")


def _now() -> datetime:
    return datetime.now(UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _topic(**kw) -> arcfg.Topic:
    """An enabled topic with a passing breadth test for its own wording."""
    t = arcfg.Topic(id="t_0a1b2c3d", name="Meetup logistics", about=ABOUT,
                    examples=("Where is the meetup?",), brief=BRIEF, enabled=True)
    t = replace(t, **kw)
    return replace(t, dry_run=arcfg.DryRun(match_hash=t.match_hash, at=_now().isoformat(),
                                           eligible=20, matched=1, rate=0.05))


TOPIC = _topic()
TOPIC_IN = {"name": "Meetup logistics", "about": ABOUT, "examples": ["Where is the meetup?"],
            "brief": BRIEF}


# ── the fake Gmail ──────────────────────────────────────────────────


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def mail(mid: str, *, frm: str = f"Alice <{SENDER}>", to: str = OWN,
         subject: str = "Where is the meetup?",
         body: str = "Hi, where is the meetup and when does it start?",
         labels=("INBOX", "UNREAD"), age_s: float = 60, thread: str = "",
         headers=()) -> dict:
    """A received, DMARC-verified Gmail message, ``format=full``."""
    hs = [("Received", MX_HOP), ("Authentication-Results", AR_PASS), ("From", frm),
          ("To", to), ("Subject", subject), ("Message-ID", f"<{mid}@example.com>"), *headers]
    return {
        "id": mid, "threadId": thread or f"th-{mid}", "labelIds": list(labels),
        "internalDate": str(_ms(_now()) - int(age_s * 1000)),
        "payload": {"mimeType": "text/plain",
                    "headers": [{"name": n, "value": v} for n, v in hs],
                    "body": {"size": len(body), "data": _b64(body)}},
    }


def sent_mail(mid: str, thread: str) -> dict:
    """A message the owner wrote themselves into *thread*."""
    hs = [("From", OWN), ("To", SENDER), ("Subject", "Re: Where is the meetup?")]
    return {"id": mid, "threadId": thread, "labelIds": ["SENT"],
            "internalDate": str(_ms(_now())),
            "payload": {"mimeType": "text/plain",
                        "headers": [{"name": n, "value": v} for n, v in hs],
                        "body": {"size": 5, "data": _b64("Hello")}}}


class FakeGmail:
    """Scripted Gmail API; records every request."""

    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.history_id = 7000
        self.profile_email = OWN
        self.send_status = 200
        self.requests: list[httpx.Request] = []
        self.n_sent = 0

    def deliver(self, msg: dict) -> str:
        self.messages[msg["id"]] = msg
        return msg["id"]

    @staticmethod
    def path(request: httpx.Request) -> str:
        return request.url.path.split("/gmail/v1/users/me", 1)[-1]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, q = self.path(request), request.url.params
        if request.method == "GET":
            if path == "/profile":
                return httpx.Response(200, json={"emailAddress": self.profile_email,
                                                 "historyId": str(self.history_id)})
            if path == "/settings/sendAs":
                return httpx.Response(200, json={"sendAs": [
                    {"sendAsEmail": OWN, "isPrimary": True}]})
            if path == "/messages":
                inbox = sorted((m for m in self.messages.values() if "INBOX" in m["labelIds"]),
                               key=lambda m: -int(m["internalDate"]))
                return httpx.Response(200, json={"messages": [
                    {"id": m["id"], "threadId": m["threadId"]}
                    for m in inbox[:int(q.get("maxResults", 100))]]})
            if path.startswith("/messages/"):
                mid = path.rsplit("/", 1)[1]
                if mid in self.messages:
                    return httpx.Response(200, json=self.messages[mid])
            if path.startswith("/threads/"):
                tid = path.rsplit("/", 1)[1]
                msgs = sorted((m for m in self.messages.values() if m["threadId"] == tid),
                              key=lambda m: int(m["internalDate"]))
                if msgs:
                    return httpx.Response(200, json={"id": tid, "messages": msgs})
        if request.method == "POST" and path == "/messages/send":
            return self._send(request)
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    def _send(self, request: httpx.Request) -> httpx.Response:
        if self.send_status != 200:
            return httpx.Response(self.send_status, json={"error": {"message": "boom"}})
        data = json.loads(request.content)
        self.n_sent += 1
        sid = f"sent-{self.n_sent}"
        tid = data.get("threadId") or f"th-{sid}"
        self.messages[sid] = {
            "id": sid, "threadId": tid, "labelIds": ["SENT"], "internalDate": str(_ms(_now())),
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": OWN}, {"name": "Auto-Submitted",
                                                 "value": "auto-replied"}],
                "body": {"size": 1, "data": _b64("x")}}}
        return httpx.Response(200, json={"id": sid, "threadId": tid})

    def sends(self) -> list[httpx.Request]:
        return [r for r in self.requests
                if r.method == "POST" and self.path(r) == "/messages/send"]

    def wire(self, i: int = -1) -> email.message.EmailMessage:
        raw = json.loads(self.sends()[i].content)["raw"]
        return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                                        policy=email.policy.default)

    def profile_reads(self) -> list[httpx.Request]:
        return [r for r in self.requests if self.path(r) == "/profile"]


# ── the fake checkers and writer ────────────────────────────────────


class Fakes:
    """judges.classify / classify_single / judge and writer.resolve_writer /
    compose_reply, recording every call."""

    def __init__(self):
        self.calls: list[str] = []
        self.matching: set[str] | None = None    # classify_single: ids that match (None: all)
        self.single_failed = False

    async def classify(self, owner, inb, topics, *, db=None, stage="match"):
        self.calls.append("classify")
        topics = list(topics)
        if not topics:
            return judges.Match(reason=judges.NO_MATCH, source="tier", fit="none")
        return judges.Match(topic_id=topics[0].id, source="tier", model="p/m",
                            confidence=0.95, fit="exact")

    async def classify_single(self, owner, inb, topic, *, db=None, stage="dry_run"):
        self.calls.append("classify_single")
        if self.single_failed:
            return judges.Match(reason=judges.CLASSIFIER_UNAVAILABLE)
        if self.matching is None or inb.message_id in self.matching:
            return judges.Match(topic_id=topic.id, source="tier", model="p/m",
                                confidence=0.95, fit="exact")
        return judges.Match(reason=judges.NO_MATCH, source="tier", model="p/m", fit="none")

    async def judge(self, owner, topic, shareable, background, inb, reply, *, thread=(),
                    db=None, stage="judge"):
        self.calls.append("judge")
        return judges.JudgeResult(passed=True, source="tier", model="p/m")

    async def resolve_writer(self, db, owner, ref):
        return REC, False

    async def compose_reply(self, db, owner, cfg, topic, inb, thread_msgs, rec, shared, *,
                            own_addresses=(), now=None):
        self.calls.append("compose")
        return writer.WriterResult(
            writer.REPLY, body=REPLY_TEXT, canary="CC-0123456789ab", shareable=(topic.brief,),
            inbound=(inb.subject, inb.text, gi.sender_address(inb)), sources_used=("playbooks",))


# ── fixtures ─────────────────────────────────────────────────────────


def _tokens(scope: str = SCOPES) -> GoogleOAuthTokens:
    return GoogleOAuthTokens(access_token="alice-access", refresh_token="alice-refresh",
                             token_type="Bearer", expires_at=time.time() + 3600, scope=scope)


@pytest.fixture()
async def env(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("FD_DATA_DIR", str(tmp_path / "fd-data"))
    monkeypatch.setenv("CLAW_VFS_ROOT", str(tmp_path / "vfs"))
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    for var in ("FD_LOCKDOWN", "FD_GMAIL_SEND", "FD_GMAIL_AUTOREPLY",
                "FD_GMAIL_REPEAT_CHECK_DAYS", "FD_GMAIL_AUTOREPLY_CLASSIFY_PER_DAY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(events, "_STORE", None)
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
    routes._dry_run_limit._requests.clear()
    routes._test_limit._requests.clear()
    # Alice: Google connected (read + send), a signature.
    await gr._store_tokens(d, ALICE, _tokens())
    await gr._store_user(d, ALICE, {"sub": SUB, "email": OWN})
    await d.set_settings(ALICE, {mail_signature.GOOGLE_KEY: SIGNATURE})

    gmail = FakeGmail()
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(gmail), **kw))
    # This deck's agents (the process registry; no containers).
    monkeypatch.setattr(srv, "_load_process_registry", lambda: REGISTRY)
    monkeypatch.setattr(srv, "_load_process_registry_strict", lambda: REGISTRY)
    monkeypatch.setattr(srv, "_process_is_alive", lambda slug: ALIVE.get(slug, False))
    monkeypatch.setattr(srv, "_deck_containers", lambda all=False, client=None: [])

    async def classifier(owner, *, db=None):
        return TIER_BALANCED

    async def judge_model(owner, *, db=None):
        return TIER_REASON

    monkeypatch.setattr(judges, "resolve_classifier", classifier)
    monkeypatch.setattr(judges, "resolve_judge", judge_model)
    fakes = Fakes()
    monkeypatch.setattr(judges, "classify", fakes.classify)
    monkeypatch.setattr(judges, "classify_single", fakes.classify_single)
    monkeypatch.setattr(judges, "judge", fakes.judge)
    monkeypatch.setattr(writer, "resolve_writer", fakes.resolve_writer)
    monkeypatch.setattr(writer, "compose_reply", fakes.compose_reply)
    try:
        yield SimpleNamespace(db=d, gmail=gmail, fakes=fakes, tmp=tmp_path)
    finally:
        gr._primary_owner_cache.update(id=None, at=0.0)
        gs._policy_locks.clear()
        gs._send_locks.clear()
        routes._dry_run_limit._requests.clear()
        routes._test_limit._requests.clear()
        auth.set_auth_db(prev_db)
        store = events._STORE
        if store is not None and store._conn is not None:
            store._conn.close()
        await d.close()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(gr.router)
    app.include_router(gs.router)
    app.include_router(routes.router)
    return TestClient(app, client=("127.0.0.1", 50123), follow_redirects=False)


def _bearer(uid: str = ALICE, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {auth.create_access_token(uid, role)}"}


URL = "/fd/google/gmail-autoreply"


async def _store(db, uid: str = ALICE, **kw) -> arcfg.AutoReplyConfig:
    """Store a config (off) directly — Alice's agent and an enabled topic that
    passed its breadth test, unless overridden."""
    cfg = arcfg.AutoReplyConfig(**{"agent_ref": REF, "topics": (TOPIC,), **kw})
    return await arcfg.save_autoreply_config(db, uid, cfg)


async def _turn_on(db, gmail: FakeGmail, uid: str = ALICE, *, sub: str = SUB,
                   **kw) -> arcfg.AutoReplyConfig:
    """Auto-replies on directly (config + state row), as the enabling PUT leaves them."""
    cfg = arcfg.AutoReplyConfig(**{"agent_ref": REF, "topics": (TOPIC,), **kw})
    cfg = arcfg.enable(cfg, google_sub=sub, account_email=OWN,
                       now=_now() - timedelta(hours=2))
    await arcfg.save_autoreply_config(db, uid, cfg)
    await db.set_autoreply_state(uid, history_id=str(gmail.history_id), google_sub=sub,
                                 account_email=OWN, enabled_at=cfg.enabled_at,
                                 own_addresses=[OWN], own_addresses_at=_now().isoformat())
    return cfg


async def _config(db, uid: str = ALICE) -> arcfg.AutoReplyConfig:
    return await arcfg.load_autoreply_config(db, uid)


def _folder(owner: str, folder: str):
    path = vfs.resolve_under(owner, "", folder)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── who may call ─────────────────────────────────────────────────────


ROUTES = [
    ("GET", URL), ("PUT", URL), ("GET", f"{URL}/agents"),
    ("POST", f"{URL}/topics/t_0a1b2c3d/dry-run"), ("POST", f"{URL}/test"),
    ("GET", f"{URL}/activity"), ("POST", f"{URL}/activity/ar_0123456789abcdef/send"),
    ("POST", f"{URL}/activity/ar_0123456789abcdef/dismiss"), ("POST", f"{URL}/pause"),
]


class TestWhoMayCall:
    @pytest.mark.parametrize("method,path", ROUTES)
    async def test_every_route_needs_a_signed_in_user(self, env, method, path):
        r = _client().request(method, path, json={} if method != "GET" else None)
        assert r.status_code == 401, r.text
        assert env.gmail.requests == []

    @pytest.mark.parametrize("method,path", ROUTES)
    async def test_an_auth_off_deck_has_no_autoreply_routes(self, env, monkeypatch, method,
                                                             path):
        monkeypatch.setenv("FD_AUTH_ENABLED", "false")
        r = _client().request(method, path, json={} if method != "GET" else None)
        assert r.status_code == 503, r.text

    async def test_act_as_is_ignored(self, env):
        c = _client()
        act_as = {**_bearer(ALICE, "admin"), auth.ACT_AS_HEADER: BOB}
        r = c.put(URL, headers=act_as, json={"daily_limit": 7})
        assert r.status_code == 200, r.text
        assert (await _config(env.db, ALICE)).daily_limit == 7
        assert (await _config(env.db, BOB)).daily_limit == arcfg.DEFAULT_DAILY_LIMIT
        assert c.get(URL, headers=act_as).json()["daily_limit"] == 7
        assert c.get(URL, headers=_bearer(BOB)).json()["daily_limit"] == 10

    async def test_users_never_see_each_other(self, env):
        await _store(env.db, ALICE, never_disclose=("Project Nightingale",))
        bob = _client().get(URL, headers=_bearer(BOB)).json()
        assert bob["topics"] == [] and bob["never_disclose"] == [] and bob["agent_ref"] == ""
        assert "Nightingale" not in json.dumps(bob)


# ── the view ─────────────────────────────────────────────────────────


class TestView:
    async def test_the_default_view_is_pinned(self, env):
        r = _client().get(URL, headers=_bearer(BOB))
        assert r.status_code == 200
        assert r.json() == {
            "enabled": False, "agent_ref": "", "daily_limit": 10, "never_disclose": [],
            "topics": [], "account_email": "", "enabled_at": "", "disabled_reason": "",
            "updated_at": "",
            "status": {
                "deck_disabled": False, "send_deck_disabled": False,
                "google_connected": False, "account_email": "", "bound_ok": True,
                "scopes_ok": False, "missing_scopes": ["gmail.readonly", "gmail.send"],
                "signature_set": False,
                "agent": {"ref": "", "name": "", "running": False, "shared": False,
                          "eligible": False, "reason": "no_agent"},
                "classifier": {"source": "tier", "model": "m-balanced", "provider": "anthropic",
                               "tier": "balanced", "fallback": None},
                "judge": {"source": "tier", "model": "m-reason", "provider": "anthropic",
                          "tier": "reason", "fallback": None},
                "sent_last_24h": 0, "held_open": 0, "last_poll_at": "", "last_error": "",
                "disabled_reason": "", "state": "off",
                "missing": ["google_not_connected", "missing_scopes", "no_signature",
                            "no_agent", "no_topics"],
            },
        }

    async def test_the_on_view_is_pinned(self, env):
        cfg = await _turn_on(env.db, env.gmail)
        r = _client().get(URL, headers=_bearer())
        view = r.json()
        assert set(view) == {"enabled", "agent_ref", "daily_limit", "never_disclose", "topics",
                             "account_email", "enabled_at", "disabled_reason", "updated_at",
                             "status"}
        assert "google_sub" not in json.dumps(view) and SUB not in json.dumps(view)
        assert (view["enabled"], view["account_email"], view["enabled_at"]) == (
            True, OWN, cfg.enabled_at)
        (topic,) = view["topics"]
        assert topic == {**TOPIC.to_json(),
                         "gate": {"ok": True, "reason": "", "note": "", "warning": ""},
                         "hidden_passages": [], "folder_error": ""}
        assert view["status"] == {
            "deck_disabled": False, "send_deck_disabled": False, "google_connected": True,
            "account_email": OWN, "bound_ok": True, "scopes_ok": True, "missing_scopes": [],
            "signature_set": True,
            "agent": {"ref": REF, "name": "Meetup bot", "running": True, "shared": False,
                      "eligible": True, "reason": ""},
            "classifier": TIER_BALANCED.to_json(), "judge": TIER_REASON.to_json(),
            "sent_last_24h": 0, "held_open": 0, "last_poll_at": "", "last_error": "",
            "disabled_reason": "", "state": "on", "missing": [],
        }

    async def test_counts_errors_and_attention(self, env):
        await _turn_on(env.db, env.gmail)
        for i, state in enumerate(("sent", "unknown", "held", "held", "skipped")):
            rid = await env.db.claim_autoreply(ALICE, f"m{i}", f"th-{i}")
            assert await env.db.cas_autoreply(rid, ALICE, "new", state)
        await env.db.update_autoreply_state(ALICE, last_error="gmail_401",
                                            last_poll_at="2026-10-14T10:00:00+00:00")
        status = _client().get(URL, headers=_bearer()).json()["status"]
        assert (status["sent_last_24h"], status["held_open"]) == (2, 2)
        assert (status["last_error"], status["last_poll_at"], status["state"]) == (
            "gmail_401", "2026-10-14T10:00:00+00:00", "attention")

    async def test_on_without_a_state_row_needs_attention(self, env):
        await _turn_on(env.db, env.gmail)
        await env.db.delete_autoreply_state(ALICE)
        status = _client().get(URL, headers=_bearer()).json()["status"]
        assert (status["state"], status["last_error"]) == ("attention", routes.STATE_MISSING)

    async def test_another_google_account_is_not_bound(self, env):
        await _turn_on(env.db, env.gmail)
        await gr._store_user(env.db, ALICE, {"sub": "another-sub", "email": OWN})
        status = _client().get(URL, headers=_bearer()).json()["status"]
        assert (status["bound_ok"], status["state"]) == (False, "attention")

    async def test_a_stopped_agent_needs_attention_but_stays_eligible(self, env):
        await _turn_on(env.db, env.gmail, agent_ref=SLEEPY_REF)
        status = _client().get(URL, headers=_bearer()).json()["status"]
        assert status["agent"] == {"ref": SLEEPY_REF, "name": "Sleepy", "running": False,
                                   "shared": False, "eligible": True, "reason": ""}
        assert status["state"] == "attention" and status["missing"] == []

    async def test_another_owners_agent_shows_nothing_of_it(self, env):
        await _store(env.db, agent_ref=BOB_REF)
        agent = _client().get(URL, headers=_bearer()).json()["status"]["agent"]
        assert agent == {"ref": BOB_REF, "name": "", "running": False, "shared": False,
                         "eligible": False, "reason": writer.AGENT_NOT_FOUND}

    async def test_hidden_passages_per_topic(self, env):
        root = _folder(ALICE, "vfs:meetup/faq")
        (root / "faq.md").write_text(
            "# Meetup FAQ\n\nThe meetup is at the Hub.\n\n"
            "Pay with card 4111 1111 1111 1111 at the door.\n", encoding="utf-8")
        topic = _topic(sources=arcfg.TopicSources(folder="vfs:meetup/faq"))
        gone = _topic(id="t_11111111", name="Venue questions", enabled=False,
                      sources=arcfg.TopicSources(folder="vfs:meetup/gone"))
        await _store(env.db, topics=(topic, gone))
        topics = _client().get(URL, headers=_bearer()).json()["topics"]
        assert topics[0]["hidden_passages"] == [{"file": "faq.md", "hidden": 1}]
        assert topics[0]["folder_error"] == ""
        # A folder deleted since it was chosen: the writer just has no excerpts.
        assert (topics[1]["hidden_passages"], topics[1]["folder_error"]) == (
            [], "not_a_directory")
        assert "4111" not in json.dumps(topics)


# ── PUT ──────────────────────────────────────────────────────────────


class TestPut:
    @pytest.mark.parametrize("body", [
        {"daily_limit": 0}, {"daily_limit": 51}, {"daily_limit": "10"}, {"enabled": "yes"},
        {"agent_ref": "not a ref"}, {"never_disclose": ["x"]}, {"topics": "nope"},
        {"topics": [{**TOPIC_IN, "name": "Anything else"}]},
        {"topics": [{**TOPIC_IN, "about": "Any email about anything at all please"}]},
        {"topics": [{**TOPIC_IN, "examples": []}]},
        {"topics": [{**TOPIC_IN, "sources": {"folder": "vfs:meetup/../../etc"}}]},
    ])
    async def test_bad_values_and_broad_topics_are_400(self, env, body):
        r = _client().put(URL, headers=_bearer(), json=body)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]
        assert await env.db.get_setting(ALICE, arcfg.AUTOREPLY_KEY) is None

    async def test_a_partial_update_persists_and_answers_the_view(self, env):
        c = _client()
        r = c.put(URL, headers=_bearer(), json={
            "agent_ref": REF, "daily_limit": 5, "never_disclose": ["Project Nightingale"],
            "topics": [TOPIC_IN], "google_sub": "forged", "enabled_at": "forged"})
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["notice"] == ""
        assert (view["agent_ref"], view["daily_limit"], view["never_disclose"]) == (
            REF, 5, ["Project Nightingale"])
        (topic,) = view["topics"]
        assert topic["enabled"] is False and topic["id"].startswith("t_")
        assert view["enabled_at"] == "" and (await _config(env.db)).google_sub == ""
        assert {k: v for k, v in view.items() if k != "notice"} == c.get(
            URL, headers=_bearer()).json()
        # Partial: the rest is left alone.
        r = c.put(URL, headers=_bearer(), json={"daily_limit": 7})
        assert r.json()["daily_limit"] == 7 and r.json()["topics"][0]["id"] == topic["id"]

    async def test_switching_a_topic_on_needs_its_breadth_test(self, env):
        r = _client().put(URL, headers=_bearer(), json={"topics": [{**TOPIC_IN,
                                                                    "enabled": True}]})
        assert r.status_code == 409
        assert "breadth test" in r.json()["detail"]

    async def test_editing_a_topic_that_is_on_switches_it_off_with_a_notice(self, env):
        await _store(env.db)
        edited = {**TOPIC.to_json(), "about": ABOUT + " Also parking near the venue."}
        r = _client().put(URL, headers=_bearer(), json={"topics": [edited]})
        assert r.status_code == 200, r.text
        assert r.json()["notice"] == arcfg.RERUN_NOTICE
        assert r.json()["topics"][0]["enabled"] is False
        # The brief isn't matching material: editing it keeps the topic on.
        await _store(env.db)
        r = _client().put(URL, headers=_bearer(), json={"topics": [
            {**TOPIC.to_json(), "brief": BRIEF + " Doors open at 17:30."}]})
        assert r.json()["notice"] == "" and r.json()["topics"][0]["enabled"] is True

    async def _drifted(self, env) -> dict:
        """Auto-replies on; the page is opened; overnight the drift breaker
        switches the topic off. Returns the view the page still shows."""
        await _turn_on(env.db, env.gmail)
        seen = _client().get(URL, headers=_bearer()).json()
        assert seen["topics"][0]["enabled"] is True
        for i in range(20):
            rid = await env.db.claim_autoreply(ALICE, f"c{i}", f"th-c{i}")
            await env.db.update_autoreply(rid, ALICE, match_judge="tier",
                                          topic_id=TOPIC.id if i < 11 else "")
            assert await env.db.cas_autoreply(rid, ALICE, "new", "skipped")
        assert await engine.drift_check(env.db, ALICE) == [TOPIC.id]
        assert (await _config(env.db)).enabled_topics == ()
        return seen

    async def test_a_page_left_open_cant_undo_the_drift_breaker(self, env):
        # Findings 15/18: the owner edits only the brief and saves the whole
        # list, the drifted topic still "on" in it.
        seen = await self._drifted(env)
        before = await env.db.get_setting(ALICE, arcfg.AUTOREPLY_KEY)
        stale = [{**t, "brief": t["brief"] + " Doors open at 17:30."} for t in seen["topics"]]
        r = _client().put(URL, headers=_bearer(), json={"topics": stale})
        assert r.status_code == 409 and "breadth test" in r.json()["detail"]
        assert await env.db.get_setting(ALICE, arcfg.AUTOREPLY_KEY) == before
        assert (await _config(env.db)).topic(TOPIC.id).enabled is False

    async def test_switching_a_drifted_topic_back_on_needs_a_new_breadth_test(self, env):
        await self._drifted(env)
        c = _client()
        topic = c.get(URL, headers=_bearer()).json()["topics"][0]
        assert topic["gate"]["ok"] is False and topic["dry_run"]["match_hash"] == ""
        # Not with its pre-drift test, nor one the page sends.
        for sent in ({}, {"dry_run": TOPIC.dry_run.to_json()}):
            r = c.put(URL, headers=_bearer(), json={"topics": [
                {**topic, **sent, "enabled": True}]})
            assert r.status_code == 409 and "breadth test" in r.json()["detail"]
        for i in range(10):
            env.gmail.deliver(mail(f"e{i}", age_s=3600 * (i + 1)))
        env.fakes.matching = {"e0"}
        r = c.post(f"{URL}/topics/{TOPIC.id}/dry-run", headers=_bearer())
        assert r.json()["gate"]["ok"] is True
        topic = c.get(URL, headers=_bearer()).json()["topics"][0]
        r = c.put(URL, headers=_bearer(), json={"topics": [{**topic, "enabled": True}]})
        assert r.status_code == 200, r.text
        assert r.json()["topics"][0]["enabled"] is True

    async def test_a_put_on_an_outdated_view_is_a_409(self, env):
        seen = await self._drifted(env)
        before = await env.db.get_setting(ALICE, arcfg.AUTOREPLY_KEY)
        c = _client()
        r = c.put(URL, headers=_bearer(), json={"daily_limit": 5,
                                                "updated_at": seen["updated_at"]})
        assert r.status_code == 409 and "changed since" in r.json()["detail"]
        assert await env.db.get_setting(ALICE, arcfg.AUTOREPLY_KEY) == before
        now = c.get(URL, headers=_bearer()).json()
        r = c.put(URL, headers=_bearer(), json={"daily_limit": 5,
                                                "updated_at": now["updated_at"]})
        assert r.status_code == 200, r.text
        assert r.json()["daily_limit"] == 5 and r.json()["updated_at"] != now["updated_at"]

    async def test_a_folder_must_be_a_folder_in_your_files(self, env):
        c = _client()
        topic = {**TOPIC_IN, "sources": {"folder": "vfs:meetup/faq"}}
        r = c.put(URL, headers=_bearer(), json={"topics": [topic]})
        assert r.status_code == 400 and "isn't a folder" in r.json()["detail"]
        (_folder(ALICE, "vfs:meetup") / "faq").write_text("a file, not a folder")
        assert c.put(URL, headers=_bearer(), json={"topics": [topic]}).status_code == 400
        _folder(ALICE, "vfs:meetup/docs")
        r = c.put(URL, headers=_bearer(), json={"topics": [
            {**TOPIC_IN, "sources": {"folder": "meetup/docs"}}]})
        assert r.status_code == 200, r.text
        assert r.json()["topics"][0]["sources"]["folder"] == "vfs:meetup/docs"
        # Bob's files are his: the same path is not a folder of Bob's.
        r = c.put(URL, headers=_bearer(BOB), json={"topics": [
            {**TOPIC_IN, "sources": {"folder": "vfs:meetup/docs"}}]})
        assert r.status_code == 400

    @pytest.mark.parametrize("ref,status", [
        (BOB_REF, 400), (MANAGED_REF, 400), ("process:ghost:4444444444444444", 400),
        (REF, 200), (SLEEPY_REF, 200),
    ])
    async def test_the_agent_must_be_one_of_your_eligible_agents(self, env, ref, status):
        r = _client().put(URL, headers=_bearer(), json={"agent_ref": ref})
        assert r.status_code == status, r.text
        assert (await _config(env.db)).agent_ref == (ref if status == 200 else "")
        if status == 400:
            assert "Bob" not in r.json()["detail"]

    async def test_unreadable_agent_records_refuse_the_agent(self, env, monkeypatch):
        def boom(ref, *, strict=False):
            raise agent_sharing.RecordUnavailable("registry unreadable")

        monkeypatch.setattr(agent_sharing, "resolve_agent_record", boom)
        r = _client().put(URL, headers=_bearer(), json={"agent_ref": REF})
        assert r.status_code == 503
        assert (await _config(env.db)).agent_ref == ""

    async def test_no_topic_may_say_a_never_disclose_term(self, env):
        c = _client()
        r = c.put(URL, headers=_bearer(), json={"never_disclose": ["ilica"],
                                                "topics": [TOPIC_IN]})
        assert r.status_code == 400
        assert "never-disclose" in r.json()["detail"] and "ilica" not in r.text.lower()
        assert await env.db.get_setting(ALICE, arcfg.AUTOREPLY_KEY) is None
        # A term added later that an existing brief says is refused too.
        assert c.put(URL, headers=_bearer(), json={"topics": [TOPIC_IN]}).status_code == 200
        r = c.put(URL, headers=_bearer(), json={"never_disclose": ["Ilića"]})
        assert r.status_code == 400 and "Ili" not in r.json()["detail"]
        assert (await _config(env.db)).never_disclose == ()
        assert c.put(URL, headers=_bearer(), json={"never_disclose": ["Nightingale"]}
                     ).status_code == 200

    async def test_the_agent_stays_while_auto_replies_are_on(self, env):
        await _turn_on(env.db, env.gmail)
        c = _client()
        assert c.put(URL, headers=_bearer(), json={"agent_ref": ""}).status_code == 400
        r = c.put(URL, headers=_bearer(), json={"agent_ref": "", "enabled": False})
        assert r.status_code == 200 and r.json()["agent_ref"] == ""

    async def test_off_drops_the_state_row(self, env):
        await _turn_on(env.db, env.gmail)
        r = _client().put(URL, headers=_bearer(), json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False and r.json()["status"]["state"] == "off"
        assert await env.db.get_autoreply_state(ALICE) is None
        cfg = await _config(env.db)
        assert (cfg.enabled, cfg.disabled_reason) == (False, "")

    async def test_the_key_is_server_owned(self, env):
        from captain_claw.flight_deck.settings_routes import _server_owned

        assert _server_owned(arcfg.AUTOREPLY_KEY)


# ── turning on ──────────────────────────────────────────────────────


async def _deck_off(env, mp):
    mp.setenv("FD_GMAIL_AUTOREPLY", "off")


async def _send_deck_off(env, mp):
    mp.setenv("FD_GMAIL_SEND", "off")


async def _no_google(env, mp):
    await gr._clear_oauth_state(env.db, ALICE)


async def _read_scope_missing(env, mp):
    await gr._store_tokens(env.db, ALICE, _tokens(
        "openid email https://www.googleapis.com/auth/gmail.send"))


async def _no_signature(env, mp):
    await env.db.set_settings(ALICE, {mail_signature.GOOGLE_KEY: ""})


async def _no_agent(env, mp):
    await _store(env.db, agent_ref="")


async def _bobs_agent(env, mp):
    await _store(env.db, agent_ref=BOB_REF)


async def _no_topic_on(env, mp):
    await _store(env.db, topics=(replace(TOPIC, enabled=False),))


async def _stale_breadth_test(env, mp):
    old = replace(TOPIC, dry_run=replace(TOPIC.dry_run,
                                         at=(_now() - timedelta(days=40)).isoformat()))
    await _store(env.db, topics=(old,))


async def _no_classifier(env, mp):
    async def nothing(owner, *, db=None):
        return judges.Resolved()

    mp.setattr(judges, "resolve_classifier", nothing)


async def _no_judge(env, mp):
    async def nothing(owner, *, db=None):
        return judges.Resolved()

    mp.setattr(judges, "resolve_judge", nothing)


async def _bound_to_bob(env, mp):
    await env.db.set_autoreply_state(BOB, google_sub=SUB, account_email=OWN,
                                     enabled_at=_now().isoformat())


async def _another_mailbox(env, mp):
    env.gmail.profile_email = "someone.else@gmail.com"


REQUIREMENT_CASES = [
    (_deck_off, routes.DECK_DISABLED),
    (_send_deck_off, routes.SEND_DECK_DISABLED),
    (_no_google, routes.GOOGLE_NOT_CONNECTED),
    (_read_scope_missing, routes.MISSING_SCOPES),
    (_no_signature, routes.NO_SIGNATURE),
    (_no_agent, routes.NO_AGENT),
    (_bobs_agent, routes.AGENT_INELIGIBLE),
    (_no_topic_on, routes.NO_TOPICS),
    (_stale_breadth_test, routes.TOPIC_GATE),
    (_no_classifier, routes.NO_CLASSIFIER),
    (_no_judge, routes.NO_JUDGE),
    (_bound_to_bob, routes.BOUND_ELSEWHERE),
    (_another_mailbox, routes.BASELINE),
]


class TestTurningOn:
    async def test_end_to_end_through_the_routes(self, env):
        c = _client()
        r = c.put(URL, headers=_bearer(), json={"agent_ref": REF, "topics": [TOPIC_IN]})
        tid = r.json()["topics"][0]["id"]
        for i in range(10):
            env.gmail.deliver(mail(f"e{i}", age_s=3600 * (i + 1)))
        env.fakes.matching = {"e0"}
        r = c.post(f"{URL}/topics/{tid}/dry-run", headers=_bearer())
        assert r.status_code == 200, r.text
        assert (r.json()["eligible"], r.json()["matched"], r.json()["rate"]) == (10, 1, 0.1)
        assert r.json()["gate"]["ok"] is True and r.json()["gate"]["warning"]
        topic = c.get(URL, headers=_bearer()).json()["topics"][0]
        r = c.put(URL, headers=_bearer(), json={"topics": [{**topic, "enabled": True}]})
        assert r.status_code == 200, r.text
        assert r.json()["status"]["missing"] == []
        assert await env.db.get_autoreply_state(ALICE) is None     # still off
        r = c.put(URL, headers=_bearer(), json={"enabled": True})
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["enabled"] is True and view["status"]["state"] == "on"
        assert view["account_email"] == OWN and view["enabled_at"]
        cfg = await _config(env.db)
        assert (cfg.google_sub, cfg.account_email) == (SUB, OWN)
        state = await env.db.get_autoreply_state(ALICE)
        assert (state["history_id"], state["google_sub"], state["own_addresses"]) == (
            str(env.gmail.history_id), SUB, [OWN])
        assert env.gmail.sends() == []
        # Again while on: nothing new — no second baseline.
        reads = len(env.gmail.profile_reads())
        assert c.put(URL, headers=_bearer(), json={"enabled": True}).status_code == 200
        assert len(env.gmail.profile_reads()) == reads

    @pytest.mark.parametrize("setup,code", REQUIREMENT_CASES,
                             ids=[code for _s, code in REQUIREMENT_CASES])
    async def test_each_requirement_is_a_409_with_nothing_written(self, env, monkeypatch,
                                                                  setup, code):
        await _store(env.db)
        await setup(env, monkeypatch)
        before = await env.db.get_setting(ALICE, arcfg.AUTOREPLY_KEY)
        r = _client().put(URL, headers=_bearer(), json={"enabled": True})
        assert r.status_code == 409, r.text
        body = r.json()
        assert code in body["missing"]
        assert isinstance(body["detail"], str) and body["detail"].startswith(
            "Topic auto-replies can't be turned on yet:")
        assert await env.db.get_setting(ALICE, arcfg.AUTOREPLY_KEY) == before
        assert not (await _config(env.db)).enabled
        assert await env.db.get_autoreply_state(ALICE) is None
        if code != routes.BASELINE:
            assert env.gmail.profile_reads() == []      # checked before any Gmail call
        else:
            assert body["missing"] == [routes.BASELINE]

    async def test_a_409_lists_everything_missing_in_order(self, env, monkeypatch):
        await _store(env.db, agent_ref="")
        monkeypatch.setenv("FD_GMAIL_SEND", "off")
        await env.db.set_settings(ALICE, {mail_signature.GOOGLE_KEY: ""})
        r = _client().put(URL, headers=_bearer(), json={"enabled": True})
        assert r.status_code == 409
        assert r.json()["missing"] == [routes.SEND_DECK_DISABLED, routes.NO_SIGNATURE,
                                       routes.NO_AGENT]
        assert list(routes.REQUIREMENTS).index(routes.BASELINE) == len(routes.REQUIREMENTS) - 1

    async def test_the_status_lists_the_same_requirements(self, env, monkeypatch):
        await _store(env.db)
        assert _client().get(URL, headers=_bearer()).json()["status"]["missing"] == []
        monkeypatch.setenv("FD_GMAIL_AUTOREPLY", "off")
        status = _client().get(URL, headers=_bearer()).json()["status"]
        assert status["deck_disabled"] is True and status["missing"] == [routes.DECK_DISABLED]

    async def test_one_google_account_is_bound_to_one_user(self, env):
        await _store(env.db)
        assert _client().put(URL, headers=_bearer(), json={"enabled": True}).status_code == 200
        # Bob connects the very same Google account and tries too.
        await gr._store_tokens(env.db, BOB, _tokens())
        await gr._store_user(env.db, BOB, {"sub": SUB, "email": OWN})
        await env.db.set_settings(BOB, {mail_signature.GOOGLE_KEY: SIGNATURE})
        await _store(env.db, BOB, agent_ref=BOB_REF)
        r = _client().put(URL, headers=_bearer(BOB), json={"enabled": True})
        assert r.status_code == 409 and r.json()["missing"] == [routes.BOUND_ELSEWHERE]
        assert await env.db.get_autoreply_state(BOB) is None
        # Once Alice turns hers off, Bob may.
        assert _client().post(f"{URL}/pause", headers=_bearer()).status_code == 200
        assert _client().put(URL, headers=_bearer(BOB), json={"enabled": True}
                             ).status_code == 200


# ── the agent picker ─────────────────────────────────────────────────


class TestAgents:
    async def test_lists_only_your_own_unmanaged_agents(self, env):
        c = _client()
        alice = c.get(f"{URL}/agents", headers=_bearer()).json()
        assert [a["name"] for a in alice] == ["Meetup bot", "Sleepy"]
        assert alice[0] == {"ref": REF, "name": "Meetup bot", "runtime": "process",
                            "running": True, "shared": False, "eligible": True, "reason": ""}
        assert alice[1]["running"] is False and alice[1]["eligible"] is True
        bob = c.get(f"{URL}/agents", headers=_bearer(BOB)).json()
        assert [a["ref"] for a in bob] == [BOB_REF]


# ── the breadth test ─────────────────────────────────────────────────


class TestDryRun:
    async def _topic_off(self, env) -> str:
        await _store(env.db, topics=(replace(TOPIC, enabled=False,
                                             dry_run=arcfg.DryRun()),))
        return TOPIC.id

    async def test_a_broad_topic_cant_be_switched_on(self, env):
        tid = await self._topic_off(env)
        for i in range(8):
            env.gmail.deliver(mail(f"e{i}", age_s=3600 * (i + 1)))
        env.gmail.deliver(mail("list", headers=(("List-Id", "<x.example>"),)))
        env.fakes.matching = {"e0", "e1", "e2"}
        r = _client().post(f"{URL}/topics/{tid}/dry-run", headers=_bearer())
        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["topic_id"], out["eligible"], out["matched"], out["rate"]) == (
            tid, 8, 3, 0.375)
        assert out["gate"]["ok"] is False and "38%" in out["gate"]["reason"]
        assert {row["verdict"] for row in out["rows"]} == {"match", "no_match",
                                                           "not eligible: auto_or_list"}
        stored = (await _config(env.db)).topic(tid).dry_run
        assert (stored.eligible, stored.matched, stored.match_hash) == (8, 3, TOPIC.match_hash)
        r = _client().put(URL, headers=_bearer(), json={"topics": [
            {**TOPIC.to_json(), "enabled": True}]})
        assert r.status_code == 409 and "narrower" in r.json()["detail"]
        assert env.gmail.sends() == [] and await env.db.list_autoreplies(ALICE) == []

    async def test_too_little_mail_passes_with_a_note(self, env):
        tid = await self._topic_off(env)
        for i in range(3):
            env.gmail.deliver(mail(f"e{i}"))
        r = _client().post(f"{URL}/topics/{tid}/dry-run", headers=_bearer())
        assert r.json()["gate"] == {"ok": True, "reason": "", "note": arcfg.TOO_LITTLE_MAIL,
                                    "warning": ""}
        r = _client().put(URL, headers=_bearer(), json={"topics": [
            {**TOPIC.to_json(), "enabled": True}]})
        assert r.status_code == 200 and r.json()["topics"][0]["enabled"] is True

    async def test_once_a_minute(self, env, monkeypatch):
        tid = await self._topic_off(env)
        clock = [1000.0]
        monkeypatch.setattr(routes, "_clock", lambda: clock[0])
        c = _client()
        assert c.post(f"{URL}/topics/{tid}/dry-run", headers=_bearer()).status_code == 200
        r = c.post(f"{URL}/topics/{tid}/dry-run", headers=_bearer())
        assert r.status_code == 429
        clock[0] += 61
        assert c.post(f"{URL}/topics/{tid}/dry-run", headers=_bearer()).status_code == 200

    async def test_a_failed_check_records_nothing(self, env):
        tid = await self._topic_off(env)
        env.gmail.deliver(mail("e0"))
        env.fakes.single_failed = True
        r = _client().post(f"{URL}/topics/{tid}/dry-run", headers=_bearer())
        assert r.status_code == 502
        assert (await _config(env.db)).topic(tid).dry_run == arcfg.DryRun()

    async def test_only_your_own_topics(self, env):
        tid = await self._topic_off(env)
        r = _client().post(f"{URL}/topics/{tid}/dry-run", headers=_bearer(BOB))
        assert r.status_code == 404
        assert env.gmail.requests == [] and env.fakes.calls == []

    async def test_a_re_test_that_matches_too_much_switches_the_topic_off(self, env):
        # Finding 7: on, auto-replies on, and the re-test now matches 10 of 10.
        await _turn_on(env.db, env.gmail)
        for i in range(10):
            env.gmail.deliver(mail(f"e{i}", age_s=3600 * (i + 1)))
        c = _client()
        r = c.post(f"{URL}/topics/{TOPIC.id}/dry-run", headers=_bearer())
        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["eligible"], out["matched"]) == (10, 10)
        assert out["gate"]["ok"] is False and "100%" in out["gate"]["reason"]
        assert out["switched_off"] is True and out["notice"] == arcfg.TOO_BROAD_NOTICE
        cfg = await _config(env.db)
        assert cfg.enabled_topics == ()                 # the engine answers nothing on it
        assert cfg.topic(TOPIC.id).dry_run.matched == 10    # the rate stays on show
        view = c.get(URL, headers=_bearer()).json()
        assert view["topics"][0]["enabled"] is False
        assert view["status"]["state"] == "attention" and "no_topics" in view["status"]["missing"]
        # Back on only with a test that passes.
        r = c.put(URL, headers=_bearer(), json={"topics": [
            {**view["topics"][0], "enabled": True}]})
        assert r.status_code == 409 and "narrower" in r.json()["detail"]

    async def test_a_passing_re_test_keeps_the_topic_on(self, env):
        await _turn_on(env.db, env.gmail)
        for i in range(20):
            env.gmail.deliver(mail(f"e{i}", age_s=3600 * (i + 1)))
        env.fakes.matching = {"e0"}
        r = _client().post(f"{URL}/topics/{TOPIC.id}/dry-run", headers=_bearer())
        assert r.status_code == 200, r.text
        assert (r.json()["switched_off"], r.json()["notice"]) == (False, "")
        assert (await _config(env.db)).topic(TOPIC.id).enabled

    async def test_a_test_of_an_earlier_wording_never_replaces_the_current_one(
            self, env, monkeypatch):
        tid = await self._topic_off(env)
        for i in range(10):
            env.gmail.deliver(mail(f"e{i}", age_s=3600 * (i + 1)))
        reworded = _topic(about=ABOUT + " Also parking near the venue.")
        real = engine.run_dry_run

        async def racing(db, owner, topic):
            out = await real(db, owner, topic)
            # Meanwhile the owner reworded it, tested the new wording and
            # switched it on; then the old test (10 of 10) comes back.
            await _store(db, topics=(reworded,))
            return out

        monkeypatch.setattr(engine, "run_dry_run", racing)
        r = _client().post(f"{URL}/topics/{tid}/dry-run", headers=_bearer())
        assert r.status_code == 200, r.text
        assert r.json()["notice"] == routes.STALE_TEST_NOTICE
        assert r.json()["switched_off"] is False and r.json()["gate"]["ok"] is True
        topic = (await _config(env.db)).topic(tid)
        assert topic.enabled and topic.dry_run == reworded.dry_run


# ── "Try it" ─────────────────────────────────────────────────────────


TRY = {"from": SENDER, "subject": "Where?", "body": "Where is the meetup?"}


class TestTryIt:
    async def test_never_sends_reads_gmail_or_writes_the_ledger(self, env):
        await _turn_on(env.db, env.gmail)
        r = _client().post(f"{URL}/test", headers=_bearer(), json=TRY)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["outcome"] == "would_send" and out["writer"]["reply"] == REPLY_TEXT
        assert out["topic"] == {"id": TOPIC.id, "name": TOPIC.name}
        assert env.fakes.calls == ["classify", "compose", "judge"]
        assert env.gmail.requests == []
        assert await env.db.list_autoreplies(ALICE, include_quiet=True) == []
        assert await env.db.list_gmail_sends(ALICE) == []

    @pytest.mark.parametrize("body,status", [
        ({"subject": "x", "body": ""}, 400), ({"body": "hi", "topic_id": "t_ffffffff"}, 404),
        ({"body": "hi", "from": "two@x.example, three@x.example"}, 400),
    ])
    async def test_bad_input(self, env, body, status):
        assert _client().post(f"{URL}/test", headers=_bearer(), json=body
                              ).status_code == status

    async def test_not_json_is_400(self, env):
        r = _client().post(f"{URL}/test", headers={**_bearer(), "Content-Type": "text/plain"},
                           content=b"hello")
        assert r.status_code == 400

    async def test_twenty_an_hour_per_user(self, env, monkeypatch):
        await _store(env.db)
        clock = [5000.0]
        monkeypatch.setattr(routes, "_clock", lambda: clock[0])
        c = _client()
        for _ in range(routes.TEST_PER_HOUR):
            assert c.post(f"{URL}/test", headers=_bearer(), json=TRY).status_code == 200
        r = c.post(f"{URL}/test", headers=_bearer(), json=TRY)
        assert r.status_code == 429
        assert c.post(f"{URL}/test", headers=_bearer(BOB), json=TRY).status_code == 200
        clock[0] += 3601
        assert c.post(f"{URL}/test", headers=_bearer(), json=TRY).status_code == 200


# ── the activity list ───────────────────────────────────────────────


async def _row(db, mid: str, state: str, *, owner: str = ALICE, reason: str = "",
               text: str = "", received: datetime | None = None, **fields) -> str:
    rid = await db.claim_autoreply(owner, mid, f"th-{mid}")
    await db.update_autoreply(
        rid, owner, from_addr=SENDER, from_domain="example.com",
        subject="Where is the meetup?", thread_id=f"th-{mid}", topic_id=TOPIC.id,
        topic_name=TOPIC.name, agent_name="Meetup bot", reason=reason, reply_text=text,
        received_at=(received or _now()).isoformat(), **fields)
    if state != "new":
        assert await db.cas_autoreply(rid, owner, "new", state)
    return rid


class TestActivity:
    async def test_text_only_where_cleared_and_quiet_skips_on_request(self, env):
        await _row(env.db, "a", "sent", text=REPLY_TEXT, sent_message_id="sent-9")
        await _row(env.db, "b", "held", reason="judge:off_topic", text="Held text",
                   findings=[{"kind": "url", "level": "hold", "hint": "url: ht…om",
                              "echo": False}],
                   judge={"source": "tier", "passed": False, "failing": ["off_topic"]})
        await _row(env.db, "c", "unknown", reason="send_unconfirmed", text="Maybe sent")
        await _row(env.db, "d", "failed", reason="writer_format", text="never cleared")
        await _row(env.db, "e", "skipped", reason="no_match")
        await _row(env.db, "f", "skipped", reason="writer_skip", text="never cleared")
        await _row(env.db, "g", "blocked", reason="guard:card_number",
                   findings=[{"kind": "card_number", "level": "block", "hint": "card_number: 41…11",
                              "echo": False}])
        c = _client()
        rows = c.get(f"{URL}/activity", headers=_bearer()).json()
        by_mid = {r["inbound_message_id"]: r for r in rows}
        assert set(by_mid) == {"a", "b", "c", "d", "f", "g"}            # no_match is quiet
        assert {m: r["reply_text"] for m, r in by_mid.items()} == {
            "a": REPLY_TEXT, "b": "Held text", "c": "Maybe sent", "d": "", "f": "", "g": ""}
        held = by_mid["b"]
        assert set(held) == {"id", "state", "reason", "detail", "from", "subject", "topic_id",
                             "topic_name", "confidence", "match_judge", "sources_used",
                             "findings", "judge", "agent_name", "thread_id",
                             "inbound_message_id", "sent_message_id", "reply_text",
                             "received_at", "created_at", "updated_at"}
        assert (held["from"], held["state"], held["reason"]) == (
            SENDER, "held", "judge:off_topic")
        assert held["findings"][0]["kind"] == "url"
        assert held["judge"]["failing"] == ["off_topic"]
        assert by_mid["a"]["sent_message_id"] == "sent-9"
        everything = c.get(f"{URL}/activity?all=true", headers=_bearer()).json()
        assert {r["inbound_message_id"] for r in everything} == set(by_mid) | {"e"}
        assert len(c.get(f"{URL}/activity?limit=2", headers=_bearer()).json()) == 2

    async def test_only_your_own_rows(self, env):
        await _row(env.db, "a", "held", text="Alice's held reply")
        assert _client().get(f"{URL}/activity?all=true", headers=_bearer(BOB)).json() == []

    async def test_an_open_held_card_is_always_listed(self, env):
        # Finding 38: one held card, then 50 newer rows that aren't quiet.
        rid = await _row(env.db, "h", "held", reason="judge:off_topic", text="Held text")
        for i in range(50):
            await _row(env.db, f"s{i}", "skipped", reason="not_addressed_to_me")
        c = _client()
        assert c.get(URL, headers=_bearer()).json()["status"]["held_open"] == 1
        rows = c.get(f"{URL}/activity?limit=50", headers=_bearer()).json()
        assert len(rows) == 50
        assert [r["id"] for r in rows if r["state"] == "held"] == [rid]
        assert rows[-1]["id"] == rid and rows[-1]["reply_text"] == "Held text"
        stamps = [r["created_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)                    # newest first
        assert rows[0]["inbound_message_id"] == "s49"
        # Acted on, it's ordinary history again.
        assert (await env.db.cas_autoreply(rid, ALICE, "held", "dismissed"))
        rows = c.get(f"{URL}/activity?limit=50", headers=_bearer()).json()
        assert rid not in {r["id"] for r in rows}

    async def test_held_cards_beyond_the_limit_are_all_listed(self, env):
        held = [await _row(env.db, f"h{i}", "held", text="Held text") for i in range(3)]
        await _row(env.db, "s", "sent", text=REPLY_TEXT)
        rows = _client().get(f"{URL}/activity?limit=2", headers=_bearer()).json()
        assert [r["id"] for r in rows] == held[::-1]


# ── held cards ───────────────────────────────────────────────────────


async def _held(env, text: str = REPLY_TEXT + " Write to carol@else.example.",
                **kw) -> str:
    await _turn_on(env.db, env.gmail)
    env.gmail.deliver(mail("m1", thread="th-m1", age_s=600))
    return await _row(env.db, "m1", "held", reason="guard:email_address", text=text, **kw)


class TestHeldCards:
    async def test_send_is_owner_approved_uncapped_and_once(self, env):
        rid = await _held(env)
        for _ in range(arcfg.DEFAULT_DAILY_LIMIT):
            await env.db.add_gmail_send(ALICE, origin="autoreply", to_addrs="x@else.example")
        c = _client()
        r = c.post(f"{URL}/activity/{rid}/send", headers=_bearer())
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] is True and out["to"] == SENDER and out["message_id"] == "sent-1"
        assert (out["row"]["state"], out["row"]["id"]) == ("sent", rid)
        row = await env.db.get_autoreply(ALICE, rid)
        assert (row["state"], row["approved_by"], row["sent_message_id"]) == (
            "sent", ALICE, "sent-1")
        (audit,) = await env.db.list_gmail_sends(ALICE, origin="autoreply_approved")
        assert audit["in_reply_to_message_id"] == "m1" and audit["topic_id"] == TOPIC.id
        wire = env.gmail.wire()
        assert wire["To"] == SENDER and wire["Auto-Submitted"] == "auto-replied"
        assert wire["Cc"] is None and wire["Bcc"] is None
        plain = wire.get_body(("plain",)).get_content()
        assert plain.startswith(REPLY_TEXT) and plain.count(SIGNATURE) == 1
        # A second call: the card isn't held any more.
        r = c.post(f"{URL}/activity/{rid}/send", headers=_bearer())
        assert r.status_code == 409
        assert len(env.gmail.sends()) == 1
        assert c.post(f"{URL}/activity/{rid}/dismiss", headers=_bearer()).status_code == 409

    async def test_edit_and_send_uses_the_edited_text(self, env):
        rid = await _held(env)
        r = _client().post(f"{URL}/activity/{rid}/send", headers=_bearer(),
                           json={"text": "See you at the Hub on 14 November!"})
        assert r.status_code == 200, r.text
        plain = env.gmail.wire().get_body(("plain",)).get_content()
        assert plain.startswith("See you at the Hub on 14 November!")
        assert "carol@else.example" not in plain

    @pytest.mark.parametrize("text", ["", "   ", 42])
    async def test_an_unusable_edit_is_400(self, env, text):
        rid = await _held(env)
        r = _client().post(f"{URL}/activity/{rid}/send", headers=_bearer(), json={"text": text})
        assert r.status_code == 400
        assert (await env.db.get_autoreply(ALICE, rid))["state"] == "held"

    async def test_an_edit_with_a_secret_is_422_with_masked_findings(self, env):
        rid = await _held(env)
        r = _client().post(f"{URL}/activity/{rid}/send", headers=_bearer(),
                           json={"text": f"Use key {SECRET} to get in."})
        assert r.status_code == 422
        body = r.json()
        assert isinstance(body["detail"], str) and body["detail"].startswith("Not sent")
        assert "secret_key" in [f["kind"] for f in body["findings"]]
        assert all(set(f) == {"kind", "level", "hint", "echo"} for f in body["findings"])
        assert all(f["hint"] == "" for f in body["findings"] if f["kind"] == "secret_key")
        assert SECRET not in r.text and "sk-ant" not in r.text
        row = await env.db.get_autoreply(ALICE, rid)
        assert (row["state"], row["approved_by"]) == ("held", "")
        assert env.gmail.sends() == []

    async def test_owner_already_replied_is_409(self, env):
        rid = await _held(env)
        env.gmail.deliver(sent_mail("mine", "th-m1"))
        r = _client().post(f"{URL}/activity/{rid}/send", headers=_bearer())
        assert r.status_code == 409 and "already replied" in r.json()["detail"]
        assert (await env.db.get_autoreply(ALICE, rid))["state"] == "held"
        assert env.gmail.sends() == []

    async def test_the_send_deck_switch_refuses(self, env, monkeypatch):
        rid = await _held(env)
        monkeypatch.setenv("FD_GMAIL_SEND", "off")
        r = _client().post(f"{URL}/activity/{rid}/send", headers=_bearer())
        assert r.status_code == 403
        assert (await env.db.get_autoreply(ALICE, rid))["state"] == "held"

    async def test_an_unknown_outcome_says_so(self, env):
        rid = await _held(env)
        env.gmail.send_status = 503
        r = _client().post(f"{URL}/activity/{rid}/send", headers=_bearer())
        assert r.status_code == 502
        assert r.headers.get("X-FD-Gmail-Send-Outcome") == "unknown"
        assert (await env.db.get_autoreply(ALICE, rid))["state"] == "unknown"

    async def test_another_users_card_is_not_found(self, env):
        rid = await _held(env)
        c = _client()
        for action in ("send", "dismiss"):
            r = c.post(f"{URL}/activity/{rid}/{action}", headers=_bearer(BOB))
            assert r.status_code == 404, action
        act_as = {**_bearer(ALICE, "admin"), auth.ACT_AS_HEADER: BOB}
        assert c.post(f"{URL}/activity/{rid}/dismiss", headers=act_as).status_code == 200
        assert (await env.db.get_autoreply(ALICE, rid))["state"] == "dismissed"
        assert env.gmail.sends() == []

    async def test_dismiss_and_expiry(self, env):
        rid = await _held(env)
        c = _client()
        r = c.post(f"{URL}/activity/{rid}/dismiss", headers=_bearer())
        assert r.status_code == 200 and r.json()["row"]["state"] == "dismissed"
        assert r.json()["row"]["reply_text"] == ""
        assert c.post(f"{URL}/activity/{rid}/dismiss", headers=_bearer()).status_code == 409
        assert c.post(f"{URL}/activity/{rid}/send", headers=_bearer()).status_code == 409
        old = await _row(env.db, "m-old", "held", text=REPLY_TEXT,
                         received=_now() - timedelta(days=8))
        r = c.post(f"{URL}/activity/{old}/send", headers=_bearer())
        assert r.status_code == 409 and "expired" in r.json()["detail"]
        row = await env.db.get_autoreply(ALICE, old)
        assert (row["state"], row["reason"]) == ("dismissed", "expired")
        assert env.gmail.sends() == []


# ── pause ────────────────────────────────────────────────────────────


class TestPause:
    async def test_pause_switches_off_and_drops_the_state_row(self, env):
        await _turn_on(env.db, env.gmail)
        r = _client().post(f"{URL}/pause", headers=_bearer())
        assert r.status_code == 200, r.text
        view = r.json()
        assert (view["enabled"], view["disabled_reason"]) == (False, routes.PAUSED_BY_USER)
        assert view["status"]["state"] == "off"
        assert view["status"]["disabled_reason"] == routes.PAUSED_BY_USER
        assert await env.db.get_autoreply_state(ALICE) is None
        # Turning on again is a deliberate act with a fresh baseline.
        r = _client().put(URL, headers=_bearer(), json={"enabled": True})
        assert r.status_code == 200 and r.json()["disabled_reason"] == ""
        assert (await env.db.get_autoreply_state(ALICE))["history_id"] == str(
            env.gmail.history_id)

    async def test_pausing_one_user_leaves_the_other(self, env):
        await _turn_on(env.db, env.gmail)
        assert _client().post(f"{URL}/pause", headers=_bearer(BOB)).status_code == 200
        assert (await _config(env.db)).enabled is True
        assert await env.db.get_autoreply_state(ALICE) is not None
