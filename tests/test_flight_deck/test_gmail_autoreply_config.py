"""Topic auto-replies for Gmail — the per-user config (gmail_autoreply_config).

The setting is server-owned (``google_oauth:gmail_autoreply``: /fd/settings
can't read or write it) and fails closed: nothing stored, or a record that is
unreadable or out of range anywhere, reads as the defaults — off. A PUT's
partial update is validated and linted (broad topics are refused, EN/HR, case
and diacritics folded); topic ids are the server's; a topic switches on only
through the breadth gate (a dry run of its current wording — the match hash —
at most 30 days old, matching at most 25 %), and editing what a topic that is on
matches turns it off with a notice.
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.gmail_autoreply_config as ac
from captain_claw.flight_deck import agent_secret, auth, settings_routes
from captain_claw.flight_deck.db import FlightDeckDB

ALICE = "user-alice"
BOB = "user-bob"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
REF = "process:meetup-bot:0123456789abcdef"


@pytest.fixture()
async def db(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    agent_secret.reset_cache_for_tests()
    d = FlightDeckDB(str(tmp_path / "fd.db"))
    await d.init()
    prev_db = auth._db
    auth.set_auth_db(d)
    now = "2026-01-01T00:00:00Z"
    for uid, email_ in [(ALICE, "alice@x.co"), (BOB, "bob@x.co")]:
        await d._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, email_, "h", uid.title(), "user", now, now),
        )
    await d._db.commit()
    try:
        yield d
    finally:
        agent_secret.reset_cache_for_tests()
        auth.set_auth_db(prev_db)
        await d.close()


def _topic(**over):
    t = {
        "name": "Meetup logistics",
        "about": ("Emails asking when or where the Zagreb AI Meetup on 14 Nov is, or how to"
                  " register."),
        "not_this": "Sponsorship, speaking slots, invoices.",
        "examples": ["Where is the meetup going to be held?"],
        "counter_examples": ["Can I give a talk at the meetup?"],
        "senders": [],
        "brief": "The meetup is on 14 Nov at 18:00 in the Zagreb hub; registration is free.",
        "sources": {"folder": "", "insights": False, "playbooks": True, "web_search": False},
    }
    t.update(over)
    return t


def _patch(cfg, patch, now=NOW):
    return ac.apply_patch(cfg, patch, now=now)


def _with_topics(*topics, cfg=None):
    return _patch(cfg or ac.default_config(), {"topics": list(topics)}).config


def _tested(cfg, topic_id, *, eligible=20, matched=1, at=NOW):
    """*cfg* with a breadth test of the topic's current wording."""
    return ac.record_dry_run(cfg, topic_id, match_hash=cfg.topic(topic_id).match_hash,
                             eligible=eligible, matched=matched, now=at)


def _view_topic(t, **enabled):
    """A topic as the browser sends it back (id kept, server fields included)."""
    return {**t.to_json(), **enabled}


def _on(cfg, topic_id):
    """*cfg* with the topic switched on through the breadth gate."""
    cfg = _tested(cfg, topic_id)
    topics = [_view_topic(t, enabled=(t.enabled or t.id == topic_id)) for t in cfg.topics]
    res = _patch(cfg, {"topics": topics})
    assert res.config.topic(topic_id).enabled
    return res.config


def _one_on():
    cfg = _with_topics(_topic())
    return _on(cfg, cfg.topics[0].id)


# ── defaults and the fail-closed loader ──────────────────────────────


DEFAULT_RECORD = {
    "enabled": False, "agent_ref": "", "daily_limit": 10, "never_disclose": [], "topics": [],
    "google_sub": "", "account_email": "", "enabled_at": "", "disabled_reason": "",
    "updated_at": "",
}


