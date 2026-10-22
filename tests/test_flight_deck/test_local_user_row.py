"""The local user's row on a deck without sign-in — ``FlightDeckDB.ensure_local_user``.

With FD_AUTH_ENABLED=false (the desktop app) every request runs as the
synthetic local user, but the per-user tables reference ``users(id)`` with
foreign keys on, so saving its settings or archetypes failed until it had a
row. The row must never become a way in once sign-in is switched on: it is a
plain user with a random password.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from captain_claw.flight_deck.auth import _LOCAL_USER, hash_password, verify_password
from captain_claw.flight_deck.db import FlightDeckDB

LOCAL = _LOCAL_USER["id"]


@pytest.fixture
async def db(tmp_path: Path):
    d = FlightDeckDB(tmp_path / "fd.db")
    await d.init()
    try:
        yield d
    finally:
        await d.close()


async def test_without_the_row_the_local_user_cannot_save(db):
    with pytest.raises(Exception, match="FOREIGN KEY"):
        await db.set_settings(LOCAL, {"fd:forge-tiers": "{}"})


async def test_row_lets_settings_and_archetypes_be_saved(db):
    await db.ensure_local_user(LOCAL, hash_password(secrets.token_urlsafe(32)))
    blob = json.dumps({"sets": [{"id": "s1", "name": "Mine", "tiers": {}}], "activeSetId": "s1"})
    await db.set_settings(LOCAL, {"fd:forge-tiers": blob})
    assert (await db.get_all_settings(LOCAL))["fd:forge-tiers"] == blob
    await db.create_user_archetype(LOCAL, "helper", json.dumps({"role": "Helper"}))


async def test_row_is_idempotent_and_never_an_admin(db):
    await db.ensure_local_user(LOCAL, hash_password("first"))
    await db.ensure_local_user(LOCAL, hash_password("second"))
    user = await db.get_user_by_id(LOCAL)
    assert user is not None
    assert user["role"] == "user"
    assert json.loads(user["metadata"]) == {"local": True}
    row = await db.get_user_by_email(LOCAL)
    # The first password stays (a second start doesn't touch the row), and
    # nothing guessable opens it.
    assert verify_password("first", row["password_hash"])
    assert not verify_password("", row["password_hash"])
