"""``GET /fd/info`` — what the kiosk Info dialog shows a signed-in user.

The tier set their agents run on (their own, else the team default) with
provider, model, endpoint host and context sizes but never a key; their
archetypes; the profile their agents receive; their plan; their connections.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from captain_claw.flight_deck import info_routes
from captain_claw.flight_deck import tenant_profile as tp
from captain_claw.flight_deck.auth import get_current_user, hash_password, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

LOOPBACK = ("127.0.0.1", 40001)
SECRET = "sk-very-secret-tier-key"


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
    user = await db.create_user("member@example.com", hash_password("pw-pw-pw"), "Mia Member", "user",
                                metadata={"kiosk": True, "plan": "pro"})
    try:
        yield db, await db.get_user_by_id(user["id"])
    finally:
        await db.close()
        fd_auth._db = prev


def _app(user: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(info_routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def _set(name: str, model: str, **extra) -> dict:
    return {"id": name.lower().replace(" ", "-"), "name": name, "envVars": [], "forgeTier": "balanced",
            "tiers": {"balanced": {"provider": "openai", "model": model, "api_key": SECRET,
                                   "base_url": "https://user:pw@gw.example.com:8443/v1?token=x",
                                   "input_ctx": 400000, "output_ctx": 64000, **extra}}}


async def _info(user) -> dict:
    async with _client(_app(user)) as c:
        r = await c.get("/fd/info")
    assert r.status_code == 200, r.text
    assert SECRET not in r.text and "user:pw" not in r.text and "token=x" not in r.text
    return r.json()


async def test_team_default_then_own_set(fd_db):
    db, user = fd_db
    await db.set_system_setting("fd:shared-tier-sets", json.dumps(
        {"sets": [_set("Team models", "gpt-team")], "defaultSetId": "team-models"}))
    info = await _info(user)
    assert info["tiers"]["source"] == "team" and info["tiers"]["set_name"] == "Team models"
    balanced = next(t for t in info["tiers"]["tiers"] if t["id"] == "balanced")
    assert balanced == {**balanced, "label": "Balanced", "provider": "openai", "model": "gpt-team",
                        "endpoint": "gw.example.com:8443", "input_ctx": 400000, "output_ctx": 64000,
                        "from_set": True}
    # A tier the set leaves out falls back to the deck's built-in one.
    reason = next(t for t in info["tiers"]["tiers"] if t["id"] == "reason")
    assert reason["from_set"] is False and reason["model"]
    assert sum(t["archetypes"] for t in info["tiers"]["tiers"]) == info["archetypes"]["total"] > 0

    await db.set_settings(user["id"], {"fd:forge-tiers": json.dumps(
        {"sets": [_set("Mine", "gpt-mine", vision=True)], "activeSetId": "mine"})})
    info = await _info(user)
    assert info["tiers"]["source"] == "own" and info["tiers"]["set_name"] == "Mine"
    balanced = next(t for t in info["tiers"]["tiers"] if t["id"] == "balanced")
    assert balanced["model"] == "gpt-mine" and balanced["vision"] is True


async def test_profile_account_and_connections(fd_db):
    db, user = fd_db
    await tp.save_deck(db, {"company": "Cohort 001 of Captain Claw.", "instructions": "Drafts first."})
    await tp.save_profile(db, user["id"], {"about_me": "I run a bakery.", "company": "", "instructions": ""})
    info = await _info(user)

    profile = info["profile"]
    assert profile["deck"]["company"] == "Cohort 001 of Captain Claw."
    assert profile["own"]["about_me"] == "I run a bakery."
    assert "I run a bakery." in profile["preview"]["full"]
    assert "Drafts first." in profile["preview"]["full"] and profile["preview"]["compact"]

    account = info["account"]
    assert account["email"] == "member@example.com" and account["plan"] == "pro"
    assert account["kiosk"] is False  # kiosk mode is off on this deck, whatever the mark says
    assert account["limits"]["max_agents"] > 0

    conns = info["connections"]
    assert conns["google"] == {"configured": False, "connected": False}
    assert conns["telegram"]["status"] == "" and conns["jev"]["connected"] is False