class TestDefaultsAndLoad:
    def test_defaults_are_off(self):
        cfg = ac.default_config()
        assert cfg.enabled is False and cfg.topics == () and cfg.enabled_topics == ()
        assert cfg.daily_limit == ac.DEFAULT_DAILY_LIMIT == 10
        assert cfg.to_json() == DEFAULT_RECORD
        view = dict(DEFAULT_RECORD)
        view.pop("google_sub")  # the bound account's id stays server-side
        assert cfg.to_view(now=NOW) == view

    async def test_nothing_stored_reads_as_off(self, db):
        assert await ac.load_autoreply_config(db, ALICE) == ac.default_config()

    @pytest.mark.parametrize("raw", [
        "not json",
        json.dumps([1]),
        json.dumps("on"),
        json.dumps({"enabled": "yes"}),
        json.dumps({"enabled": True, "daily_limit": 0}),
        json.dumps({"enabled": True, "daily_limit": 51}),
        json.dumps({"enabled": True, "daily_limit": True}),
        json.dumps({"enabled": True, "daily_limit": "10"}),
        json.dumps({"enabled": True, "agent_ref": "not-a-ref"}),
        json.dumps({"enabled": True, "never_disclose": "Falcon"}),
        json.dumps({"enabled": True, "never_disclose": ["x"]}),
        json.dumps({"enabled": True, "never_disclose": [f"term {i}" for i in range(51)]}),
        json.dumps({"enabled": True, "google_sub": 5}),
        json.dumps({"enabled": True, "account_email": "a" * 400}),
        json.dumps({"enabled": True, "topics": "all"}),
        json.dumps({"enabled": True, "topics": [5]}),
    ])
    async def test_corrupt_record_reads_as_off(self, db, raw):
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: raw})
        assert await ac.load_autoreply_config(db, ALICE) == ac.default_config()

    @pytest.mark.parametrize("change", [
        {"id": "meetup"},                                 # not a server id
        {"id": None},
        {"name": "ab"},
        {"about": "too short"},
        {"examples": ["x" * 301]},
        {"examples": ["Where is it held, please?"] * 6},
        {"counter_examples": ["short"]},
        {"senders": ["not an address"]},
        {"brief": "too short"},
        {"sources": {"insights": "yes"}},
        {"sources": {"folder": "proj/../secrets"}},
        {"enabled": "on"},
        {"dry_run": "done"},
        {"dry_run": {"match_hash": "abc", "at": NOW.isoformat(), "eligible": 1, "matched": 0,
                     "rate": 0.0}},
        {"dry_run": {"match_hash": "", "at": "yesterday", "eligible": 0, "matched": 0,
                     "rate": 0.0}},
        {"dry_run": {"match_hash": "", "at": "", "eligible": 2, "matched": 3, "rate": 0.5}},
        {"dry_run": {"match_hash": "", "at": "", "eligible": 2, "matched": 1, "rate": 2.0}},
        {"dry_run": {"match_hash": "", "at": "", "eligible": -1, "matched": 0, "rate": 0.0}},
    ])
    async def test_corrupt_topic_reads_as_off(self, db, change):
        good = _one_on()
        record = good.to_json() | {"enabled": True}
        record["topics"][0].update(change)
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert await ac.load_autoreply_config(db, ALICE) == ac.default_config()

    async def test_too_many_topics_or_too_many_on_read_as_off(self, db):
        cfg = _with_topics(*[_topic(name=f"Meetup logistics {i}") for i in range(10)])
        record = cfg.to_json()
        record["topics"].append({**record["topics"][0], "id": "t_0000ffff"})
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert await ac.load_autoreply_config(db, ALICE) == ac.default_config()

        for t in cfg.topics[:6]:
            cfg = _tested(cfg, t.id)
        record = cfg.to_json()
        for t in record["topics"][:6]:
            t["enabled"] = True  # six on, each with a valid breadth test
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert await ac.load_autoreply_config(db, ALICE) == ac.default_config()
        record["topics"][5]["enabled"] = False  # five is the limit
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert len((await ac.load_autoreply_config(db, ALICE)).enabled_topics) == 5

    async def test_duplicate_topic_ids_read_as_off(self, db):
        cfg = _with_topics(_topic(), _topic(name="Meetup parking"))
        record = cfg.to_json()
        record["topics"][1]["id"] = record["topics"][0]["id"]
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert await ac.load_autoreply_config(db, ALICE) == ac.default_config()

    async def test_unreadable_record_is_logged_without_its_values(self, db, monkeypatch):
        calls = []

        class _Log:
            def warning(self, *args, **kwargs):
                calls.append((args, kwargs))

        monkeypatch.setattr(ac, "log", _Log())
        record = {"enabled": True, "never_disclose": ["Project Falcon"], "daily_limit": 999}
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert await ac.load_autoreply_config(db, ALICE) == ac.default_config()
        assert len(calls) == 1
        assert "Falcon" not in repr(calls) and "999" not in repr(calls)

    async def test_a_failing_database_read_is_raised_not_read_as_off(self):
        class _BrokenDB:
            async def get_setting(self, user_id, key):
                raise RuntimeError("database is locked")

        with pytest.raises(RuntimeError):
            await ac.load_autoreply_config(_BrokenDB(), ALICE)

    async def test_save_and_load_round_trip_per_user(self, db):
        cfg = _patch(_one_on(), {"agent_ref": REF, "daily_limit": 7,
                                 "never_disclose": ["Project Falcon"]}).config
        cfg = ac.enable(cfg, google_sub="1234567890", account_email="alice@x.co", now=NOW)
        stored = await ac.save_autoreply_config(db, ALICE, cfg, now=NOW + timedelta(seconds=1))
        assert stored.updated_at == (NOW + timedelta(seconds=1)).isoformat()
        loaded = await ac.load_autoreply_config(db, ALICE)
        assert loaded == stored
        assert loaded.enabled and loaded.enabled_topics and loaded.google_sub == "1234567890"
        assert json.loads(await db.get_setting(ALICE, "google_oauth:gmail_autoreply")) == (
            stored.to_json())
        # Bob's config is his own.
        assert await ac.load_autoreply_config(db, BOB) == ac.default_config()

    async def test_stored_record_shape(self, db):
        cfg = _one_on()
        stored = await ac.save_autoreply_config(db, ALICE, cfg, now=NOW)
        record = json.loads(await db.get_setting(ALICE, ac.AUTOREPLY_KEY))
        assert set(record) == set(DEFAULT_RECORD)
        topic = record["topics"][0]
        assert list(topic) == ["id", "name", "enabled", "about", "not_this", "examples",
                               "counter_examples", "senders", "brief", "sources", "dry_run"]
        assert topic["sources"] == {"folder": "", "insights": False, "playbooks": True,
                                    "web_search": False}
        assert topic["dry_run"] == {"match_hash": stored.topics[0].match_hash,
                                    "at": NOW.isoformat(), "eligible": 20, "matched": 1,
                                    "rate": 0.05, "since": NOW.isoformat()}

    async def test_topic_on_without_a_test_of_its_wording_reads_as_off(self, db):
        record = _one_on().to_json()
        record["topics"][0]["about"] += " Also parking."  # edited behind the PUT's back
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        cfg = await ac.load_autoreply_config(db, ALICE)
        assert cfg.topics and cfg.topics[0].enabled is False

    async def test_save_refuses_a_record_that_would_not_read_back(self, db):
        bad = replace(ac.default_config(), daily_limit=0)
        with pytest.raises(ValueError):
            await ac.save_autoreply_config(db, ALICE, bad)
        assert await db.get_setting(ALICE, ac.AUTOREPLY_KEY) is None


# ── the PUT parser: limits and normalization ─────────────────────────


def _n_examples(n, size=40):
    return [(f"Example-{i}-" + "x" * size)[:size] for i in range(n)]


BAD_TOP_LEVEL = [
    {"enabled": "yes"},
    {"enabled": 1},
    {"daily_limit": 0},
    {"daily_limit": 51},
    {"daily_limit": "10"},
    {"daily_limit": True},
    {"daily_limit": 2.5},
    {"agent_ref": "meetup-bot"},
    {"agent_ref": "process:meetup-bot:xyz"},
    {"agent_ref": 5},
    {"never_disclose": "Falcon"},
    {"never_disclose": [5]},
    {"never_disclose": ["x"]},
    {"never_disclose": ["x" * 81]},
    {"never_disclose": [f"term {i}" for i in range(51)]},
    {"topics": "meetup"},
    {"topics": [5]},
    {"topics": [_topic(name=f"Meetup logistics {i}") for i in range(11)]},
]

