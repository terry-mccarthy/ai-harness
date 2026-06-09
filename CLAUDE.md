# AI Harness — Claude Code Guide

## Ways of working

These rules apply to every phase. Follow them without being asked.

**1. Update docs when tests go green.**
Before declaring a phase done, update:
- `README.md` — stack section, test count, config table, project layout
- `CLAUDE.md` — any new gotchas, changed startup commands, updated flow description
- `PROGRESS.md` — tick off passing tests and DoD items, note any divergences from `spec-full.md`

**2. Document gotchas immediately, not at end-of-phase.**
If something takes more than one attempt to get right — a library quirk, an API difference from docs, a config flag that was removed, a subtle ordering issue — add it to the relevant section of this file *before* moving on. Future sessions start cold; anything not written here will be re-discovered the hard way.

**3. Note divergences from the spec explicitly.**
When the implementation departs from `spec-full.md` (deliberate skip, pragmatic simplification, upstream difference), record it in `PROGRESS.md` under that phase's Notes section. Don't silently drift.

**4. Red before green.**
Write the test file first. Run it, confirm it fails for the right reason, then implement. A test that was never red proves nothing.

---

## Running the stack

```bash
# First time or after any changes to stub_servers/ or services/
docker compose build git-diff-stub linter-stub architect-stub sre-stub review-server governance dolt
docker compose down && docker compose up -d
# wait ~30s for Dolt to init and MCP init containers to register servers
docker compose ps   # all should show (healthy) or Exited (0)
```

```bash
# Run all tests
make test-integration
```

## Python environment

Python 3.14 is in use. The project uses **uv** for dependency management with a workspace layout.

```bash
# First time or after adding/changing dependencies
uv sync --all-packages   # creates .venv and installs all workspace packages

# Regenerate uv.lock after editing any pyproject.toml
uv lock
```

Workspace members in `packages/` reference each other via `[tool.uv.sources]` with `{ workspace = true }` — do not use PyPI paths for these.

All Docker services (`services/governance`, `services/review_server`, `stub_servers`) are also workspace members. Their `requirements.txt` files are generated from `uv.lock` and committed — Dockerfiles install from them:

```bash
# After editing any service's pyproject.toml or after uv lock
make requirements
```

Never hand-edit a service `requirements.txt` — it is always regenerated from the lockfile.

## Request flow (Phase 1)

```
Agent / GatewayClient
    → POST /api/v0/tools/invoke   (governance :8090)
        → validate JWT
        → POST /v1/data/harness/allow   (OPA :8181)
        → INSERT audit_log + CALL DOLT_COMMIT   (Dolt :3306)
        → POST /api/v0/tools/invoke   (MCPJungle :8080)
            → MCP server (architect-stub :9004 / sre-stub :9005 / etc.)
```

Claude Code connects to MCPJungle at `:8080/mcp` directly — governance is for agent-to-agent calls only.

## Governance service (`:8090`)

FastAPI app at `services/governance/server.py`. Three responsibilities:

1. **OAuth 2.1 client credentials** — `POST /oauth/token` (form body). Three clients: `architect`, `code-reviewer`, `sre`. Issues HS256 JWTs with 15-min TTL, signed with `JWT_SECRET`.
2. **OPA policy enforcement** — calls `POST /v1/data/harness/allow` with `{"input": {"agent_role": "...", "tool_name": "..."}}` before forwarding. Returns 403 if `result != true`.
3. **Dolt audit** — `INSERT INTO audit_log` then `CALL DOLT_COMMIT('-Am', 'audit: <tool_name>')` after every call (allow or deny).

Key env vars: `JWT_SECRET`, `MCPJUNGLE_INTERNAL_URL` (internal docker hostname), `OPA_URL`, `DOLT_HOST/PORT/USER/PASSWORD`.

## GatewayClient — OAuth token handling

`GatewayClient` in `packages/harness-gateway/harness_gateway/client.py` auto-fetches bearer tokens:

- `_get_token()` posts to `{base_url}/oauth/token` with `client_id` + `client_secret`
- Token is cached until 30s before expiry
- Returns `None` gracefully if governance returns 404 (e.g. running against raw MCPJungle)
- `call_tool()` adds `Authorization: Bearer <token>` when a token is available
- 401 responses raise `ToolAccessDenied`

## What MCPJungle actually does (free tier)

MCPJungle v0.4.5 is a **CLI-managed MCP proxy** — not the auth layer. Governance sits in front of it.

- Tool invocation: `POST /api/v0/tools/invoke` with a **flat** JSON body: `{"name": "server__tool", "key": "value", ...}`. Do NOT nest params inside an `"input"` or `"arguments"` key.
- Tool names use double-underscore separator: `serverName__toolName`.
- Exposes itself as an MCP server at `:8080/mcp`.
- Servers are registered via CLI or init containers — registration is ephemeral, lost on MCPJungle restart.
- The image is distroless — no shell, no wget, no curl. Healthchecks must use the `/mcpjungle` binary itself.

## MCPJungle flat API — `name` parameter conflict

MCPJungle's invoke body is flat: `{"name": "<server__tool>", ...params}`. If a tool has a parameter also named `name`, the params dict will **silently overwrite the tool identifier**:

```python
# BAD — {"name": "sre_stub__runbook_read", "name": "incident-response"}
#       Python merges to {"name": "incident-response"} — wrong tool!
invoke_tool(token, "sre_stub__runbook_read", {"name": "incident-response"})

# GOOD — parameter renamed to runbook_name
invoke_tool(token, "sre_stub__runbook_read", {"runbook_name": "incident-response"})
```

**Never name an MCP tool parameter `name`.**

