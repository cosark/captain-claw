"""The deck's branding: the instance name, a short description and a logo.

Name and description live in the system config (Admin → Settings, ``PUT
/fd/admin/config``) and reach the sign-in page through ``/fd/auth/status``.
The logo is a system setting of its own (an image an admin uploads), served
publicly so the sign-in page can show it before anyone is signed in.

* ``GET    /fd/branding/logo``        — public: the logo image (404 when none).
* ``PUT    /fd/admin/branding/logo``  — admin: upload one, as a ``data:`` URL.
* ``DELETE /fd/admin/branding/logo``  — admin: back to no logo.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from captain_claw.flight_deck.admin_routes import require_admin
from captain_claw.flight_deck.auth import get_db

router = APIRouter(tags=["branding"])

LOGO_SETTING = "fd:instance-logo"
LOGO_MAX_BYTES = 512 * 1024
LOGO_PATH = "/fd/branding/logo"

# The image itself, never a page: an SVG opened on its own must not run script
# on the deck's origin (an <img> never runs it anyway).
_LOGO_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}


def sniff_image(data: bytes) -> str | None:
    """The image type *data* really is (whatever the upload claimed), or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    try:
        head = data[:4096].decode("utf-8").lstrip("﻿").lstrip().lower()
    except UnicodeDecodeError:
        return None
    if head.startswith("<") and "<svg" in head and "<html" not in head and "doctype html" not in head:
        return "image/svg+xml"
    return None


async def load_logo(db) -> dict | None:
    """``{"mime", "data": bytes, "version"}`` or None."""
    raw = await db.get_system_setting(LOGO_SETTING)
    try:
        blob = json.loads(raw) if raw else None
        if not isinstance(blob, dict) or not blob.get("data"):
            return None
        return {"mime": str(blob.get("mime") or ""),
                "data": base64.b64decode(blob["data"]),
                "version": str(blob.get("version") or "")}
    except (TypeError, ValueError, binascii.Error):
        return None


async def logo_url(db) -> str:
    """Where the sign-in page and the panels load the logo from ('' = none).
    Versioned, so a new upload isn't hidden behind the cached old one."""
    logo = await load_logo(db)
    return f"{LOGO_PATH}?v={logo['version']}" if logo else ""


@router.get(LOGO_PATH)
async def get_logo():
    logo = await load_logo(get_db())
    if not logo:
        raise HTTPException(status_code=404, detail="No logo")
    return Response(content=logo["data"], media_type=logo["mime"], headers=_LOGO_HEADERS)


class LogoUpload(BaseModel):
    data_url: str


@router.put("/fd/admin/branding/logo")
async def upload_logo(body: LogoUpload, _admin: dict = Depends(require_admin)):
    head, sep, payload = body.data_url[: LOGO_MAX_BYTES * 2].partition(",")
    if not sep or not head.startswith("data:") or not head.endswith(";base64"):
        raise HTTPException(status_code=400, detail="Upload the logo as an image file")
    if len(body.data_url) > LOGO_MAX_BYTES * 2:
        raise HTTPException(status_code=413, detail=f"The logo is at most {LOGO_MAX_BYTES // 1024} KB")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="That file couldn't be read") from None
    if len(data) > LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"The logo is at most {LOGO_MAX_BYTES // 1024} KB")
    mime = sniff_image(data)
    if mime is None:
        raise HTTPException(status_code=400, detail="Use a PNG, JPEG, WebP, GIF or SVG image")
    version = hashlib.sha256(data).hexdigest()[:12]
    db = get_db()
    await db.set_system_setting(LOGO_SETTING, json.dumps({
        "mime": mime, "data": base64.b64encode(data).decode("ascii"), "version": version}))
    return {"ok": True, "logo": f"{LOGO_PATH}?v={version}"}


@router.delete("/fd/admin/branding/logo")
async def delete_logo(_admin: dict = Depends(require_admin)):
    await get_db().set_system_setting(LOGO_SETTING, "")
    return {"ok": True, "logo": ""}