BAD_TOPIC = [
    {"name": "ab"},
    {"name": "x" * 61},
    {"name": 5},
    {"name": None},
    {"about": "Emails about it."},                          # < 20 characters
    {"about": "Emails asking about the meetup " + "x" * 400},
    {"about": None},
    {"not_this": "x" * 401},
    {"examples": "Where is the meetup?"},
    {"examples": ["short"]},
    {"examples": ["x" * 301]},
    {"examples": _n_examples(6)},
    {"examples": [5]},
    {"counter_examples": ["short"]},
    {"counter_examples": _n_examples(6)},
    {"senders": ["not an address"]},
    {"senders": "alice@ok.com"},
    {"senders": [f"u{i}@ok.com" for i in range(51)]},
    {"brief": "Too short to be useful."},
    {"brief": "x" * 4001},
    {"brief": None},
    {"enabled": "yes"},
    {"sources": "all"},
    {"sources": {"insights": "yes"}},
    {"sources": {"playbooks": 1}},
    {"sources": {"web_search": "no"}},
    {"sources": {"folder": 5}},
    {"sources": {"folder": "vfs:"}},
    {"sources": {"folder": "proj/../../etc"}},
    {"sources": {"folder": "proj/a\x00b"}},
    {"sources": {"folder": "proj/" + "d" * 600}},
]


class TestPatchLimits:
    @pytest.mark.parametrize("patch", [[], "enabled", None, 5])
    def test_patch_must_be_an_object(self, patch):
        with pytest.raises(ac.ConfigError) as err:
            _patch(ac.default_config(), patch)
        assert err.value.status == 400

    @pytest.mark.parametrize("patch", BAD_TOP_LEVEL)
    def test_bad_top_level_values_are_400(self, patch):
        with pytest.raises(ac.ConfigError) as err:
            _patch(ac.default_config(), patch)
        assert err.value.status == 400 and err.value.detail == str(err.value)
        assert err.value.detail

    @pytest.mark.parametrize("change", BAD_TOPIC)
    def test_bad_topic_values_are_400(self, change):
        with pytest.raises(ac.ConfigError) as err:
            _patch(ac.default_config(), {"topics": [_topic(**change)]})
        assert err.value.status == 400 and err.value.detail

    def test_never_disclose_errors_never_echo_a_term(self):
        with pytest.raises(ac.ConfigError) as err:
            _patch(ac.default_config(), {"never_disclose": ["Falcon", "S" + "e" * 90]})
        assert "Falcon" not in err.value.detail and "eeee" not in err.value.detail

    def test_the_bounds_are_accepted(self):
        bounds = _topic(
            name="Abc",
            about="Emails about it: a b c",                 # 20 characters, 6 words
            not_this="x" * 400,
            examples=_n_examples(5, 300),
            counter_examples=_n_examples(5, 10),
            brief="b" * 50,
        )
        cfg = _patch(ac.default_config(), {
            "daily_limit": 50, "agent_ref": REF,
            "never_disclose": ["ab", "y" * 80] + [f"term {i}" for i in range(48)],
            "topics": [bounds] + [_topic(name=f"Meetup logistics {i}", brief="b" * 4000,
                                         examples=["Where is the meetup going to be held?"])
                                  for i in range(9)],
        }).config
        assert cfg.daily_limit == 50 and cfg.agent_ref == REF
        assert len(cfg.never_disclose) == 50 and len(cfg.topics) == 10
        assert _patch(cfg, {"daily_limit": 1}).config.daily_limit == 1
        assert _patch(cfg, {"agent_ref": ""}).config.agent_ref == ""

    def test_absent_and_null_fields_are_left_unchanged(self):
        cfg = _patch(_one_on(), {"agent_ref": REF, "daily_limit": 7,
                                 "never_disclose": ["Project Falcon"]}).config
        for patch in ({}, {"enabled": None, "agent_ref": None, "daily_limit": None,
                           "never_disclose": None, "topics": None}, {"unknown": 1}):
            res = _patch(cfg, patch)
            assert res.config == cfg and not res.wants_enable and res.notice == ""

    def test_normalization(self):
        cfg = _patch(ac.default_config(), {
            "never_disclose": ["  Project   Falcon ", "project falcon", "ŠIFRA", "sifra", "  "],
            "topics": [_topic(
                name="  Meetup\tlogistics  ",
                about="Emails asking\nwhen or where the meetup is,​ or how to register.",
                examples=["  Where is the\n meetup going to be held?  ", "   ", ""],
                counter_examples=["", "Can I give a talk at the meetup?"],
                senders=[" Alice@OK.com ", "ok.org", "@ok.org", "@Corp.IO"],
                brief="Line one of the brief, which is long enough.\r\n\r\n\r\n\r\n"
                      "Line two.\x07   \n",
            )],
        }).config
        assert cfg.never_disclose == ("Project Falcon", "ŠIFRA")
        t = cfg.topics[0]
        assert t.name == "Meetup logistics"
        assert t.about == "Emails asking when or where the meetup is, or how to register."
        assert t.examples == ("Where is the meetup going to be held?",)
        assert t.counter_examples == ("Can I give a talk at the meetup?",)
        assert t.senders == ("alice@ok.com", "@ok.org", "@corp.io")
        assert t.brief == "Line one of the brief, which is long enough.\n\nLine two."

    @pytest.mark.parametrize("given, stored", [
        ("", ""),
        ("   ", ""),
        ("vfs:meetup/faq", "vfs:meetup/faq"),
        ("VFS:meetup/faq/", "vfs:meetup/faq"),
        ("meetup/faq", "vfs:meetup/faq"),
        ("/meetup//./faq", "vfs:meetup/faq"),
        ("meetup\\faq", "vfs:meetup/faq"),
        ("meetup", "vfs:meetup/"),           # a project's root
        ("vfs:meetup/", "vfs:meetup/"),
    ])
    def test_folder_is_stored_as_a_vfs_folder(self, given, stored):
        topic = _topic(sources={"folder": given})
        cfg = _patch(ac.default_config(), {"topics": [topic]}).config
        assert cfg.topics[0].sources.folder == stored

    def test_sources_default_and_partial(self):
        cfg = _patch(ac.default_config(), {"topics": [
            _topic(sources=None), _topic(name="Meetup parking", sources={"web_search": True})]}
        ).config
        assert cfg.topics[0].sources == ac.TopicSources(
            folder="", insights=False, playbooks=True, web_search=False)
        assert cfg.topics[1].sources == ac.TopicSources(
            folder="", insights=False, playbooks=True, web_search=True)

    def test_server_managed_fields_are_ignored(self):
        cfg = ac.enable(_one_on(), google_sub="sub-1", account_email="alice@x.co", now=NOW)
        cfg = replace(cfg, disabled_reason="", updated_at="2026-10-14T12:00:00+00:00")
        res = _patch(cfg, {"google_sub": "evil", "account_email": "evil@x.co",
                           "enabled_at": "1999-01-01", "disabled_reason": "x",
                           "updated_at": "1999-01-01"})
        assert res.config == cfg


