# AI Harness — Claude Code Guide

## Running the stack

```bash
# First time or after any changes to stub_servers/ or services/
docker compose build git-diff-stub linter-stub review-server
docker compose down && docker compose up -d
# wait ~20s for init containers to register MCP servers
docker compose ps   # all should show (healthy) or Exited (0)
```

```bash
# Run all tests
source .env
.venv/bin/pytest packages/harness-tests/ -v -m integration
```

## Python environment

Python 3.14 is in use. **Use the venv**, not system pip:

```bash
.venv/bin/pip install -e packages/harness-gateway -e packages/harness-agents -e packages/harness-tests
```

Root `pyproject.toml` uses `[tool.uv.workspace]` for IDE tooling only — the venv is managed manually with pip.

## What MCPJungle actually does (free tier)

MCPJungle v0.4.5 is a **CLI-managed MCP proxy**, not an OAuth gateway. Key facts:

- No OAuth endpoint — `/oauth/token` does not exist in free tier.
- Tool invocation: `POST /api/v0/tools/invoke` with a **flat** JSON body: `{"name": "server__tool", "key": "value", ...}`. Do NOT nest params inside an `"input"` or `"arguments"` key — they get passed through as a named argument and break Pydantic validation on the server.
- Tool names use a double-underscore separator: `serverName__toolName`.
- MCPJungle also exposes itself as an MCP server at `:8080/mcp` — Claude Code can connect to it directly.
- Servers are registered via CLI or init containers, not via `config.yml` at startup.
- The image is distroless — no shell, no wget, no curl. Healthchecks must use the `/mcpjungle` binary itself.
- Registration is ephemeral — lost on MCPJungle restart. Init containers re-register on every `docker compose up`.

## MCP server config (FastMCP)

All MCP servers use **FastMCP with streamable-HTTP transport** (`mcp[cli]` package, Python 3.12 inside Docker).

Critical config — without this, MCPJungle gets **421 Misdirected Request** when registering:

```python
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "server_name",
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
```

FastMCP defaults to DNS rebinding protection on `127.0.0.1`. Since Docker containers call each other by hostname (e.g. `git-diff-stub:9001`), the Host header validation fires and returns 421 unless disabled.

## GatewayClient response unwrapping

MCPJungle returns `{"content": [{"type": "text", "text": "<json string>"}]}`. The client unwraps this automatically — callers get plain parsed dicts back. The key is `"content"`, not `"result"` (an earlier mistake).

## Gateway client (no OAuth)

`GatewayClient` in `packages/harness-gateway/harness_gateway/client.py` calls MCPJungle directly without tokens. The `client_id` and `client_secret` fields are kept for interface compatibility but are unused. Policy enforcement is done by the tool allowlist in `TOOL_NAME_MAP` — any tool not in the map raises `ToolAccessDenied` immediately.

## OPA policy syntax

OPA `latest` requires the `if` keyword:

```rego
allow if {          # correct
    ...
}

allow {             # broken — rego_parse_error on modern OPA
    ...
}
```

## Ollama from inside Docker

The `review-server` container needs to reach Ollama on the host. Docker Desktop exposes this via `host.docker.internal`:

```yaml
environment:
  OLLAMA_HOST: http://host.docker.internal:11434
```

This is set in `docker-compose.yml` for the `review-server` service.

## Adding a new tool

1. Add a `@mcp.tool()` function to an existing server, or create a new server under `services/`.
2. Rebuild: `docker compose build <service>`.
3. Re-register: `docker compose up register-<service>` (or add a new init container to compose).
4. Add the short-name → `server__tool` mapping to `TOOL_NAME_MAP` in `client.py`.
5. If the `CodeReviewerAgent` should call it, add the name to `allowed_tools`.

## Adding a new MCP server (service)

New services that depend on `harness-gateway` or `harness-agents` need those packages copied into their Docker context. See `services/review_server/Dockerfile` for the pattern:

```dockerfile
COPY packages/harness-gateway /app/packages/harness-gateway
COPY packages/harness-agents /app/packages/harness-agents
RUN pip install -e /app/packages/harness-gateway -e /app/packages/harness-agents
```

Build context must be `.` (repo root), not the service subdirectory, so the `packages/` COPY works.

## Connecting Claude Code to the harness

MCPJungle exposes itself as an MCP server at `http://localhost:8080/mcp`. Add to Claude Code settings:

```json
{
  "mcpServers": {
    "ai-harness": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

This gives Claude Code access to all registered tools, including `review_server__review_diff`.
