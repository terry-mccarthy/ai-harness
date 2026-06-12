# AI Harness — Claude Code Guide

## Ways of working

These rules apply to every phase. Follow them without being asked.

**1. Update docs when tests go green.**
Before declaring a phase done, update:
- `README.md` — stack section, test count, config table, project layout
- `CLAUDE.md` — any new gotchas, changed startup commands, updated flow description
- `ARCHITECTURE.md` — the current architecture and ADRs. 
- `PROGRESS.md` — tick off passing tests and DoD items, note any divergences from `spec-full.md`

**2. Document gotchas immediately, not at end-of-phase.**
If something takes more than one attempt to get right — a library quirk, an API difference from docs, a config flag that was removed, a subtle ordering issue — add it to the relevant section of this file *before* moving on. Future sessions start cold; anything not written here will be re-discovered the hard way.

**3. Note divergences from the spec explicitly.**
When the implementation departs from `spec-full.md` (deliberate skip, pragmatic simplification, upstream difference), record it in `PROGRESS.md` under that phase's Notes section. Don't silently drift.

**4. Red before green.**
Write the test file first. Run it, confirm it fails for the right reason, then implement. A test that was never red proves nothing.

**5. Code health**

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

## Request flow (Phase 6+)

```
Agent / GatewayClient
    → POST /check          (governance :8090 — OPA policy decision)
    → POST /api/v0/tools/invoke   (MCPJungle :8080 or CF :4444 — direct)
        → MCP server (architect-stub :9004 / sre-stub :9005 / etc.)
    → POST /audit          (governance :8090 — async Dolt write, fire-and-forget)
```

Governance is no longer in the forwarding path. GatewayClient calls the gateway (MCPJungle or ContextForge) directly, with governance providing policy checks and audit as a sidecar.

Claude Code connects to MCPJungle at `:8080/mcp` directly.

## Governance service (`:8090`)

FastAPI app at `services/governance/server.py`. Three responsibilities:

1. **OAuth 2.1 client credentials** — `POST /oauth/token` (form body). Three clients: `architect`, `code-reviewer`, `sre`. Issues **RS256 JWTs** with 15-min TTL, signed with a private RSA key loaded from `JWT_PRIVATE_KEY_FILE`.
2. **OPA policy check** — `POST /check` validates a token and calls OPA. Returns 200 `{"allowed": true, ...}` or 403.
3. **Dolt audit** — `POST /audit` accepts an audit record and writes to Dolt asynchronously (202 response). `CALL DOLT_COMMIT` per write.
4. **JWKS** — `GET /jwks` returns the RSA public key as a JWK set; downstream verifiers fetch from here.

Rate limiting is delegated to the gateway (ContextForge natively). Governance does not rate-limit.

Key env vars: `JWT_PRIVATE_KEY_FILE` (path to PEM private key), `OPA_URL`, `DOLT_HOST/PORT/USER/PASSWORD`.

**Test key tripwire:** `test-fixtures/jwt-test-key.pem` is committed for local dev. Governance refuses to start with this key unless `ENV=test` is set — fingerprint-checked at startup. Never set `ENV=test` in a production deployment.

## GatewayClient — split gateway + governance

`GatewayClient` in `packages/harness-gateway/harness_gateway/client.py`:

- `gateway_url` — direct tool invocation (MCPJungle `:8080` or CF `:4444`)
- `governance_url` — policy check + audit sidecar (governance `:8090`); optional
- When `governance_url` is set: calls `POST /check` before invoking, fires `POST /audit` after (async background task)
- When `governance_url` is None: legacy proxy mode (gateway_url is the governance proxy)
- `gateway_backend="contextforge"` enables CF's JSON-RPC call format + CF JWT auth
- 401/403 responses raise `ToolAccessDenied`

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

**Embedding model**: `nomic-embed-text` (768 dims, controlled by `EMBED_MODEL` env var) is used for all vector operations — separate from `OLLAMA_MODEL` which is the chat/LLM model. `nomic-embed-text` gives clean semantic separation: same-topic pairs score ~0.82–0.93, different-topic pairs ~0.35–0.62. The consolidation cluster threshold is 0.80.

