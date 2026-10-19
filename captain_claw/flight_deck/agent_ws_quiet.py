"""Keep Flight Deck's log readable while an agent's chat socket can't connect.

When Flight Deck restarts, its process agents restart with it, and every open
chat keeps knocking on ``/fd/agent-ws`` until its agent answers. Each knock used
to print three uvicorn lines. Now the proxy logs one line when an agent goes
down and one when it is back. This filter mutes the knocks in between.

The state lives here rather than in ``server.py``: that module can be loaded
twice (``python -m …server`` runs it as ``__main__``, and importers get a second
copy), and the filter must see the same set of down agents as the proxy.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs, unquote, urlsplit

_AGENT_WS_PATH = "/fd/agent-ws/"

# Proxy target → monotonic time of its first failed connect, while it stays down.
_down: dict[str, float] = {}


def target_key(host: str, port: int | str, agent: str = "") -> str:
    """The agent a chat socket is for: its chat id when the client sent one
    (that survives a port change), else the host:port in the path."""
    return f"agent:{agent}" if agent else f"{host}:{port}"


def mark_down(key: str) -> bool:
    """Record a failed connect. True when the agent was up until now."""
    if key in _down:
        return False
    _down[key] = time.monotonic()
    return True


def mark_up(key: str) -> float | None:
    """Record a connect. How long the agent was down, or None if it wasn't."""
    since = _down.pop(key, None)
    return None if since is None else time.monotonic() - since


def _accepted_key(path: str) -> str | None:
    """The target key of a ``/fd/agent-ws/<host>/<port>?…`` request path."""
    parts = urlsplit(path)
    if not parts.path.startswith(_AGENT_WS_PATH):
        return None
    segs = parts.path[len(_AGENT_WS_PATH):].split("/")
    if len(segs) != 2:
        return None
    agent = (parse_qs(parts.query).get("agent") or [""])[0]
    return target_key(unquote(segs[0]), unquote(segs[1]), agent)


class AgentWsLogFilter(logging.Filter):
    """For ``uvicorn.error``: drop websockets' bare "connection open/closed"
    lines (they never say which socket), and the "[accepted]" line of a chat
    socket whose agent is already known to be down."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg in ("connection open", "connection closed") and not record.args:
            return False
        if (_down and isinstance(record.msg, str) and record.msg.endswith("[accepted]")
                and isinstance(record.args, tuple) and record.args):
            key = _accepted_key(str(record.args[-1]))
            if key is not None and key in _down:
                return False
        return True
