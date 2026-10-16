# Local repository lookup with Sem

[Sem](https://github.com/Ataraxy-Labs/sem) exposes symbol lookup and source
search over stdio MCP. This optional example uses Flight Deck's existing MCP
manager; it does not change built-in tools or require model credentials.

Install Sem using its upstream instructions (`sem --version`), then from this
repository root:

```bash
uv venv .venv-sem
uv pip install --python .venv-sem/bin/python -e .
.venv-sem/bin/python examples/mcp/sem_code.py --repo /absolute/path/to/repo parse_config
```

Replace the path and symbol with an existing Git checkout and symbol. Use
`--sem /absolute/path/to/sem` if Sem is not on PATH. The script discovers tools
and calls `sem_find` with a 1,200-token output budget. An empty lookup is not a
transport failure: try a symbol that exists in the checkout. The first lookup
may build a local index. This is a direct tool smoke test, not an agent benchmark.
It uses a temporary servers file and closes the child process on exit, leaving
saved Flight Deck connections unchanged.

To use it in conversations, open **Connections → MCP servers → Add server**:

- Name: `sem-code` (choose a new name if it already exists)
- Transport: `stdio`
- Command: the absolute path to the Sem executable
- Arguments: `["mcp"]`
- Environment: `{"SEM_REPO":"/absolute/path/to/repo"}`
- Allowed agents: select the agents that should access this checkout

Test and save the connection. Paths refer to the **Flight Deck host**, not the
browser or an agent's remote machine. A standalone agent without `FD_URL` does
not load Flight Deck tools. The proxy lookup tool is `mcp_sem-code_sem_find`.
Consult the discovered tool schemas for the installed Sem version.

Only the lookup in this script is read-only with respect to source code; Sem
may write its index. The full stdio server also exposes validation tools that
can execute commands. Use a trusted checkout and appropriate host permissions;
`SEM_REPO` is not a sandbox, and an empty agent allowlist permits every agent.