# ── the broad-topic lint (EN/HR) ─────────────────────────────────────


GOOD_ABOUT = _topic()["about"]


class TestLint:
    @pytest.mark.parametrize("name, about", [
        ("Any question", GOOD_ABOUT),
        ("All mail", GOOD_ABOUT),
        ("Everything", GOOD_ABOUT),
        ("General", GOOD_ABOUT),
        ("Misc stuff", GOOD_ABOUT),
        ("Other requests", GOOD_ABOUT),
        ("Whatever comes", GOOD_ABOUT),
        ("Meetup", "Any question somebody asks about the meetup or anything else."),
        ("Meetup", "All emails sent to me about the meetup or the venue."),
        ("Meetup", "Every email about the Zagreb meetup and its venue."),
        ("Meetup", "Anything people want to know about the Zagreb meetup."),
        ("Meetup", "Emails with general inquiries about the Zagreb meetup."),
        ("Meetup", "Emails asking about the meetup, or anything else at all."),
        ("Meetup", "Emails asking everything about the meetup and its venue."),
        ("Meetup", "Replies to any e-mail about the Zagreb meetup venue."),
        ("Meetup", "Questions in ALL E-MAILS about the Zagreb meetup venue."),
        ("Meetup", "Questions where people ask any question about the meetup."),
        ("Meetup", "  “Whatever people send about the Zagreb meetup venue.”"),
        # Croatian, with and without diacritics, any case.
        ("Sve o meetupu", GOOD_ABOUT),
        ("Svi upiti", GOOD_ABOUT),
        ("Bilo koji upit", GOOD_ABOUT),
        ("Općenito", GOOD_ABOUT),
        ("OPĆENITO", GOOD_ABOUT),
        ("Opcenito", GOOD_ABOUT),
        ("Meetup", "Općenito pitanje o meetupu u Zagrebu i registraciji."),
        ("Meetup", "Odgovori na svi mailovi o meetupu u Zagrebu i lokaciji."),
        ("Meetup", "Odgovori na sve e-mailove o meetupu u Zagrebu."),
        ("Meetup", "Odgovori na SVE MAILOVE o meetupu u Zagrebu i lokaciji."),
        ("Meetup", "Odgovaraj svim mailovima o meetupu u Zagrebu i lokaciji."),
        ("Meetup", "Ovo su sva pitanja o meetupu u Zagrebu i registraciji."),
        ("Meetup", "Ovo su SVA PITANJA o meetupu u Zagrebu i registraciji."),
        ("Meetup", "Pitanja o meetupu: bilo što o lokaciji i registraciji."),
        ("Meetup", "Pitanja o meetupu: bilo sto o lokaciji i registraciji."),
    ])
    def test_broad_wording_is_refused(self, name, about):
        reason = ac.lint_topic(name, about, ["Where is the meetup going to be held?"])
        assert reason.startswith("Too broad: ")
        with pytest.raises(ac.ConfigError) as err:
            _patch(ac.default_config(), {"topics": [_topic(name=name, about=about)]})
        assert err.value.status == 400 and "Too broad: " in err.value.detail

    def test_too_few_words_and_no_example_are_refused(self):
        assert "6 words" in ac.lint_topic("Meetup", "Meetup venue and registration", ["x" * 20])
        assert "example" in ac.lint_topic("Meetup", GOOD_ABOUT, [])
        assert "example" in ac.lint_topic("Meetup", GOOD_ABOUT, ["   "])
        for change in ({"about": "Meetup venue and registration"},
                       {"examples": []}, {"examples": ["   ", ""]}):
            with pytest.raises(ac.ConfigError) as err:
                _patch(ac.default_config(), {"topics": [_topic(**change)]})
            assert err.value.status == 400 and "Too broad: " in err.value.detail

    @pytest.mark.parametrize("name, about", [
        ("Meetup logistics", GOOD_ABOUT),
        ("All-hands logistics", "Emails asking when the company all-hands is and where to join."),
        ("Allegro tickets", "Emails asking how to buy tickets for the Allegro concert."),
        ("Generator rentals", "Emails asking whether our diesel generators can be rented."),
        ("Others' invoices", "Emails asking where to send invoices for the others' event."),
        ("Company email setup", "Emails from new staff asking how to set up their company email."),
        ("Meetup", "Emails from many emails lists? No: people asking where the meetup is."),
        ("Raspored meetupa", "Pitanja o rasporedu Zagreb AI Meetupa 14. studenog i prijavi."),
        ("Svibanjski meetup", "Pitanja gdje se održava svibanjski meetup i kako se prijaviti."),
        ("Promjena maila", "Pitanja kako promijeniti svoj email ili svoje mailove na portalu."),
        ("Meetup", "Emails asking about the meetup; a general question is not this."),
    ])
    def test_narrow_wording_passes(self, name, about):
        assert ac.lint_topic(name, about, ["Where is the meetup going to be held?"]) == ""
        cfg = _patch(ac.default_config(), {"topics": [_topic(name=name, about=about)]}).config
        assert cfg.topics[0].name == name

    def test_fold(self):
        assert ac.fold("Općenito ĐAK Šifra ŽČĆ") == "opcenito dak sifra zcc"
        assert ac.fold("Straße") == "strasse"


# ── the match hash ───────────────────────────────────────────────────


