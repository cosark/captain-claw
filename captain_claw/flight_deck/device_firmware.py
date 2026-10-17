"""Firmware for over-the-air device updates (docs/device-ota-plan.md).

An admin publishes a signed build per device kind (``sticks3``,
``stopwatch`` — the PlatformIO env) with ``firmware/device/tools/publish.py``;
it lands in ``<fd_home>/device_firmware/<hw>/<version>.bin`` beside a
``manifest.json``. A device reports ``hw`` and ``fw`` on ``/device/ws`` and is
offered the newest build of its kind when that's newer than what it runs; it
downloads the file with its device token and checks the SHA-256 and the
signature itself (the private key never touches Flight Deck).

    POST /fd/device/firmware                 admin: publish (multipart)
    GET  /fd/device/firmware                 admin: what's published
    GET  /device/firmware/{hw}/{version}.bin device token: the build
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from captain_claw.flight_deck.auth import get_mcp_user
from captain_claw.flight_deck.fd_home import fd_home

router = APIRouter()

MAX_SIZE = 8 * 1024 * 1024   # bigger than any app slot the devices have
KEEP = 3                     # builds kept per kind (newest)
_HW_RE = re.compile(r"[a-z0-9_-]{1,24}")
_VERSION_RE = re.compile(r"\d{1,4}(\.\d{1,4}){0,3}")


def firmware_root() -> Path:
    return fd_home(Path(os.path.expanduser("~/.captain-claw-fd"))) / "device_firmware"


def version_key(version: str) -> tuple[int, ...]:
    """Numeric ordering: "0.10.0" sorts after "0.9.3"."""
    return tuple(int(x) for x in re.findall(r"\d+", str(version or ""))) or (0,)


def _manifest_path(hw: str) -> Path:
    return firmware_root() / hw / "manifest.json"


def _builds(hw: str) -> list[dict]:
    try:
        data = json.loads(_manifest_path(hw).read_text())
        return [b for b in data.get("builds", []) if isinstance(b, dict) and b.get("version")]
    except (OSError, ValueError):
        return []


def latest(hw: str) -> dict | None:
    """The newest published build of a kind (whose file is there), or None."""
    if not _HW_RE.fullmatch(str(hw or "")):
        return None
    for build in sorted(_builds(hw), key=lambda b: version_key(b["version"]), reverse=True):
        if (firmware_root() / hw / f"{build['version']}.bin").is_file():
            return build
    return None


def offer_for(hw: str, fw: str) -> dict | None:
    """The update message for a device running ``fw``, when there's a newer build."""
    build = latest(hw)
    if not build or not fw or version_key(build["version"]) <= version_key(fw):
        return None
    return {
        "type": "update",
        "version": build["version"],
        "size": build["size"],
        "sha256": build["sha256"],
        "sig": build["sig"],
        "notes": build.get("notes", ""),
        "url": f"/device/firmware/{hw}/{build['version']}.bin",
    }


def store(hw: str, version: str, blob: bytes, sha256: str, sig: str, notes: str, by: str) -> dict:
    """Keep a build; raises ValueError (with a reason) for anything off."""
    if not _HW_RE.fullmatch(hw or ""):
        raise ValueError("hw must be a device kind like sticks3 or stopwatch")
    if not _VERSION_RE.fullmatch(version or ""):
        raise ValueError("version must look like 0.8.1")
    if not blob or len(blob) > MAX_SIZE:
        raise ValueError(f"firmware must be 1 byte to {MAX_SIZE // (1024 * 1024)} MB")
    if blob[0] != 0xE9:  # every ESP32 app image starts with this magic byte
        raise ValueError("not an ESP32 app image (firmware.bin)")
    digest = hashlib.sha256(blob).hexdigest()
    if (sha256 or "").lower() != digest:
        raise ValueError("sha256 doesn't match the file")
    try:
        if not base64.b64decode(sig or "", validate=True):
            raise ValueError
    except (binascii.Error, ValueError):
        raise ValueError("sig must be the base64 signature from publish.py") from None
    builds = [b for b in _builds(hw) if b["version"] != version]
    same = next((b for b in _builds(hw) if b["version"] == version), None)
    if same and same.get("sha256") != digest:
        raise ValueError(f"{version} is already published with different contents — bump FW_VERSION")
    folder = firmware_root() / hw
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{version}.bin.tmp"
    tmp.write_bytes(blob)
    tmp.replace(folder / f"{version}.bin")
    entry = {
        "version": version, "size": len(blob), "sha256": digest, "sig": sig,
        "notes": (notes or "")[:200], "uploaded_at": datetime.now(UTC).isoformat(), "uploaded_by": by,
    }
    builds.append(entry)
    builds.sort(key=lambda b: version_key(b["version"]))
    for old in builds[:-KEEP]:
        (folder / f"{old['version']}.bin").unlink(missing_ok=True)
    builds = builds[-KEEP:]
    tmp = folder / ".manifest.json.tmp"
    tmp.write_text(json.dumps({"hw": hw, "builds": builds}, indent=2))
    tmp.replace(_manifest_path(hw))
    return entry


def _require_admin(user: dict) -> None:
    if str(user.get("role", "")) != "admin":
        raise HTTPException(status_code=403, detail="only an admin can publish firmware")


@router.post("/fd/device/firmware")
async def publish_firmware(request: Request, user: dict = Depends(get_mcp_user)) -> dict:
    _require_admin(user)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="file is required")
    blob = await upload.read(MAX_SIZE + 1)
    hw = str(form.get("hw", "")).strip()
    try:
        entry = store(hw, str(form.get("version", "")).strip(), blob, str(form.get("sha256", "")).strip(),
                      str(form.get("sig", "")).strip(), str(form.get("notes", "")),
                      str(user.get("email") or user.get("id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from captain_claw.flight_deck.device_bridge import offer_to_connected

    offered = await offer_to_connected(hw)
    public = {k: v for k, v in entry.items() if k != "sig"}
    return {"ok": True, "hw": hw, **public, "offered_to": offered}


@router.get("/fd/device/firmware")
async def list_firmware(user: dict = Depends(get_mcp_user)) -> dict:
    _require_admin(user)
    root = firmware_root()
    kinds = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    return {hw: [{k: v for k, v in b.items() if k != "sig"} for b in _builds(hw)] for hw in kinds}


@router.get("/device/firmware/{hw}/{filename}")
async def download_firmware(hw: str, filename: str, request: Request) -> FileResponse:
    from captain_claw.flight_deck.device_bridge import _device_user

    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else request.query_params.get("token", "")
    if await _device_user(token) is None:
        raise HTTPException(status_code=401, detail="device token required")
    version = filename[:-4] if filename.endswith(".bin") else ""
    if not _HW_RE.fullmatch(hw) or not _VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=404, detail="no such firmware")
    path = firmware_root() / hw / f"{version}.bin"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such firmware")
    return FileResponse(path, media_type="application/octet-stream", filename=f"{hw}-{version}.bin")
