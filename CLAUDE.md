# AI Harness — Claude Code Guide

## Ways of working

See `.claude/rules/ways-of-working.md`.

---

## Running the stack

```bash
# First time or after any changes to stub_servers/ or services/
docker compose build diff-proxy linter-stub github-mcp sre-stub review-server governance dolt
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
        → MCP server (github-mcp :9010 / review-server :9003 / sre-stub :9005 / etc.)
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
- `architect` → `codebase_search`, `adr_read`, `architecture_review`, `execute_architecture_check`
- `code_reviewer` → `git_diff`, `run_linter`, `coverage_report`, `repo_conventions_read`, `review_diff`
- `sre` → `observability_query`, `runbook_read`, `log_search`, `shell_exec`, `skill_search`

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

FastMCP defaults to DNS rebinding protection on `127.0.0.1`. Since Docker containers call each other by hostname (e.g. `diff-proxy:9001`), the Host header validation fires and returns 421 unless disabled.

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

## OpenRouter provider

Set `LLM_PROVIDER=openrouter` to route all LLM calls through [OpenRouter](https://openrouter.ai), which proxies dozens of hosted models — useful when local Ollama context limits are too small for large diffs.

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | *(required)* | Get from openrouter.ai/keys |
| `OPENROUTER_MODEL` | `anthropic/claude-3.5-sonnet` | Any slug from openrouter.ai/models |
| `LLM_TEMPERATURE` | `0.1` | Shared with other providers |
| `LLM_MAX_TOKENS` | `1024` | Output token cap — raise for large diffs |

Recommended large-context models via OpenRouter:
- `anthropic/claude-3.5-sonnet` — 200K context, strong reasoning
- `google/gemini-2.5-flash` — 1M context, very fast
- `openai/gpt-4o` — 128K context, reliable JSON output

```bash
# .env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet
LLM_MAX_TOKENS=2048
```

After changing `.env`, restart the container (no rebuild needed):
```bash
docker compose up -d --no-deps review-server
```

**Implementation note:** `OpenRouterProvider` uses the `openai` Python SDK with `base_url="https://openrouter.ai/api/v1"` — OpenRouter is OpenAI API-compatible. The class is in `packages/harness-agents/harness_agents/llm.py`.

**o-series reasoning models:** `temperature` is silently omitted for models matching `openai/o\d` (e.g. `openai/o1`, `openai/o4-mini`) — these models reject the parameter with a 400 error. All other models receive temperature normally.

**Error handling:** provider errors (auth failure, rate limit, empty choices from content filter) are caught in the reviewer's retry loop and returned as structured `{"code": "provider_error", "reason": "..."}` agent errors — same shape as all other agent errors. The retry loop does not retry provider errors.

**`OPENROUTER_API_KEY` validation:** the key is `.strip()`-ed before the empty check, so a whitespace-only value is caught at startup rather than producing a 401 at review time. Unknown provider names raise `ValueError` with the supported list (`ollama`, `gemini`, `openrouter`) — previously they silently fell through to Ollama.

## LLM provider factory — `build_llm_from_env()`

All agents and scripts must construct LLM providers via the canonical factory in `harness_agents/llm.py`. **Do not construct `OllamaProvider`, `GeminiProvider`, or `OpenRouterProvider` directly** in scripts or tests.

```python
from harness_agents.llm import build_llm_from_env

# env-driven (LLM_PROVIDER, OLLAMA_MODEL, etc.)
provider = build_llm_from_env()

# kwarg overrides
provider = build_llm_from_env(model="qwen3.6:27b", max_tokens=2048)