class TestMatchHash:
    def test_hex_and_stable(self):
        a = _with_topics(_topic()).topics[0]
        b = _with_topics(_topic()).topics[0]
        assert len(a.match_hash) == 64 and int(a.match_hash, 16) >= 0
        assert a.match_hash == b.match_hash == ac.match_hash(a)  # different ids, same hash

    async def test_survives_save_and_load(self, db):
        cfg = _with_topics(_topic())
        await ac.save_autoreply_config(db, ALICE, cfg)
        loaded = await ac.load_autoreply_config(db, ALICE)
        assert loaded.topics[0].match_hash == cfg.topics[0].match_hash

    @pytest.mark.parametrize("change", [
        {"brief": "Totally different brief text that is still long enough to keep."},
        {"sources": {"folder": "vfs:meetup/faq", "insights": True, "playbooks": False,
                     "web_search": True}},
        {"enabled": True},
        {"id": "t_0000ffff"},
        {"dry_run": ac.DryRun(match_hash="0" * 64, at=NOW.isoformat(), eligible=9, matched=1)},
    ])
    def test_brief_sources_switch_id_and_dry_run_are_left_out(self, change):
        base = _with_topics(_topic()).topics[0]
        if "brief" in change or "sources" in change:
            other = _with_topics(_topic(**change)).topics[0]
        else:
            other = replace(base, **change)
        assert other.match_hash == base.match_hash

    @pytest.mark.parametrize("change", [
        {"name": "Meetup logistics 2"},
        {"about": GOOD_ABOUT + " Also parking."},
        {"not_this": "Sponsorship only."},
        {"not_this": ""},
        {"examples": ["Where is the meetup going to be held?", "How do I register for it?"]},
        {"counter_examples": []},
        {"senders": ["@ok.com"]},
    ])
    def test_every_matching_field_changes_it(self, change):
        base = _with_topics(_topic()).topics[0]
        assert _with_topics(_topic(**change)).topics[0].match_hash != base.match_hash

    def test_normalized_spelling_hashes_the_same(self):
        base = _with_topics(_topic(senders=["alice@ok.com", "@ok.org"])).topics[0]
        same = _with_topics(_topic(name="  Meetup   logistics ",
                                   senders=[" Alice@OK.com", "ok.org"])).topics[0]
        assert same.match_hash == base.match_hash


# ── topic ids ────────────────────────────────────────────────────────


class TestTopicIds:
    def test_server_assigns_stable_ids(self):
        cfg = _with_topics(_topic(), _topic(name="Meetup parking"))
        ids = [t.id for t in cfg.topics]
        assert all(ac._TOPIC_ID_RE.match(i) for i in ids) and len(set(ids)) == 2
        # Edited and re-sent with its id: same topic, same id, its breadth test kept.
        cfg = _tested(cfg, ids[0])
        edited = _patch(cfg, {"topics": [
            _view_topic(cfg.topics[1]),
            {**_view_topic(cfg.topics[0]),
             "brief": "A brand new brief, public like the old one, and long enough."},
        ]}).config
        assert [t.id for t in edited.topics] == [ids[1], ids[0]]
        assert edited.topic(ids[0]).dry_run == cfg.topic(ids[0]).dry_run

    @pytest.mark.parametrize("given", ["t_deadbeef", "anything", "", 5, None])
    def test_a_client_id_is_never_adopted(self, given):
        cfg = _with_topics(_topic(id=given))
        assert cfg.topics[0].id != given and ac._TOPIC_ID_RE.match(cfg.topics[0].id)

    def test_client_dry_run_is_ignored(self):
        forged = {"match_hash": "f" * 64, "at": NOW.isoformat(), "eligible": 100,
                  "matched": 0, "rate": 0.0}
        cfg = _with_topics(_topic(dry_run=forged))
        assert cfg.topics[0].dry_run == ac.DryRun()
        tested = _tested(cfg, cfg.topics[0].id)
        resent = _patch(tested, {"topics": [{**_view_topic(tested.topics[0]),
                                             "dry_run": forged}]}).config
        assert resent.topics[0].dry_run == tested.topics[0].dry_run

    def test_a_deleted_topics_id_is_never_reused(self, monkeypatch):
        cfg = _with_topics(_topic())
        old = cfg.topics[0].id
        draws = iter([old[2:], "0000abcd"])
        monkeypatch.setattr(ac.secrets, "token_hex", lambda n: next(draws))
        cfg = _patch(cfg, {"topics": [_topic(name="Meetup parking")]}).config
        assert [t.id for t in cfg.topics] == ["t_0000abcd"]

    def test_the_same_topic_twice_is_refused(self):
        cfg = _with_topics(_topic())
        t = _view_topic(cfg.topics[0])
        with pytest.raises(ac.ConfigError) as err:
            _patch(cfg, {"topics": [t, {**t, "name": "Meetup parking"}]})
        assert err.value.status == 400 and "twice" in err.value.detail

    def test_omitting_a_topic_deletes_it(self):
        cfg = _with_topics(_topic(), _topic(name="Meetup parking"))
        kept = _patch(cfg, {"topics": [_view_topic(cfg.topics[1])]}).config
        assert [t.id for t in kept.topics] == [cfg.topics[1].id]
        assert _patch(cfg, {"topics": []}).config.topics == ()


# ── the breadth gate ─────────────────────────────────────────────────


def _switch_on(cfg, topic_id, now=NOW):
    topics = [_view_topic(t, enabled=(t.enabled or t.id == topic_id)) for t in cfg.topics]
    return _patch(cfg, {"topics": topics}, now=now)


