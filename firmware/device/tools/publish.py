"""Publish a firmware build to Flight Deck for over-the-air updates.

    uv run --no-project --with cryptography python tools/publish.py keygen
    uv run --no-project --with cryptography python tools/publish.py sticks3 \\
        --deck https://glasses.captain-claw.com [--token cc_pat_…] [--notes "…"] [--no-build]

keygen   makes the signing key pair once: the private key stays on this Mac
         (~/.captain-claw/firmware-signing/ota-key.pem, mode 600); the public
         key goes into src/ota_pubkey.h, compiled into every build.
<env>    builds that PlatformIO env (sticks3 / stopwatch), reads FW_VERSION
         from src/main.cpp, signs the SHA-256 of firmware.bin (ECDSA P-256)
         and uploads it to the deck (admin token: a cc_pat_… or an admin JWT).
         Devices of that kind are offered it on their next connect.

See docs/device-ota-plan.md.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = Path(os.environ.get("CLAW_OTA_KEY", "~/.captain-claw/firmware-signing/ota-key.pem")).expanduser()
# The admin token can live in a file (mode 600) instead of the command line.
TOKEN_FILE = KEY.parent / "deck-token"
PUBKEY_H = ROOT / "src/ota_pubkey.h"


def keygen() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if KEY.exists():
        sys.exit(f"{KEY} already exists — refusing to replace the signing key (devices trust it).")
    key = ec.generate_private_key(ec.SECP256R1())
    KEY.parent.mkdir(parents=True, exist_ok=True)
    KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    KEY.chmod(0o600)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    lines = "\n".join(f'    "{line}\\n"' for line in pem.strip().splitlines())
    PUBKEY_H.write_text(
        "// Public half of the firmware signing key (tools/publish.py keygen). The\n"
        "// private half stays on the publishing Mac; a build signed with anything else\n"
        "// is refused by the over-the-air updater.\n"
        "#pragma once\n\n"
        f"static const char kOtaPublicKey[] =\n{lines};\n")
    print(f"private key: {KEY} (keep it safe; back it up)")
    print(f"public key:  {PUBKEY_H} (commit it)")


def fw_version() -> str:
    m = re.search(r'#define\s+FW_VERSION\s+"([^"]+)"', (ROOT / "src/main.cpp").read_text())
    if not m:
        sys.exit("FW_VERSION not found in src/main.cpp")
    return m.group(1)


def build(env: str) -> Path:
    pio = os.environ.get("PIO", "pio")
    subprocess.run([pio, "run", "-e", env], cwd=ROOT, check=True)
    return ROOT / ".pio/build" / env / "firmware.bin"


def sign(blob: bytes) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if not KEY.exists():
        sys.exit(f"no signing key at {KEY} — run `publish.py keygen` first")
    key = serialization.load_pem_private_key(KEY.read_bytes(), password=None)
    return base64.b64encode(key.sign(blob, ec.ECDSA(hashes.SHA256()))).decode()


def upload(deck: str, token: str, fields: dict[str, str], blob: bytes) -> dict:
    boundary = uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="firmware.bin"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode()
    body += blob + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        deck.rstrip("/") + "/fd/device/firmware", data=bytes(body), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"upload refused: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as exc:
        # Flight Deck answers a bad token before reading the upload, so the
        # client sees the connection closed mid-send.
        sys.exit(f"upload failed ({exc.reason}) — usually a rejected token, or the deck is unreachable")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="keygen, or a PlatformIO env to publish (sticks3, stopwatch)")
    ap.add_argument("--deck", help="Flight Deck URL, e.g. https://glasses.captain-claw.com")
    ap.add_argument("--token", default=os.environ.get("FD_TOKEN", ""),
                    help=f"admin token (or FD_TOKEN, or the file {TOKEN_FILE})")
    ap.add_argument("--notes", default="", help="release notes shown on the device")
    ap.add_argument("--no-build", action="store_true", help="publish the last build as is")
    a = ap.parse_args()
    if a.target == "keygen":
        return keygen()
    if not a.token and TOKEN_FILE.exists():
        a.token = TOKEN_FILE.read_text().strip()
    if not a.deck or not a.token:
        sys.exit(f"--deck and a token (--token, FD_TOKEN or {TOKEN_FILE}) are required")
    if any(c.isspace() for c in a.token) or not (a.token.startswith("cc_pat_") or a.token.count(".") == 2):
        sys.exit("the token doesn't look like a Flight Deck token (cc_pat_… or a JWT) — "
                 f"check {TOKEN_FILE if not os.environ.get('FD_TOKEN') else 'FD_TOKEN'}")
    path = ROOT / ".pio/build" / a.target / "firmware.bin" if a.no_build else build(a.target)
    blob = path.read_bytes()
    version = fw_version()
    digest = hashlib.sha256(blob).hexdigest()
    fields = {"hw": a.target, "version": version, "sha256": digest, "sig": sign(blob), "notes": a.notes}
    print(f"{a.target} {version}: {len(blob) // 1024} KB, sha256 {digest[:16]}…")
    result = upload(a.deck, a.token, fields, blob)
    print(f"published: {json.dumps(result)}")


if __name__ == "__main__":
    main()