# config dict layer (from DB or any source) — same schema as server_config JSONB
provider = build_llm_from_env(config={"llm_provider": "gemini", "gemini": {"model": "gemini-2.5-flash"}})
```

Resolution order: **kwarg > config dict > env var > default**.

Provider dispatch uses `_PROVIDER_BUILDERS` dict; adding a new provider means adding a `_build_<name>()` function and an entry there.

**`harness-agents` has no asyncpg dependency.** If you need to read the `server_config` table to populate `config=`, do it in the calling script (see `scripts/demo_sre.py:_load_llm_config_from_pg()`). The factory itself stays DB-agnostic.

## Runtime LLM config via Postgres (`server_config` table)

The review-server's `PUT /config` endpoint writes LLM settings to the `server_config` Postgres table (JSONB column `config`). Any process that reads from this table at startup will pick up the same provider/model without a restart.

**`demo_sre.py`** does this: it calls `_load_llm_config_from_pg(pg_dsn)` before constructing the agent, so `make demo-sre` automatically uses whichever LLM the review-server is configured to use.

The table schema:
```json
{
  "llm_provider": "gemini",
  "gemini": { "model": "gemini-2.5-flash", "api_key": "..." },
  "ollama": { "model": "qwen2.5-coder:7b" },
  "openrouter": { "model": "anthropic/claude-3.5-sonnet" }
}
```

Only the active provider's sub-dict is used; the others are ignored.

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
7. If the OPA policy already lists the tool but it has no server or `TOOL_NAME_MAP` entry (e.g. `coverage_report`, `repo_conventions_read` — which were documented but unreachable), implement it rather than removing the OPA rule. A tool in OPA without a mapping is dead policy: `_resolve_name()` raises `ToolAccessDenied` before OPA is ever reached.

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

## SRE signal sources (slices 2, 4, 5)

All three signal-source tools share the same lazy-init + fallback pattern: they connect when the relevant env var is set, return a stub dict otherwise (unit tests pass without infra).

| Tool | Module | Store | Namespace | Seed command |
|---|---|---|---|---|
| `runbook_read` | `runbook_retriever.py` | `PostgresMemoryStore` | `"runbooks"` | `make seed-runbooks` |
| `log_search` | `log_retriever.py` | `PostgresMemoryStore` | `"logs"` | `make seed-logs` |
| `skill_search` | `skill_retriever.py` | `DoltFormulaStore` | N/A (TF-IDF lookup) | Dolt seed formulas |

`sre_stub` (`stub_servers/sre_server.py`) holds two lazy singletons:
- `_store` — `PostgresMemoryStore`, async-init on first `runbook_read` or `log_search` call when `PG_DSN` set
- `_dolt_store` — `DoltFormulaStore`, sync-init on first `skill_search` call when `DOLT_HOST` set

**sre-stub Docker gotcha**: sre-stub uses `Dockerfile.sre` (not `Dockerfile.stub`) with **build context `.` (repo root)** so it can COPY `packages/harness-memory`. diff-proxy and linter-stub continue to use `Dockerfile.stub`. When rebuilding:
```bash
docker compose build sre-stub
docker compose up -d --no-deps sre-stub
```

Before the agent can find runbooks and logs, seed them once with:
```bash
make seed-runbooks   # docs/runbooks/*.md  → pgvector "runbooks" namespace
make seed-logs       # docs/logs/*.jsonl   → pgvector "logs" namespace
```

## DynamicSREAgent — skill-aware guidance (slice 6)

`DynamicSREAgent(gateway, llm_provider, memory_store=None, formula_store=None)`.

When `formula_store` is provided, `_load_formula(task)` calls `store.lookup(self.name, task)` synchronously before the ReAct loop. A matched formula's steps are injected into the opening user message as a structured investigation plan (precedence over free-form investigation).

`make demo-sre` wires both stores when env vars are set; shows a capability banner on startup.

## DynamicSREAgent — semantic response cache

`DynamicSREAgent(gateway, llm_provider, memory_store=None, formula_store=None, cache_threshold=0.92, cache_ttl_seconds=86400)`.

`run()` calls `_cache_lookup(task)` before `_load_formula` / `_load_memory`. A hit skips the entire ReAct loop (no LLM calls, no tool invocations, no `_report_llm_usage` call) and returns `{**cached_state, "cache_hit": True}`.

**Two-tier lookup in `_cache_lookup`:**
1. Exact key match via `memory.read("cache", f"cache:{hash(task)}")` — Redis-accelerated, O(1) for repeated identical tasks.
2. Semantic match via `memory.search("cache", task, top_k=1)` — pgvector cosine similarity for near-identical tasks; hit only if score ≥ `cache_threshold`.

**`_cache_write` gotcha — use `_embedding_text=task`:** `PostgresMemoryStore.write()` embeds `json.dumps(value)` by default. A cache entry stores `{"task": task, "agent_output": ...}` and passing `_embedding_text=task` makes the pgvector embedding represent the task string only, not the noisy report JSON. Without this, semantic search scores against the full value and near-identical tasks may not reach the threshold.

**`cache` namespace:** separate from `"sre"`, `"runbooks"`, and `"logs"`. No DDL migration needed — it's a text column value, auto-created on first write.

**`force_refresh: bool` in `AgentState`:** when `True`, `_cache_lookup` returns `None` unconditionally and `_cache_write` is skipped.

**Cache write only on success:** `_cache_write` is called alongside `_save_memory` in `_react_loop` only when `agent_output` is set (i.e., the ReAct loop produced a valid report). Error states never populate the cache.

**Threshold calibration:** 0.92 is the default. Same-topic pairs with `nomic-embed-text` score 0.82–0.93; different-topic pairs score 0.35–0.62. A threshold too low returns wrong cached answers; too high produces few hits. The integration tests use 0.88 for the near-identical paraphrase scenario to give headroom.

## Agent orchestration (Phases 3–4)

### Task classification — LLM-primary with keyword fallback

`classify_node` asks the LLM for structured JSON (`{"task_type": "design|review|incident|bootstrap"}`) and parses it leniently (`<think>` blocks stripped, first `{...}` extracted). Fallback order: LLM JSON → keyword heuristic → `review`. Keywords are a *fallback only* — do not reintroduce them as the primary path; surface keywords misroute (e.g. "Review the alert that fired" is an incident). Mocks in tests must return the JSON contract, not a bare word.

`bootstrap` is the fourth task type — triggered by tasks like "generate ARCHITECTURE.md" or "document the architecture". It routes to the architect, runs the full four-phase analysis, adds a fifth `_phase_bootstrap_doc` pass that converts phase results to a markdown document, and stores the result in `agent_output["architecture_md"]`. Bootstrap tasks bypass the architectural gate (no sandbox validation needed for doc generation).

**`bootstrap_architecture` MCP tool** — `review_server__bootstrap_architecture` is now registered with MCPJungle. Accepts `repo` (GitHub URL), optional `task`, and LLM provider overrides. Calls `ArchitectAgent` directly (no supervisor graph), uses `architect` OAuth credentials (`ARCHITECT_SECRET`). Returns `{"architecture_md": "...", "summary": "...", "findings": [...], "recommendations": [...]}`. **Timeout note:** this tool runs 5 LLM calls sequentially and will exceed Claude Code's default 60s MCP timeout — launch with `MCP_TOOL_TIMEOUT=300000 claude`. See the Claude Code MCP tool timeout section.

**`ArchitectAgent.repo` param** — The architect agent previously passed `self.gateway.gateway_url` (the MCPJungle URL) as the `repo` parameter to `codebase_search` and `adr_read`. This was a latent bug hidden by mock gateways in tests. Fixed: `ArchitectAgent.__init__` now accepts `repo: str = ""` and uses `self.repo` in all tool calls. Always pass a GitHub URL (`https://github.com/owner/repo`) when constructing the agent for real usage.

