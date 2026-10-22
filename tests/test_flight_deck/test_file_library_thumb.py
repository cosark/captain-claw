"""The File library's PDF thumbnail — ``GET /fd/file-library/files/{id}/thumb``.

The library's preview shows a PDF's first page as a picture Flight Deck
renders (with pypdfium2, the payments extra), because browsers show PDFs
inline only with a viewer plugin the desktop app and phones lack. Only the
file's owner gets it; anything but a PDF, a missing renderer or a damaged
file is refused, and the preview then offers Open instead.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import httpx
import pytest

from captain_claw.flight_deck import auth as fd_auth
from captain_claw.flight_deck import file_library as fl
from captain_claw.flight_deck import rate_limiter, server
from captain_claw.flight_deck.auth import create_access_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

pdfium = pytest.importorskip("pypdfium2")

BOB = "user-bob"
CAROL = "user-carol"


def _pdf(path: Path) -> None:
    """A one-page PDF, written with pdfium itself."""
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 300)
    with open(path, "wb") as f:
        doc.save(f)
    doc.close()


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "fd.db")
    await db.init()
    set_auth_db(db)
    for uid in (BOB, CAROL):
        await db._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
            " VALUES (?, ?, 'h', ?, 'user', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (uid, f"{uid}@x.co", uid))
    await db._db.commit()

    data = tmp_path / "fd-data"
    data.mkdir()
    monkeypatch.setattr(server, "DATA_DIR", data)
    monkeypatch.setattr(server, "PROCESS_REGISTRY_FILE", data / ".processes.json")
    monkeypatch.setattr(server, "_processes", {})
    monkeypatch.setattr(server, "AUTH_ENABLED", True)
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("FD_ALLOWED_HOSTS", "fd.test")
    monkeypatch.delenv("FD_LOCKDOWN", raising=False)
    monkeypatch.setattr(rate_limiter, "_api_limiter", rate_limiter._SlidingWindow())
    monkeypatch.setattr(fl, "_STORE", fl.LibraryStore(tmp_path / "file_library.db"))
    monkeypatch.setattr(fl, "_thumbs", {})
    monkeypatch.setattr(fl, "_thumb_failed", {})
    monkeypatch.setattr(fl, "_thumb_inflight", {})

    server._save_process_registry({
        "bob-agent": {"slug": "bob-agent", "name": "Bob's agent", "web_port": 24101,
                      "web_auth": "bob-tok", "owner": BOB, "pid": None},
    })
    ws = data / "bob-agent" / "data" / "workspace" / "saved"
    ws.mkdir(parents=True)
    _pdf(ws / "brief.pdf")
    (ws / "broken.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf\n")
    (ws / "notes.md").write_text("# Notes\n")
    fl.get_store().sync_agent("bob-agent", fl.walk_workspace("bob-agent"))
    try:
        yield data
    finally:
        await db.close()
        fd_auth._db = prev


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, client=("203.0.113.9", 40001)), base_url="http://fd.test")


def _auth(uid: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _ids(c: httpx.AsyncClient) -> dict[str, str]:
    r = await c.get("/fd/file-library/files?limit=50", headers=_auth(BOB))
    assert r.status_code == 200
    return {i["filename"]: i["id"] for i in r.json()["items"]}


async def test_first_page_comes_back_as_a_png(deck: Path):
    async with _client() as c:
        ids = await _ids(c)
        r = await c.get(f"/fd/file-library/files/{ids['brief.pdf']}/thumb", headers=_auth(BOB))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n")


async def test_only_the_owner_gets_it(deck: Path):
    async with _client() as c:
        ids = await _ids(c)
        r = await c.get(f"/fd/file-library/files/{ids['brief.pdf']}/thumb", headers=_auth(CAROL))
    assert r.status_code == 404


async def test_not_a_pdf_damaged_or_no_renderer_is_refused(deck: Path, monkeypatch: pytest.MonkeyPatch):
    async with _client() as c:
        ids = await _ids(c)
        assert (await c.get(f"/fd/file-library/files/{ids['notes.md']}/thumb", headers=_auth(BOB))).status_code == 404
        r = await c.get(f"/fd/file-library/files/{ids['broken.pdf']}/thumb", headers=_auth(BOB))
        assert r.status_code == 422
        assert "Couldn't read the PDF" in r.json()["detail"]

        real_import = builtins.__import__

        def no_pdfium(name, *args, **kwargs):
            if name == "pypdfium2":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pdfium)
        monkeypatch.setattr(fl, "_thumbs", {})
        monkeypatch.setattr(fl, "_thumb_failed", {})
        r = await c.get(f"/fd/file-library/files/{ids['brief.pdf']}/thumb", headers=_auth(BOB))
    assert r.status_code == 404


async def test_a_slow_pdf_gives_up_and_is_not_drawn_again(deck: Path, monkeypatch: pytest.MonkeyPatch):
    """A page built to take ages to draw: the preview gives up, and asking
    again is refused at once instead of queueing another render."""
    import threading
    import time

    calls = []
    release = threading.Event()

    def slow(path):
        calls.append(path)
        release.wait(5)
        return None

    monkeypatch.setattr(fl, "_pdf_first_page_png", slow)
    monkeypatch.setattr(fl, "_THUMB_TIMEOUT_S", 0.2)
    try:
        async with _client() as c:
            ids = await _ids(c)
            url = f"/fd/file-library/files/{ids['brief.pdf']}/thumb"
            t = time.monotonic()
            r = await c.get(url, headers=_auth(BOB))
            assert r.status_code == 422 and "too long" in r.json()["detail"]
            assert time.monotonic() - t < 3
            r = await c.get(url, headers=_auth(BOB))
            assert r.status_code == 422
    finally:
        release.set()
    assert len(calls) == 1
