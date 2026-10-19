"""Encryption at rest for what Flight Deck keeps of other people's words.

The Telegram inbox (telegram_inbox.py) stores message text and names. Telegram's
bot developer terms ask for that data "encrypted at rest and stored separately
from its encryption key", so those columns hold Fernet tokens (AES-128-CBC +
HMAC-SHA256, from the ``cryptography`` package) and the key lives in its own
file, never in the database:

* ``FD_DATA_KEY_FILE`` — a path ops choose (another disk, a mounted secret), or
* ``<FD home>/data_key`` — beside ``agent_secret`` (see :mod:`fd_home`), mode 0600,
  generated on first use.

Losing the key makes the stored text unreadable (rows open as ``None``); nothing
else depends on it. Without ``cryptography`` installed, :func:`available` is
False and the inbox refuses to switch on rather than store plain text.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

from captain_claw.logging import get_logger

log = get_logger(__name__)

_KEY_FILE_NAME = "data_key"
_lock = Lock()
_fernet: Any = None


class CryptoUnavailable(RuntimeError):
    """Encryption can't be used here (no ``cryptography`` package, or no key)."""


def available() -> bool:
    try:
        import cryptography.fernet  # noqa: F401
    except ImportError:
        return False
    return True


def key_path() -> Path:
    custom = (os.environ.get("FD_DATA_KEY_FILE") or "").strip()
    if custom:
        return Path(custom).expanduser()
    from captain_claw.flight_deck.agent_secret import _real_user_home
    from captain_claw.flight_deck.fd_home import fd_home

    return fd_home(_real_user_home() / ".captain-claw-fd") / _KEY_FILE_NAME


def _load_or_create_key() -> bytes:
    from cryptography.fernet import Fernet

    path = key_path()
    try:
        key = path.read_bytes().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    log.info("Created the Flight Deck data key", path=str(path))
    return key


def _get() -> Any:
    global _fernet
    with _lock:
        if _fernet is None:
            if not available():
                raise CryptoUnavailable(
                    "the 'cryptography' package isn't installed on this Flight Deck "
                    "(pip install cryptography, then restart it)")
            from cryptography.fernet import Fernet

            try:
                _fernet = Fernet(_load_or_create_key())
            except (OSError, ValueError) as exc:
                raise CryptoUnavailable(f"the data key can't be used ({type(exc).__name__})") from None
        return _fernet


def seal(value: Any) -> str:
    """*value* (anything JSON can hold) as an encrypted token."""
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _get().encrypt(raw).decode("ascii")


def unseal(token: str) -> Any:
    """What :func:`seal` stored; ``None`` for "" or a token this key can't open."""
    if not token:
        return None
    try:
        raw = _get().decrypt(token.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except CryptoUnavailable:
        raise
    except Exception:
        return None


def reset_for_tests() -> None:
    global _fernet
    with _lock:
        _fernet = None