### Stale pytest processes can deadlock the suite

A hung/abandoned `pytest -m integration` process holds Dolt + PostgreSQL connections and can make a fresh run hang indefinitely (observed at `test_otel_spans_emitted`, which opens a real `DoltFormulaStore` connection). If the suite stalls, check `pgrep -f pytest` for zombies before debugging anything else.

### OllamaProvider timeout

`OllamaProvider` now enforces a **120-second timeout** on embeddings and LLM calls. If Ollama is memory-starved (e.g., 32b model loaded while running large test suite), requests will timeout after 120s rather than hanging forever. The timeout prevents indefinite waits but will fail fast on slow systems.

### Embedding dimension caching

`PostgresMemoryStore` caches the detected embedding dimension as a class variable (`_embed_dim_cache: dict[str, int]`) keyed by model name. The first `setup()` call to detect dimension still calls Ollama (unavoidable), but subsequent stores reuse the cached value instead of calling Ollama again. This eliminates ~19 redundant embed calls per 27-test Phase 2 run (from ~8 min down to ~9 sec).

### Human approval token scoping

The `human_approval_token` is a short-lived JWT (10-min TTL) scoped to a specific `thread_id` and `tool_name` (e.g., `shell_exec`). The token is passed as the `X-Human-Approval-Token` header to governance, which validates the signature and scope before allowing the tool call. A token issued for thread A cannot be reused for thread B, and a token for `shell_exec` cannot be used for other tools.