**Formula test isolation**: test formulas use `agent_role="test_sre"` to avoid interference with seed formulas (`agent_role="sre"`).

**Stack startup**: rebuild Dolt when `services/dolt/init.sh` changes:
```bash
docker compose build dolt && docker compose up -d --no-deps dolt
```

## Agent orchestration (Phases 3–4)

### Task classification — LLM-primary with keyword fallback

`classify_node` asks the LLM for structured JSON (`{"task_type": "design|review|incident"}`) and parses it leniently (`<think>` blocks stripped, first `{...}` extracted). Fallback order: LLM JSON → keyword heuristic → `review`. Keywords are a *fallback only* — do not reintroduce them as the primary path; surface keywords misroute (e.g. "Review the alert that fired" is an incident). Mocks in tests must return the JSON contract, not a bare word.

### Stale pytest processes can deadlock the suite

A hung/abandoned `pytest -m integration` process holds Dolt + PostgreSQL connections and can make a fresh run hang indefinitely (observed at `test_otel_spans_emitted`, which opens a real `DoltFormulaStore` connection). If the suite stalls, check `pgrep -f pytest` for zombies before debugging anything else.

### OllamaProvider timeout

`OllamaProvider` now enforces a **120-second timeout** on embeddings and LLM calls. If Ollama is memory-starved (e.g., 32b model loaded while running large test suite), requests will timeout after 120s rather than hanging forever. The timeout prevents indefinite waits but will fail fast on slow systems.

### Embedding dimension caching

`PostgresMemoryStore` caches the detected embedding dimension as a class variable (`_embed_dim_cache: dict[str, int]`) keyed by model name. The first `setup()` call to detect dimension still calls Ollama (unavoidable), but subsequent stores reuse the cached value instead of calling Ollama again. This eliminates ~19 redundant embed calls per 27-test Phase 2 run (from ~8 min down to ~9 sec).

### Human approval token scoping

The `human_approval_token` is a short-lived JWT (10-min TTL) scoped to a specific `thread_id` and `tool_name` (e.g., `shell_exec`). The token is passed as the `X-Human-Approval-Token` header to governance, which validates the signature and scope before allowing the tool call. A token issued for thread A cannot be reused for thread B, and a token for `shell_exec` cannot be used for other tools.

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

## ContextForge gateway (Phase 5)

ContextForge (`ghcr.io/ibm/mcp-context-forge:latest`) runs on port 4444 as an alternative to MCPJungle.

**Setup flow:**
1. `docker compose up -d contextforge` — waits for health check
2. `docker compose up setup-contextforge` — registers all stubs + creates `harness_all` virtual server
3. Set `GATEWAY_BACKEND=contextforge` in governance to route through ContextForge

**Key gotchas:**

- **Transport must be `STREAMABLEHTTP` (uppercase)** when registering a gateway. `streamablehttp` (lowercase) returns 422. FastMCP stubs use POST-based streamable HTTP; ContextForge sends `GET /mcp` by default (SSE) which returns 400 from FastMCP.
- **SSRF protection blocks Docker internal hostnames** by default. Must set `SSRF_ALLOW_PRIVATE_NETWORKS=true` and `SSRF_ALLOW_LOCALHOST=true` in the ContextForge container env.
- **JWT format for ContextForge API calls** requires: `sub`, `preferred_username`, `iss=mcpgateway`, `aud=mcpgateway-api`, `jti` (UUID), `exp`. Signed with `CF_JWT_SECRET`. `create_jwt_token` in ContextForge is async.
- **Tool name mapping**: `architect_stub__codebase_search` → `architect-stub-codebase-search` (replace `__` and `_` with `-`).
- **Virtual server UUID**: discovered at runtime by `ContextForgeGatewayClient` via `GET /servers` matching by name. `toolCount=0` on gateway registration is normal — tools are discovered asynchronously.

## Rate limiting (Phase 6+)

Rate limiting is now delegated to the gateway (ContextForge natively). Governance no longer rate-limits — the old Redis sliding-window counter was removed. `test_governance_no_rate_limit` verifies governance returns no 429s regardless of call volume.