## Dolt — init and gotchas

Dolt is a git-versioned MySQL-compatible database. The init approach in `services/dolt/init.sh` is three-phase:

1. **Local SQL mode** (`dolt sql`): DDL (`CREATE TABLE`) and initial commit (`CALL DOLT_COMMIT`). No server needed.
2. **Start server**: `dolt sql-server --host 0.0.0.0 --port 3306` (no `--user`/`--password` flags — removed in Dolt v1.x; root starts with no password).
3. **User management via `mysql` client**: `CREATE USER`, `GRANT` — these require server mode.

Key gotchas:
- `dolt init` requires author identity: set `user.email` and `user.name` via `dolt config --global` before running it.
- `dolt sql-server` v1.x: no `--user`/`--password` flags. Root has no password by default.
- `dolt sql` is a **local** command — it does not connect to a running server. Use a real MySQL client (`mysql`) to interact with a running Dolt server.
- `dolt_log` and `dolt_diff_audit_log` are system tables — they require explicit `GRANT SELECT` to non-root users.
- Governance commits after every audit INSERT: `CALL DOLT_COMMIT('-Am', 'audit: <tool_name>')`. The `-A` flag stages all changes.

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

Current policy (`policies/harness.rego`) maps three roles to tool sets:
- `architect` → `codebase_search`, `adr_read`, `adr_write`, `diagram_gen`
- `code_reviewer` → `git_diff`, `run_linter`, `coverage_report`, `repo_conventions_read`, `review_diff`
- `sre` → `observability_query`, `runbook_read`, `log_search`, `shell_exec`

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

MCPJungle returns `{"content": [{"type": "text", "text": "<json string>"}]}`. The client unwraps this automatically — callers get plain parsed dicts back. The key is `"content"`, not `"result"`.

## Model tuning (Ollama)

Three env vars control the LLM call in `review-server`. Set them in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Model name. 32b gives much better findings; 7b is faster for iteration. |
| `OLLAMA_NUM_CTX` | `8192` | Context window in tokens. Large diffs need more — default Ollama is 2048 which truncates real diffs. |
| `OLLAMA_TEMPERATURE` | `0.1` | Low = deterministic JSON. Don't raise above 0.3 or schema failures increase. |
| `OLLAMA_NUM_PREDICT` | `1024` | Max tokens to generate. Raise to 2048 for diffs with many findings. |

After changing `.env`, restart the container (no rebuild needed):
```bash
docker compose up -d --no-deps review-server
```

**Thinking models (qwen3 and similar):** Models that emit `<think>...</think>` blocks before their answer are handled automatically — the reviewer strips them before JSON parsing. `qwen3.6:27b` uses this path and reasons more carefully but is significantly slower.

**Speed vs quality on Apple Silicon:**
- `qwen2.5-coder:7b` — ~10s, misses subtle bugs
- `qwen2.5-coder:32b` — ~60–90s, catches most issues  
- `qwen3.6:27b` — ~2–5 min, best reasoning (thinking mode)

## Ollama from inside Docker

The `review-server` container needs to reach Ollama on the host. Docker Desktop exposes this via `host.docker.internal`:

```yaml
environment:
  OLLAMA_HOST: http://host.docker.internal:11434
```

## Adding a new tool

1. Add a `@mcp.tool()` function to an existing server, or create a new server under `services/`.
2. Rebuild: `docker compose build <service>`.
3. Re-register: `docker compose up register-<service>` (or add a new init container to compose).
4. Add the short-name → `server__tool` mapping to `TOOL_NAME_MAP` in `client.py`.
5. If the `CodeReviewerAgent` should call it, add the name to `allowed_tools`.
6. Add the tool to the appropriate role in `policies/harness.rego`; restart OPA or send a `PUT /v1/policies/harness` request.

## Adding a new MCP server (service)

New services that depend on `harness-gateway` or `harness-agents` need those packages copied into their Docker context. See `services/review_server/Dockerfile` for the pattern:

```dockerfile
COPY packages/harness-gateway /app/packages/harness-gateway
COPY packages/harness-agents /app/packages/harness-agents
RUN pip install -e /app/packages/harness-gateway -e /app/packages/harness-agents
```

Build context must be `.` (repo root), not the service subdirectory, so the `packages/` COPY works.

## Memory layer (Phase 2)

`packages/harness-memory` provides three layers:

- **Checkpointer** (`AsyncPostgresSaver`): use `AsyncPostgresSaver.from_conn_string(PG_DSN)` — not a raw `psycopg.AsyncConnection`. The raw connection path triggers `CREATE INDEX CONCURRENTLY inside transaction block` errors.
- **Memory store** (`PostgresMemoryStore`): auto-detects embedding dimension at `setup()` by calling Ollama. If the model changes between runs, the table is dropped and recreated. Dimension depends on model: `qwen2.5-coder:32b` → 5120, `qwen2.5:7b` → 3584.
- **Formula store** (`DoltFormulaStore`): uses synchronous pymysql (consistent with governance). Commit hash retrieved via `SELECT commit_hash FROM dolt_log LIMIT 1` — `@@dolt_repo_head` does not exist in Dolt v1.x.

**Embedding cosine similarity baseline**: code-oriented LLMs produce high baseline cosine similarity (~0.86–0.94) for all short natural-language text. The consolidation cluster threshold is 0.95 so only near-duplicate items merge.

**Formula test isolation**: test formulas use `agent_role="test_sre"` to avoid interference with seed formulas (`agent_role="sre"`).

**Stack startup**: rebuild Dolt when `services/dolt/init.sh` changes:
```bash
docker compose build dolt && docker compose up -d --no-deps dolt
```

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