## Architectural gate — Phase 7 gotchas

- **Graph wiring change for architect path:** The architect agent goes through `_route_after_architect` (a conditional edge), not a hard edge. `_route_after_architect` sends `bootstrap` tasks straight to `synthesise` (gate skipped) and `design` tasks to `architectural_gate → route_after_gate`. Code reviewer and SRE agents are unaffected (still use `_should_propose_formula`). If you add a new task type that should also skip the gate, add a branch to `_route_after_architect`.
- **`route_after_gate` routing:** PASS → `synthesise`, FAIL with HARD → `human_gate`, FAIL with SOFT without `human_justification` → `human_gate`, FAIL with SOFT with `human_justification` → `synthesise`. No gate signal → `error_handler`.
- **`human_gate` now has two resume paths:** `human_justification` (gate soft-fail) → resume to `synthesise`; `human_approval_token` (shell_exec) → resume to `sre`. The justification check comes first.
- **`execute_architecture_check` is a stub:** Mapped to `review_server__execute_architecture_check` in the TOOL_NAME_MAP. The actual sandbox isolation (Docker-in-Docker) is not implemented. The graph wiring, OPA policy, Dolt schema, and governance endpoint are all functional — only the stub handler needs to be replaced when sandboxes are built.
- **`architecture_review` moved to review server:** Mapped to `review_server__architecture_review`. The host-side architect server (which previously provided this tool) has been retired. The review server uses the GitHub API to fetch invariants directly.
- **Architect read-only + issue filing:** `architect_stub` is now served by `github-mcp` with `codebase_search`, `adr_read`, and `issue_create`. `adr_write` and `diagram_gen` were removed. The architect files GitHub issues for CRITICAL/HIGH findings instead of writing ADRs.
- **Docker build for github-mcp:** `docker compose build github-mcp` builds the new service. No separate `register` init container needed — `register-architect` points to `github-mcp:9010`.
- **Dolt migration for gate failures:** The `architectural_gate_failures` table must exist before integration tests pass. Use `docker compose exec dolt mysql ... -e "CREATE TABLE IF NOT EXISTS ..."` against the running Dolt container (see `services/dolt/init.sh` for the full schema).

## Connecting Claude Code to the harness

MCPJungle exposes itself as an MCP server at `http://localhost:8080/mcp`. Register it with a static `Authorization` header to bypass OAuth discovery (see gotcha below):

```bash
claude mcp add -s user --transport http "ai-harness" "http://localhost:8080/mcp" \
  -H "Authorization: Bearer no-auth"
```

This gives Claude Code access to all registered tools, including `review_server__review_diff`.

**OAuth discovery gotcha (Claude Code ≥ v2.1.181):** Claude Code now performs OAuth 2.0 discovery (`GET /.well-known/oauth-authorization-server`) before connecting to any HTTP MCP server. MCPJungle returns `404 page not found` as plain text, which the SDK can't parse as JSON — dropping the connection with "SDK auth failed: HTTP 404". The `-H "Authorization: Bearer no-auth"` flag makes the SDK treat the server as pre-authenticated and skip discovery entirely. MCPJungle ignores the header value.

## Claude Code MCP tool timeout

Claude Code's MCP client has a **hard ~60-second timeout** derived from the MCP TypeScript SDK default (`DEFAULT_REQUEST_TIMEOUT_MSEC = 60000`). Long-running tools like `bootstrap_architecture` (5 LLM calls + 5 tool round-trips) routinely exceed this.

**Workaround — launch Claude Code with an extended timeout:**

```bash
MCP_TOOL_TIMEOUT=300000 claude   # 5 minutes
```

