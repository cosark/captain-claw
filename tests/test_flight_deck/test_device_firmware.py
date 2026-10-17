"""Over-the-air firmware store, routes and offers (device_firmware.py)."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import device_bridge as db
from captain_claw.flight_deck import device_firmware as fw
from captain_claw.flight_deck.auth import get_mcp_user

SIG = base64.b64encode(b"\x30\x45" + b"\x01" * 69).decode()


def _blob(tag: bytes = b"a", size: int = 4096) -> bytes:
    return b"\xe9" + (tag * size)[: size - 1]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "firmware_root", lambda: tmp_path / "device_firmware")
    return tmp_path / "device_firmware"


def test_versions_order_numerically():
    assert fw.version_key("0.10.0") > fw.version_key("0.9.3")
    assert fw.version_key("1.0") > fw.version_key("0.99.99")
    assert fw.version_key("0.8.0") == fw.version_key("0.8.0")


def test_offer_only_when_newer_and_same_kind():
    b = _blob()
    fw.store("sticks3", "0.8.1", b, _sha(b), SIG, "faster boot", "admin@x")
    offer = fw.offer_for("sticks3", "0.8.0")
    assert offer == {
        "type": "update", "version": "0.8.1", "size": len(b), "sha256": _sha(b), "sig": SIG,
        "notes": "faster boot", "url": "/device/firmware/sticks3/0.8.1.bin",
    }
    assert fw.offer_for("sticks3", "0.8.1") is None   # same
    assert fw.offer_for("sticks3", "0.9.0") is None   # newer than published
    assert fw.offer_for("stopwatch", "0.1.0") is None  # other kind
    assert fw.offer_for("sticks3", "") is None        # old firmware doesn't report fw


@pytest.mark.parametrize("hw, version, blob, sha, sig, why", [
    ("Sticks3!", "0.8.1", _blob(), None, SIG, "hw"),
    ("sticks3", "v0.8", _blob(), None, SIG, "version"),
    ("sticks3", "0.8.1", b"\x00" * 100, None, SIG, "ESP32"),
    ("sticks3", "0.8.1", _blob(), "0" * 64, SIG, "sha256"),
    ("sticks3", "0.8.1", _blob(), None, "not base64!", "sig"),
])
def test_store_refuses_bad_builds(hw, version, blob, sha, sig, why):
    with pytest.raises(ValueError, match=why):
        fw.store(hw, version, blob, sha or _sha(blob), sig, "", "admin")


def test_republishing_a_version_needs_the_same_bytes(_root):
    a, b = _blob(b"a"), _blob(b"b")
    fw.store("sticks3", "0.8.1", a, _sha(a), SIG, "", "admin")
    fw.store("sticks3", "0.8.1", a, _sha(a), SIG, "again", "admin")  # same bytes: fine
    with pytest.raises(ValueError, match="bump FW_VERSION"):
        fw.store("sticks3", "0.8.1", b, _sha(b), SIG, "", "admin")


def test_keeps_the_newest_few(_root):
    for v in ["0.8.1", "0.8.2", "0.10.0", "0.9.0"]:
        b = _blob(v.encode())
        fw.store("sticks3", v, b, _sha(b), SIG, "", "admin")
    kept = [b["version"] for b in json.loads((_root / "sticks3/manifest.json").read_text())["builds"]]
    assert kept == ["0.8.2", "0.9.0", "0.10.0"]
    assert not (_root / "sticks3/0.8.1.bin").exists()
    assert fw.latest("sticks3")["version"] == "0.10.0"


def _app(user: dict) -> TestClient:
    app = FastAPI()
    app.include_router(fw.router)
    app.dependency_overrides[get_mcp_user] = lambda: user
    return TestClient(app)


def _publish(client: TestClient, version="0.8.1", blob=None):
    blob = blob or _blob()
    return client.post("/fd/device/firmware", data={
        "hw": "sticks3", "version": version, "sha256": _sha(blob), "sig": SIG, "notes": "n"},
        files={"file": ("firmware.bin", blob, "application/octet-stream")})


def test_only_admins_publish():
    assert _publish(_app({"id": "u1", "role": "user"})).status_code == 403
    r = _publish(_app({"id": "a1", "role": "admin", "email": "a@x"}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "0.8.1" and "sig" not in body and body["offered_to"] == 0
    listed = _app({"id": "a1", "role": "admin"}).get("/fd/device/firmware").json()
    assert [b["version"] for b in listed["sticks3"]] == ["0.8.1"]


def test_bad_upload_is_a_400():
    r = _publish(_app({"id": "a1", "role": "admin"}), version="latest")
    assert r.status_code == 400 and "version" in r.json()["detail"]


def test_download_needs_a_device_token(monkeypatch):
    blob = _blob()
    fw.store("sticks3", "0.8.1", blob, _sha(blob), SIG, "", "admin")
    client = _app({"id": "a1", "role": "admin"})

    async def _user(token):
        return {"id": "u1"} if token == "cc_pat_good" else None

    monkeypatch.setattr(db, "_device_user", _user)
    assert client.get("/device/firmware/sticks3/0.8.1.bin").status_code == 401
    r = client.get("/device/firmware/sticks3/0.8.1.bin", headers={"Authorization": "Bearer cc_pat_good"})
    assert r.status_code == 200 and r.content == blob
    assert client.get("/device/firmware/sticks3/0.9.9.bin",
                      headers={"Authorization": "Bearer cc_pat_good"}).status_code == 404
    assert client.get("/device/firmware/sticks3/..%2Fmanifest.json",
                      headers={"Authorization": "Bearer cc_pat_good"}).status_code == 404


class _WS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def _conn(hw="sticks3", fw_version="0.8.0"):
    return db._DeviceConn(_WS(), {"id": "u1"}, "stick", lang="", speak=False, screen=(240, 135),
                          hw=hw, fw=fw_version)


@pytest.mark.asyncio
async def test_device_gets_the_offer_or_update_none():
    conn = _conn()
    assert await conn.offer_update(reply_none=True) is False
    assert conn.ws.sent == [{"type": "update_none"}]
    b = _blob()
    fw.store("sticks3", "0.8.1", b, _sha(b), SIG, "", "admin")
    assert await conn.offer_update() is True
    assert conn.ws.sent[-1]["type"] == "update" and conn.ws.sent[-1]["version"] == "0.8.1"


@pytest.mark.asyncio
async def test_publishing_offers_to_connected_devices_of_that_kind():
    stick, watch, old = _conn(), _conn(hw="stopwatch"), _conn(fw_version="0.9.0")
    db._live.update({stick, watch, old})
    try:
        b = _blob()
        fw.store("sticks3", "0.8.1", b, _sha(b), SIG, "", "admin")
        assert await db.offer_to_connected("sticks3") == 1
        assert stick.ws.sent and not watch.ws.sent and not old.ws.sent
    finally:
        db._live.difference_update({stick, watch, old})