class TestDryRunGate:
    def test_no_breadth_test_cannot_switch_on(self):
        cfg = _with_topics(_topic())
        gate = ac.dry_run_gate(cfg.topics[0], now=NOW)
        assert not gate.ok and "breadth test" in gate.reason
        with pytest.raises(ac.ConfigError) as err:
            _switch_on(cfg, cfg.topics[0].id)
        assert err.value.status == 409 and "Meetup logistics" in err.value.detail
        # A brand-new topic sent switched on is refused the same way.
        with pytest.raises(ac.ConfigError) as err:
            _patch(ac.default_config(), {"topics": [_topic(enabled=True)]})
        assert err.value.status == 409

    def test_a_test_of_an_older_wording_does_not_count(self):
        cfg = _with_topics(_topic())
        tid = cfg.topics[0].id
        cfg = _tested(cfg, tid)
        edited = _patch(cfg, {"topics": [{**_view_topic(cfg.topics[0]),
                                          "about": GOOD_ABOUT + " Also parking."}]}).config
        assert not ac.dry_run_gate(edited.topic(tid), now=NOW).ok
        with pytest.raises(ac.ConfigError) as err:
            _switch_on(edited, tid)
        assert err.value.status == 409 and "changed" in err.value.detail
        # A test started before an edit doesn't open the gate either.
        stale = ac.record_dry_run(edited, tid, match_hash=cfg.topic(tid).match_hash,
                                  eligible=20, matched=1, now=NOW)
        assert not ac.dry_run_gate(stale.topic(tid), now=NOW).ok
        assert _switch_on(_tested(edited, tid), tid).config.topic(tid).enabled

    def test_at_most_30_days_old(self):
        cfg = _with_topics(_topic())
        tid = cfg.topics[0].id
        cfg = _tested(cfg, tid, at=NOW - timedelta(days=30))
        assert ac.dry_run_gate(cfg.topic(tid), now=NOW).ok
        assert _switch_on(cfg, tid).config.topic(tid).enabled
        old = _tested(cfg, tid, at=NOW - timedelta(days=30, minutes=1))
        assert not ac.dry_run_gate(old.topic(tid), now=NOW).ok
        with pytest.raises(ac.ConfigError) as err:
            _switch_on(old, tid)
        assert err.value.status == 409 and "30 days" in err.value.detail
        future = _tested(cfg, tid, at=NOW + timedelta(hours=1))
        assert not ac.dry_run_gate(future.topic(tid), now=NOW).ok

    @pytest.mark.parametrize("eligible, matched, ok, note, warning", [
        (20, 0, True, "", False),
        (20, 1, True, "", False),
        (20, 2, True, "", True),      # 10 %: passes, with a warning
        (20, 5, True, "", True),      # 25 %: the limit passes
        (20, 6, False, "", False),    # 30 %
        (30, 30, False, "", False),
        (4, 4, True, ac.TOO_LITTLE_MAIL, False),   # under 5 eligible: passes with a note
        (0, 0, True, ac.TOO_LITTLE_MAIL, False),
        (5, 2, False, "", False),     # 5 eligible is enough to judge
    ])
    def test_rate(self, eligible, matched, ok, note, warning):
        cfg = _with_topics(_topic())
        tid = cfg.topics[0].id
        cfg = _tested(cfg, tid, eligible=eligible, matched=matched)
        gate = ac.dry_run_gate(cfg.topic(tid), now=NOW)
        assert (gate.ok, gate.note, bool(gate.warning)) == (ok, note, warning)
        if ok:
            assert _switch_on(cfg, tid).config.topic(tid).enabled
        else:
            assert "narrower" in gate.reason
            with pytest.raises(ac.ConfigError) as err:
                _switch_on(cfg, tid)
            assert err.value.status == 409

    def test_record_dry_run(self):
        cfg = _with_topics(_topic())
        tid = cfg.topics[0].id
        cfg = _tested(cfg, tid, eligible=30, matched=7)
        assert cfg.topic(tid).dry_run == ac.DryRun(
            match_hash=cfg.topic(tid).match_hash, at=NOW.isoformat(), eligible=30, matched=7,
            rate=0.2333, since=NOW.isoformat())
        with pytest.raises(ac.ConfigError) as err:
            ac.record_dry_run(cfg, "t_00000000", match_hash="0" * 64, eligible=1, matched=0)
        assert err.value.status == 404
        for bad in ({"eligible": 2, "matched": 3}, {"eligible": -1, "matched": 0},
                    {"eligible": 2, "matched": 1, "match_hash": "nope"}):
            args = {"match_hash": "0" * 64, **bad}
            with pytest.raises(ValueError):
                ac.record_dry_run(cfg, tid, now=NOW, **args)

    def test_at_most_five_topics_on(self):
        cfg = _with_topics(*[_topic(name=f"Meetup logistics {i}") for i in range(6)])
        for t in cfg.topics:
            cfg = _tested(cfg, t.id)
        for t in cfg.topics[:5]:
            cfg = _switch_on(cfg, t.id).config
        assert len(cfg.enabled_topics) == 5
        with pytest.raises(ac.ConfigError) as err:
            _switch_on(cfg, cfg.topics[5].id)
        assert err.value.status == 400 and "5" in err.value.detail

    def test_an_already_on_topic_is_not_regated(self):
        cfg = _one_on()
        later = NOW + timedelta(days=45)  # its breadth test has aged out meanwhile
        res = _patch(cfg, {"topics": [_view_topic(cfg.topics[0])],
                           "daily_limit": 5}, now=later)
        assert res.config.topics[0].enabled and res.notice == ""
        # …but turning auto-replies on again needs a fresh test.
        assert ac.enable_problems(res.config, now=later)

    def test_view_carries_the_gate(self):
        cfg = _with_topics(_topic(), _topic(name="Meetup parking"))
        cfg = _tested(cfg, cfg.topics[0].id, eligible=3, matched=1)
        view = cfg.to_view(now=NOW)
        assert view["topics"][0]["gate"] == {"ok": True, "reason": "", "note": ac.TOO_LITTLE_MAIL,
                                             "warning": ""}
        assert view["topics"][1]["gate"]["ok"] is False
        assert set(view["topics"][0]) == set(cfg.topics[0].to_json()) | {"gate"}


# ── editing what a topic that is on matches turns it off ─────────────