`MCP_TOOL_TIMEOUT` is honoured by Claude Code CLI. The per-server `timeout` field in `.mcp.json` was working in ≤ v2.1.107 but is silently ignored for HTTP transport since v2.1.113 (open regression: [anthropics/claude-code#50289](https://github.com/anthropics/claude-code/issues/50289)).

**Why ContextForge doesn't fix this:** ContextForge's `TOOL_TIMEOUT` (default 60s, configurable) controls how long ContextForge waits for the upstream server — but Claude Code's client-side timeout fires first regardless. Both would need to be extended, and only `MCP_TOOL_TIMEOUT` is actually configurable today.

## git_diff tool modes

Three input modes, evaluated in priority order:

1. `diff_text` (non-empty string) — passthrough; echoed back immediately. Used by agents that already have the diff.
2. `pr_number` + `github_repo` — fetches the unified diff from `GET https://api.github.com/repos/{github_repo}/pulls/{pr_number}` with `Accept: application/vnd.github.v3.diff`. Token read from `GITHUB_TOKEN` env var (optional; omit for public repos). Works from inside Docker — no filesystem access needed.
3. `repo_path` + `base`/`head` — runs `git diff` against the Docker-internal baked sample repo at `/app/sample-repo`.

## PG config persistence gotcha

The review-server stores runtime config (PUT /config) in Postgres `server_config` table. `.env` has `PG_DSN=localhost:5432` — works for host-side Python but **breaks inside Docker**. In docker-compose.yml, `PG_DSN` is hardcoded to `postgres` hostname (service name) to avoid the `.env` override.

Even with correct PG_DSN, FastMCP's streamable-http transport only runs the lifespan **per MCP request**, not at server startup. Custom routes (GET /config) bypass the lifespan entirely. Config loads lazily on first MCP call (e.g. `initialize`). Until that call, `_CONFIG` shows defaults.

`_init_pg_pool` has 5-attempt retry with backoff because the review-server has no `depends_on: postgres`.

## review_server HTTP endpoint

`POST http://localhost:9003/review` — plain JSON endpoint for CI pipelines, pre-commit hooks, and webhooks.

```bash
DIFF=$(git diff origin/main...HEAD)
curl -s http://localhost:9003/review \
  -H "Content-Type: application/json" \
  -d "{\"diff_text\": $(echo "$DIFF" | jq -Rs .)}" | jq .
```

Body: `{"diff_text": "...", "task": "...", "provider": "ollama|gemini|openrouter"}` (`task` and `provider` optional).
Returns same schema as `review_diff` MCP tool. Errors: 401 for bad/missing key, 422 for missing `diff_text`, 400 for bad provider name or missing `OPENROUTER_API_KEY`, 500 for agent failure.

**Auth:** set `REVIEW_API_KEY` in env to require `Authorization: Bearer <key>`. When unset, the endpoint is open (dev/local mode). The empty default in `docker-compose.yml` (`${REVIEW_API_KEY:-}`) means auth is off by default locally — set the var before deploying publicly.

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
make monitoring-up   # starts prometheus:9090, grafana:3000, and otel-collector:4317 (monitoring profile)
# Grafana: admin/admin — dashboards are pre-provisioned (see services/grafana/dashboards/)
```

Governance exposes `GET /metrics`. Metrics: `harness_tool_calls_total`, `harness_tool_call_latency_ms`. (`harness_rate_limit_rejections_total` was removed — rate limiting delegated to CF.)

## Claude Code OTEL telemetry pipeline

Claude Code emits OTLP metrics natively. The pipeline:

```
Claude Code (Mac host)
  → OTLP gRPC :4317
    → otel-collector (Docker, monitoring profile)
      → Prometheus scrapes :8889
        → Grafana dashboards
```

**To activate:** start the monitoring stack then set this env var before launching Claude:

```bash
make monitoring-up
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 claude
```

**Config files:**
- `services/otel-collector/otel-collector.yml` — collector config
- `services/grafana/dashboards/claude-code-telemetry.json` — overview (sessions, cost, tokens)
- `services/grafana/dashboards/claude-code-by-project.json` — per-project/branch breakdown

**Delta → cumulative gotcha:** Claude Code emits delta temporality; Prometheus requires cumulative. The `deltatocumulative` processor in `otel-collector.yml` converts automatically — no env var needed on the Claude side. Alternatively, `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative` bypasses the processor.

**Resource attribute labels:** `resource_to_telemetry_conversion: enabled: true` in the exporter causes OTEL resource attributes (service.name, project_name, project_branch, host.arch, etc.) to appear as Prometheus labels — this is what makes the per-project dashboard's template variables work.

**Key metrics:** `claude_code_session_count_total`, `claude_code_cost_usage_USD_total`, `claude_code_token_usage_tokens_total`.

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

## Eval suites (reviewer + architect)

See [`docs/eval-guide.md`](docs/eval-guide.md) for fixture formats, pass bars, CI setup, and known gotchas.

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
