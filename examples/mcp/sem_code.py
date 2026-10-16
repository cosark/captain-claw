"""Look up local code through Flight Deck's MCP manager, without an LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from captain_claw.flight_deck import mcp_storage
from captain_claw.flight_deck.mcp_manager import MCPManager


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="Symbol name to find, e.g. parse_config")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--sem", default="sem", help="Sem executable or absolute path")
    args = parser.parse_args()
    executable = shutil.which(args.sem)
    if not executable:
        parser.error("Sem executable not found; install Sem or pass --sem")
    repo = args.repo.resolve()
    if not repo.is_dir():
        parser.error("--repo must be an existing repository directory")
    record = {
        "name": "sem-code-example",
        "transport": "stdio",
        "command": executable,
        "args": ["mcp"],
        "env": {"SEM_REPO": str(repo), "SEM_NO_NETWORK": "1", "SEM_NO_TELEMETRY": "1"},
        "enabled": True,
        "allowed_agents": [],
    }
    previous = os.environ.get("CAPTAIN_CLAW_FD_MCP_PATH")
    with tempfile.TemporaryDirectory(prefix="claw-sem-") as directory:
        os.environ["CAPTAIN_CLAW_FD_MCP_PATH"] = str(Path(directory) / "servers.json")
        manager = MCPManager()
        try:
            await mcp_storage.upsert_server(record)
            tools = await manager.list_tools(record["name"])
            if "sem_find" not in {tool["name"] for tool in tools}:
                raise RuntimeError("Server did not expose sem_find")
            result = await manager.call_tool(record["name"], "sem_find", {
                "query": args.query, "mode": "context", "token_budget": 1200,
            })
            if result.get("isError"):
                raise RuntimeError(f"Code lookup failed: {result}")
            print(json.dumps(result, indent=2))
        finally:
            await manager.close()
            if previous is None:
                os.environ.pop("CAPTAIN_CLAW_FD_MCP_PATH", None)
            else:
                os.environ["CAPTAIN_CLAW_FD_MCP_PATH"] = previous


if __name__ == "__main__":
    asyncio.run(main())