class TestEditTurnsTopicOff:
    @pytest.mark.parametrize("change", [
        {"name": "Meetup logistics 2"},
        {"about": GOOD_ABOUT + " Also parking."},
        {"not_this": "Sponsorship only."},
        {"examples": ["Where is the meetup going to be held?", "How do I register for it?"]},
        {"counter_examples": ["Can I sponsor the meetup this year?"]},
        {"senders": ["@ok.com"]},
    ])
    def test_matching_edit_turns_it_off_with_a_notice(self, change):
        cfg = _one_on()
        tid = cfg.topics[0].id
        res = _patch(cfg, {"topics": [{**_view_topic(cfg.topics[0]), **change}]})
        assert res.config.topic(tid).enabled is False
        assert res.auto_disabled == (tid,) and res.notice == ac.RERUN_NOTICE == (
            "re-run the breadth test")
        # The old test is kept, but it no longer opens the gate.
        assert res.config.topic(tid).dry_run == cfg.topic(tid).dry_run
        assert not ac.dry_run_gate(res.config.topic(tid), now=NOW).ok

    @pytest.mark.parametrize("change", [
        {"brief": "A rewritten brief, still public, still long enough to pass."},
        {"sources": {"folder": "vfs:meetup/faq", "insights": True, "playbooks": False,
                     "web_search": True}},
        {"about": "  " + GOOD_ABOUT.replace(" ", "  ") + " "},   # same once normalized
    ])
    def test_other_edits_keep_it_on(self, change):
        cfg = _one_on()
        res = _patch(cfg, {"topics": [{**_view_topic(cfg.topics[0]), **change}]})
        assert res.config.topics[0].enabled and res.auto_disabled == () and res.notice == ""

    def test_switched_off_by_the_user_is_no_notice(self):
        cfg = _one_on()
        res = _patch(cfg, {"topics": [{**_view_topic(cfg.topics[0]), "enabled": False,
                                       "about": GOOD_ABOUT + " Also parking."}]})
        assert res.config.topics[0].enabled is False and res.notice == ""

    def test_a_new_breadth_test_lets_it_back_on(self):
        cfg = _one_on()
        tid = cfg.topics[0].id
        edited = _patch(cfg, {"topics": [{**_view_topic(cfg.topics[0]),
                                          "about": GOOD_ABOUT + " Also parking."}]}).config
        back = _switch_on(_tested(edited, tid), tid)
        assert back.config.topic(tid).enabled and back.notice == ""


# ── switching the whole feature ──────────────────────────────────────


class TestEnableDisable:
    def test_a_put_never_switches_on_by_itself(self):
        cfg = _patch(_one_on(), {"agent_ref": REF}).config
        res = _patch(cfg, {"enabled": True})
        assert res.wants_enable is True and res.config.enabled is False
        on = ac.enable(res.config, google_sub="sub-1", account_email="alice@x.co", now=NOW)
        assert (on.enabled, on.google_sub, on.account_email, on.enabled_at,
                on.disabled_reason) == (True, "sub-1", "alice@x.co", NOW.isoformat(), "")
        # Already on: nothing to switch, no new baseline.
        again = _patch(on, {"enabled": True})
        assert again.wants_enable is False and again.config == on

    def test_switching_off(self):
        on = ac.enable(_one_on(), google_sub="sub-1", account_email="alice@x.co", now=NOW)
        off = _patch(on, {"enabled": False})
        assert off.config.enabled is False and off.wants_enable is False
        assert off.config.disabled_reason == ""
        paused = ac.disable(on, "auto_paused")
        assert paused.enabled is False and paused.disabled_reason == "auto_paused"
        # Saving while off keeps the reason the user should see.
        assert _patch(paused, {"enabled": False}).config.disabled_reason == "auto_paused"
        assert ac.enable(paused, google_sub="sub-1", account_email="alice@x.co",
                         now=NOW).disabled_reason == ""

    def test_enable_problems(self):
        assert ac.enable_problems(ac.default_config(), now=NOW) == [
            "choose the agent that writes the replies", "turn on at least one topic"]
        cfg = _patch(_one_on(), {"agent_ref": REF}).config
        assert ac.enable_problems(cfg, now=NOW) == []
        later = NOW + timedelta(days=31)
        problems = ac.enable_problems(cfg, now=later)
        assert len(problems) == 1 and "Meetup logistics" in problems[0]

    def test_disable_topic(self):
        cfg = _one_on()
        tid = cfg.topics[0].id
        off = ac.disable_topic(cfg, tid)
        # The drift breaker drops the breadth test too: on again needs a new one.
        assert off.topic(tid).enabled is False and off.topic(tid).dry_run == ac.DryRun()
        assert ac.disable_topic(cfg, "t_00000000") == cfg


# ── a topic that is on always has a passing test of its wording ──────


def _drift_off():
    """One topic, on, then switched off by the drift breaker — and the
    config as a page opened before that still shows it (on, old test)."""
    seen = _one_on()
    return ac.disable_topic(seen, seen.topics[0].id), seen


class TestDriftedTopicNeedsANewTest:
    """Findings 15/18: the drift breaker's switch-off can't be undone by a PUT."""

    def test_switching_it_back_on_needs_a_new_breadth_test(self):
        off, _seen = _drift_off()
        tid = off.topics[0].id
        gate = ac.dry_run_gate(off.topic(tid), now=NOW)
        assert not gate.ok and "breadth test" in gate.reason
        with pytest.raises(ac.ConfigError) as err:
            _switch_on(off, tid)
        assert err.value.status == 409 and "breadth test" in err.value.detail
        assert _switch_on(_tested(off, tid), tid).config.topic(tid).enabled

    def test_a_stale_page_cant_switch_it_back_on(self):
        off, seen = _drift_off()
        # The page still shows it on (with its old test) and saves an edit of
        # something else: the whole list goes back, the topic in it "on".
        stale = [{**_view_topic(t), "brief": t.brief + " Doors open at 17:30."}
                 for t in seen.topics]
        with pytest.raises(ac.ConfigError) as err:
            _patch(off, {"topics": stale})
        assert err.value.status == 409

    @pytest.mark.parametrize("sent", [
        lambda t: t.dry_run.to_json(),                                       # the old one
        lambda t: {"match_hash": t.match_hash, "at": NOW.isoformat(), "eligible": 100,
                   "matched": 0, "rate": 0.0},                               # a forged one
    ])
    def test_a_put_never_sets_or_restores_a_breadth_test(self, sent):
        off, seen = _drift_off()
        tid = off.topics[0].id
        body = {**_view_topic(seen.topic(tid)), "dry_run": sent(seen.topic(tid))}
        with pytest.raises(ac.ConfigError):
            _patch(off, {"topics": [body]})
        kept = _patch(off, {"topics": [{**body, "enabled": False}]}).config
        assert kept.topic(tid).dry_run == ac.DryRun()

    def test_a_test_cant_be_moved_to_another_topic(self):
        cfg = _with_topics(_topic(), _topic(name="Meetup parking",
                                            about=GOOD_ABOUT + " Also parking."))
        a, b = (t.id for t in cfg.topics)
        cfg = _tested(cfg, a)
        # B's wording sent under A's id (which has a passing test): refused.
        moved = {**_view_topic(cfg.topic(b)), "id": a, "enabled": True}
        with pytest.raises(ac.ConfigError) as err:
            _patch(cfg, {"topics": [moved]})
        assert err.value.status == 409

    async def test_the_loader_reads_it_off_whatever_wrote_it(self, db):
        off, _seen = _drift_off()
        record = off.to_json()
        record["topics"][0]["enabled"] = True       # on again, behind the PUT's back
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert (await ac.load_autoreply_config(db, ALICE)).enabled_topics == ()


