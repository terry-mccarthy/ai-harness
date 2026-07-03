# Memory Layer & Agent Internals

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

**Embedding dimension caching**: `PostgresMemoryStore` caches the detected embedding dimension as a class variable (`_embed_dim_cache: dict[str, int]`) keyed by model name. The first `setup()` call to detect dimension still calls Ollama (unavoidable), but subsequent stores reuse the cached value instead of calling Ollama again. This eliminates ~19 redundant embed calls per 27-test Phase 2 run (from ~8 min down to ~9 sec).

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

**`bootstrap_architecture` MCP tool** — `review_server__bootstrap_architecture` is now registered with MCPJungle. Accepts `repo` (GitHub URL), optional `task`, and LLM provider overrides. Calls `ArchitectAgent` directly (no supervisor graph), uses `architect` OAuth credentials (`ARCHITECT_SECRET`). Returns `{"architecture_md": "...", "summary": "...", "findings": [...], "recommendations": [...]}`. **Timeout note:** this tool runs 5 LLM calls sequentially and will exceed Claude Code's default 60s MCP timeout — launch with `MCP_TOOL_TIMEOUT=300000 claude`. See `docs/dev/gateway.md`.

**`ArchitectAgent.repo` param** — The architect agent previously passed `self.gateway.gateway_url` (the MCPJungle URL) as the `repo` parameter to `codebase_search` and `adr_read`. This was a latent bug hidden by mock gateways in tests. Fixed: `ArchitectAgent.__init__` now accepts `repo: str = ""` and uses `self.repo` in all tool calls. Always pass a GitHub URL (`https://github.com/owner/repo`) when constructing the agent for real usage.

### Stale pytest processes can deadlock the suite

A hung/abandoned `pytest -m integration` process holds Dolt + PostgreSQL connections and can make a fresh run hang indefinitely (observed at `test_otel_spans_emitted`, which opens a real `DoltFormulaStore` connection). If the suite stalls, check `pgrep -f pytest` for zombies before debugging anything else.

### Token budget (Phase 5)

`HarnessState` now has `tokens_used: int` and `token_budget: int | None`. Budget check fires in `run_agent_node` — if `tokens_used >= token_budget`, returns `error.code = "budget_exceeded"`. Existing tests pass because they don't set `token_budget` (`.get()` defaults to `None` = unlimited).

### Agent-level token tracking

`LLMResponse` has `prompt_tokens: int = 0` and `completion_tokens: int = 0`. Both `OllamaProvider` (from `prompt_eval_count`/`eval_count`) and `GeminiProvider` (from `usage_metadata`) populate them. `None` values from the API default to 0.

`AgentState` has `token_usage: dict` (`{"prompt_tokens": int, "completion_tokens": int}`) and `token_budget: int | None`. `CodeReviewerAgent` accumulates counts each iteration and checks the budget **after a failed parse attempt** — successful responses are never cancelled. Error code: `token_budget_exceeded`.

`AgentState` uses `total=False` so existing code constructing partial state dicts does not need updating.

## Architectural gate — Phase 7 gotchas

- **Graph wiring change for architect path:** The architect agent goes through `_route_after_architect` (a conditional edge), not a hard edge. `_route_after_architect` sends `bootstrap` tasks straight to `synthesise` (gate skipped) and `design` tasks to `architectural_gate → route_after_gate`. Code reviewer and SRE agents are unaffected (still use `_should_propose_formula`). If you add a new task type that should also skip the gate, add a branch to `_route_after_architect`.
- **`route_after_gate` routing:** PASS → `synthesise`, FAIL with HARD → `human_gate`, FAIL with SOFT without `human_justification` → `human_gate`, FAIL with SOFT with `human_justification` → `synthesise`. No gate signal → `error_handler`.
- **`human_gate` now has two resume paths:** `human_justification` (gate soft-fail) → resume to `synthesise`; `human_approval_token` (shell_exec) → resume to `sre`. The justification check comes first.
- **`execute_architecture_check` is a stub:** Mapped to `review_server__execute_architecture_check` in the TOOL_NAME_MAP. The actual sandbox isolation (Docker-in-Docker) is not implemented. The graph wiring, OPA policy, Dolt schema, and governance endpoint are all functional — only the stub handler needs to be replaced when sandboxes are built.
- **`architecture_review` moved to review server:** Mapped to `review_server__architecture_review`. The host-side architect server (which previously provided this tool) has been retired. The review server uses the GitHub API to fetch invariants directly.
- **Architect read-only + issue filing:** `architect_stub` is now served by `github-mcp` with `codebase_search`, `adr_read`, and `issue_create`. `adr_write` and `diagram_gen` were removed. The architect files GitHub issues for CRITICAL/HIGH findings instead of writing ADRs.
- **Docker build for github-mcp:** `docker compose build github-mcp` builds the new service. No separate `register` init container needed — `register-architect` points to `github-mcp:9010`.
- **Dolt migration for gate failures:** The `architectural_gate_failures` table must exist before integration tests pass. Use `docker compose exec dolt mysql ... -e "CREATE TABLE IF NOT EXISTS ..."` against the running Dolt container (see `services/dolt/init.sh` for the full schema).