## Token budget (Phase 5)

`HarnessState` now has `tokens_used: int` and `token_budget: int | None`. Budget check fires in `run_agent_node` — if `tokens_used >= token_budget`, returns `error.code = "budget_exceeded"`. Existing tests pass because they don't set `token_budget` (`.get()` defaults to `None` = unlimited).

## Agent-level token tracking

`LLMResponse` has `prompt_tokens: int = 0` and `completion_tokens: int = 0`. Both `OllamaProvider` (from `prompt_eval_count`/`eval_count`) and `GeminiProvider` (from `usage_metadata`) populate them. `None` values from the API default to 0.

`AgentState` has `token_usage: dict` (`{"prompt_tokens": int, "completion_tokens": int}`) and `token_budget: int | None`. `CodeReviewerAgent` accumulates counts each iteration and checks the budget **after a failed parse attempt** — successful responses are never cancelled. Error code: `token_budget_exceeded`.

`AgentState` uses `total=False` so existing code constructing partial state dicts does not need updating.

## Monitoring stack (Phase 5)

```bash
make monitoring-up   # starts prometheus:9090 and grafana:3000 (monitoring profile)
# Grafana: admin/admin — "AI Harness — Cost per Agent Role" dashboard is pre-provisioned
```

Governance exposes `GET /metrics`. Metrics: `harness_tool_calls_total`, `harness_tool_call_latency_ms`. (`harness_rate_limit_rejections_total` was removed — rate limiting delegated to CF.)

## Prompt files

All LLM system prompts live in `prompts/` and are loaded at import time:

| File | Loaded by | Used for |
|---|---|---|
| `prompts/classify.md` | `harness_supervisor/nodes.py` | Task classification (system message) |
| `prompts/synthesise.md` | `harness_supervisor/nodes.py` | Final-response synthesis (system message, LLM call) |
| `prompts/code_reviewer.md` | `harness_agents/reviewer.py` | Code review system prompt |
| `prompts/architect.md` | `harness_agents/architect.py` | Architect agent system prompt |
| `prompts/sre.md` | `harness_agents/sre.py` | SRE agent system prompt |

Override the prompts directory: set `PROMPTS_DIR=/path/to/prompts` before starting any service.

`synthesise_node` makes a real LLM call when `llm_provider` is supplied (graph wiring); falls back to a string-format summary when `llm_provider=None` (test-only path).

## Reviewer eval suite

`eval-fixtures/` contains labeled diffs for benchmarking the `CodeReviewerAgent` against known security bugs without a running Docker stack:

```bash
pytest -m eval -v -s   # runs against live Ollama; slow (~2 min for 7b model)
```

**Fixture format:**
- `eval-fixtures/diffs/<name>.diff` — synthetic git diff
- `eval-fixtures/labels/<name>.json` — `{"verdict": "pass|fail", "must_flag": [{"pattern": "...", "min_severity": "CRITICAL"}]}`

**Pass bars:** verdict accuracy ≥ 80%, average recall ≥ 60% across all fixtures.

**Adding fixtures:** write a `.diff` + matching `.json` in `eval-fixtures/`. The parametrized test picks them up automatically. When the model uses different phrasing than your pattern (e.g. "role enforcement" instead of "authorization"), update the label pattern — the fixture labels are as much under test as the model.

Eval tests use a `_MockGateway` that returns the fixture diff for `git_diff` and empty findings for `run_linter`, bypassing the live stack entirely.

## Linter stub (semgrep)

`stub_servers/linter_server.py` runs semgrep against the added lines extracted from the diff. Rules are in `stub_servers/semgrep-rules.yml` — edit this file to add or tune rules, then `docker compose cp` to test without a full rebuild.

**metavariable-regex gotcha:** semgrep's `metavariable-regex` is anchored (`re.match`), not substring (`re.search`). To match a variable name that *contains* a keyword (e.g. `AWS_SECRET_ACCESS_KEY`), the regex must be `(?i).*secret.*` — not `(?i)secret`. Without the `.*` prefix, compound names silently miss.

## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