class TestReTestOfATopicThatIsOn:
    """Finding 7: a re-test that matched too much switches the topic off."""

    def test_too_broad_switches_it_off_and_keeps_the_rate_on_show(self):
        cfg = _one_on()
        tid = cfg.topics[0].id
        again = _tested(cfg, tid, eligible=10, matched=10)
        topic = again.topic(tid)
        assert topic.enabled is False and again.enabled_topics == ()
        assert (topic.dry_run.eligible, topic.dry_run.matched) == (10, 10)
        gate = ac.dry_run_gate(topic, now=NOW)
        assert not gate.ok and "100%" in gate.reason
        with pytest.raises(ac.ConfigError):
            _switch_on(again, tid)

    @pytest.mark.parametrize("eligible, matched", [(20, 5), (20, 0), (4, 4), (0, 0)])
    def test_a_passing_re_test_keeps_it_on(self, eligible, matched):
        cfg = _one_on()
        tid = cfg.topics[0].id
        assert _tested(cfg, tid, eligible=eligible, matched=matched).topic(tid).enabled

    def test_a_test_of_older_wording_never_replaces_the_current_one(self):
        cfg = _with_topics(_topic())
        tid = cfg.topics[0].id
        old_hash = cfg.topic(tid).match_hash
        # Reworded while a test of the old wording ran; the new wording was
        # tested and switched on before that old test came back.
        edited = _patch(cfg, {"topics": [{**_view_topic(cfg.topics[0]),
                                          "about": GOOD_ABOUT + " Also parking."}]}).config
        on = _switch_on(_tested(edited, tid), tid).config
        late = ac.record_dry_run(on, tid, match_hash=old_hash, eligible=10, matched=10,
                                 now=NOW)
        assert late == on and late.topic(tid).enabled

    async def test_the_loader_reads_a_too_broad_topic_that_is_on_as_off(self, db):
        record = _one_on().to_json()
        record["topics"][0]["dry_run"].update(eligible=10, matched=10, rate=1.0)
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert (await ac.load_autoreply_config(db, ALICE)).enabled_topics == ()
        record["topics"][0]["dry_run"].update(eligible=4, matched=4, rate=1.0)  # too little mail
        await db.set_settings(ALICE, {ac.AUTOREPLY_KEY: json.dumps(record)})
        assert len((await ac.load_autoreply_config(db, ALICE)).enabled_topics) == 1

    async def test_save_answers_what_the_loader_reads(self, db):
        cfg = _one_on()
        tid = cfg.topics[0].id
        broad = replace(cfg, topics=(replace(cfg.topic(tid), dry_run=replace(
            cfg.topic(tid).dry_run, eligible=10, matched=10, rate=1.0)),))
        stored = await ac.save_autoreply_config(db, ALICE, broad, now=NOW)
        assert stored.enabled_topics == () == (
            await ac.load_autoreply_config(db, ALICE)).enabled_topics


# ── the key is server-owned ──────────────────────────────────────────


class TestServerOwnedKey:
    def test_prefix(self):
        assert ac.AUTOREPLY_KEY == "google_oauth:gmail_autoreply"
        assert settings_routes._server_owned(ac.AUTOREPLY_KEY)

    async def test_settings_routes_hide_and_refuse_it(self, db):
        cfg = await ac.save_autoreply_config(db, ALICE, _one_on())
        stored = await db.get_setting(ALICE, ac.AUTOREPLY_KEY)
        app = FastAPI()
        app.include_router(settings_routes.router)
        c = TestClient(app, client=("127.0.0.1", 50123))
        hdr = {"Authorization": f"Bearer {auth.create_access_token(ALICE)}"}

        r = c.get("/fd/settings", headers=hdr)
        assert r.status_code == 200 and ac.AUTOREPLY_KEY not in r.json()
        on = json.dumps({**cfg.to_json(), "enabled": True, "google_sub": "forged"})
        r = c.put("/fd/settings", headers=hdr, json={"settings": {ac.AUTOREPLY_KEY: on}})
        assert r.status_code == 400
        r = c.delete(f"/fd/settings/{ac.AUTOREPLY_KEY}", headers=hdr)
        assert r.status_code == 400
        assert await db.get_setting(ALICE, ac.AUTOREPLY_KEY) == stored
        assert (await ac.load_autoreply_config(db, ALICE)).enabled is False
        # Bob can't plant one for himself either.
        bob = {"Authorization": f"Bearer {auth.create_access_token(BOB)}"}
        r = c.put("/fd/settings", headers=bob, json={"settings": {ac.AUTOREPLY_KEY: on}})
        assert r.status_code == 400
        assert await db.get_setting(BOB, ac.AUTOREPLY_KEY) is None



def test_a_retest_of_the_same_wording_keeps_when_it_was_first_tested():
    """The drift breaker samples from then: re-testing can't wipe it."""
    cfg = _with_topics(_topic())
    t = cfg.topics[0]
    first = datetime(2026, 10, 1, tzinfo=timezone.utc)
    cfg = ac.record_dry_run(cfg, t.id, match_hash=t.match_hash, eligible=30, matched=2,
                            now=first)
    cfg = ac.record_dry_run(cfg, t.id, match_hash=t.match_hash, eligible=30, matched=2,
                            now=first + timedelta(days=5))
    dry = cfg.topic(t.id).dry_run
    assert dry.at.startswith("2026-10-06") and dry.since.startswith("2026-10-01")
