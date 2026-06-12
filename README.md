# AI Harness

A governed, self-learning agent harness with production hardening. Every tool call routes through a governance layer: OAuth 2.1 auth, OPA policy enforcement, Redis rate limiting, and a tamper-evident Dolt audit log. Supports MCPJungle and ContextForge as MCP gateway backends with a feature-flag rollback.

## What it does

Point it at a diff (or let it fetch one from a repo). It runs a linter, analyses both, and returns structured JSON:

```json
{
  "verdict": "fail",
  "findings": [
    {
      "severity": "CRITICAL",
      "file": "auth.py",
      "line": 14,
      "message": "Password is being printed to stdout — credential leak risk.",
      "suggestion": "Remove the print statement."
    }
  ],
  "summary": "The diff introduces a critical security vulnerability: passwords are logged in plaintext."
}
```

The reviewer checks for security vulnerabilities (credential leaks, injection flaws, path traversal), code quality issues (error handling gaps, dead code, resource leaks), and architectural concerns (hardcoded values, tight coupling, shared mutable state). Findings are classified as `CRITICAL`, `WARNING`, or `INFO`; verdict is `fail` if any `CRITICAL` finding exists.

The agent is also exposed as an MCP tool (`review_diff`) — Claude Code or any MCP client can call it directly.

## Stack

- **Governance** — FastAPI service (`:8090`) that issues RS256 JWTs, enforces OPA policy, and writes tamper-evident audit rows to Dolt; exposes `GET /jwks` for public key distribution
- **MCPJungle** — MCP proxy that routes tool calls and exposes itself as an MCP server
- **OPA** — policy engine; `policies/harness.rego` maps agent roles to allowed tools; enforced on every request
- **Dolt** — git-versioned MySQL-compatible database; audit rows and formula versions are auto-committed so both logs are append-only and diffable
- **PostgreSQL** (`pgvector/pgvector:pg16`) — MCPJungle state, LangGraph checkpoints, and vector memory store; pgvector extension enables semantic search
- **Redis 7** — hot-read cache for the memory store; frequently accessed items served in-process without hitting PostgreSQL
- **Ollama** (`qwen2.5-coder:32b` default) — local LLM for reviews and vector embeddings; no API key needed
- **git-diff-stub** — runs real `git diff` on a baked-in sample repo
- **linter-stub** — semgrep-based linter (`semgrep-rules.yml`); catches SQL f-string injection, hardcoded credentials, `subprocess shell=True`, `open()` f-string paths, and `eval()`
- **architect-stub** — stub MCP server for architect-role tools (`codebase_search`, `adr_read`, `adr_write`, `diagram_gen`)
- **sre-stub** — stub MCP server for SRE-role tools (`observability_query`, `runbook_read`, `log_search`, `shell_exec`)
- **review-server** — FastMCP service wrapping the full code-reviewer agent; callable from Claude Code
- **ContextForge** (`ghcr.io/ibm/mcp-context-forge`, `:4444`) — production MCP gateway; alternative to MCPJungle, enabled via `GATEWAY_BACKEND=contextforge`
- **Prometheus + Grafana** — optional monitoring stack (`make monitoring-up`); governance exposes `/metrics` with tool-call counters, latency histograms, and rate-limit rejections; pre-built cost-per-role dashboard at `localhost:3000`

## Quick start

**Prerequisites:** Docker, Ollama running with `qwen2.5-coder` pulled, [uv](https://docs.astral.sh/uv/) installed.

```bash
# 1. Configure
cp .env.example .env
# edit .env — set CODE_REVIEWER_SECRET, ARCHITECT_SECRET, SRE_SECRET
# JWT_PRIVATE_KEY_FILE defaults to test-fixtures/jwt-test-key.pem (dev only; set ENV=test)

# 2. Build and start the stack
docker compose build git-diff-stub linter-stub architect-stub sre-stub review-server governance dolt
docker compose up -d
sleep 30  # wait for Dolt to init and MCP init containers to register servers

# 3. Install Python deps (workspace layout — installs all packages including harness-memory)
uv sync --all-packages

# 4. Run all tests
make test-integration
```

## Configuration

All options are in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5-coder` | LLM used by agents for chat/reasoning |
| `EMBED_MODEL` | `nomic-embed-text` | Dedicated embedding model for semantic memory search (768 dims) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `MCPJUNGLE_URL` | `http://localhost:8090` | Governance service URL (agent tool calls route here) |
| `GOVERNANCE_URL` | `http://localhost:8090` | Governance service URL (tests use this directly) |
| `JWT_PRIVATE_KEY_FILE` | `test-fixtures/jwt-test-key.pem` | Path to PEM-encoded RSA private key for RS256 JWT signing. Set `ENV=test` when using the committed test key. In production, supply a real key and omit `ENV=test`. |
| `CODE_REVIEWER_SECRET` | — | Client secret for the `code-reviewer` OAuth client |
| `ARCHITECT_SECRET` | `architect-secret` | Client secret for the `architect` OAuth client |
| `SRE_SECRET` | `sre-secret` | Client secret for the `sre` OAuth client |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL for memory hot-read cache |
| `PG_DSN` | `postgresql://harness:harness@localhost:5432/harness` | PostgreSQL DSN for memory store and checkpointer |
| `DOLT_HOST` | `localhost` | Dolt MySQL endpoint host |
| `DOLT_PORT` | `3306` | Dolt MySQL endpoint port |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `RATE_LIMIT_PER_MINUTE` | `20` | Rate limit delegated to ContextForge gateway; this value is kept for CF configuration |
| `GATEWAY_BACKEND` | `mcpjungle` | Active MCP backend: `mcpjungle` or `contextforge` |
| `CF_JWT_SECRET` | `cf-dev-secret-…` | JWT signing secret for ContextForge API calls |
| `CF_ADMIN_EMAIL` | `admin@harness.local` | Admin subject claim in ContextForge JWTs |
| `CF_SERVER_NAME` | `harness_all` | ContextForge virtual server name aggregating all tools |

To enable debug logging without restarting the whole stack:

```bash
LOG_LEVEL=DEBUG docker compose up -d git-diff-stub linter-stub review-server governance
```

## Tests

### Integration suite (74 tests: all green) — `make test-integration`

### Phase 0 — Core reviewer (9 tests)

| Test | What it proves |
|---|---|
| `test_reviewer_produces_structured_output` | Diff in → valid JSON out, catches obvious bugs |
| `test_tool_calls_go_through_gateway` | Tool calls are visible in the gateway audit log |
| `test_reviewer_denied_cross_role_tool` | Unlisted tools are blocked before the network call |
| `test_review_diff_tool_is_reachable` | `review_diff` MCP tool is registered and callable |
| `test_review_diff_returns_valid_schema` | MCP tool output satisfies the output schema |
| `test_review_diff_catches_credential_leak` | End-to-end: MCP call → agent → model → CRITICAL finding |
| `test_git_diff_returns_real_diff_format` | `git_diff` runs real git and returns proper diff output |
| `test_git_diff_contains_commit_changes` | Diff output contains the actual changed lines |
| `test_git_diff_respects_ref` | Tool accepts base/head refs |

### Phase 1 — Governance (17 tests)

| Test | What it proves |
|---|---|
| `test_architect_client_auth` | `/oauth/token` issues tokens for architect client |
| `test_reviewer_client_auth` | `/oauth/token` issues tokens for code-reviewer client |
| `test_sre_client_auth` | `/oauth/token` issues tokens for sre client |
| `test_architect_allowed_tool` | Architect token can call `codebase_search` |
| `test_architect_denied_tool` | Architect token cannot call `shell_exec` (403) |
| `test_reviewer_allowed_tool` | code-reviewer token can call `git_diff` |
| `test_reviewer_denied_tool` | code-reviewer token cannot call `adr_write` (403) |
| `test_sre_allowed_tool` | sre token can call `runbook_read` |
| `test_unknown_token_rejected` | Invalid bearer token returns 401 |
| `test_audit_row_written` | Tool call writes a row to `audit_log` in Dolt |
| `test_audit_policy_rule_recorded` | Audit row has `policy_decision` and `policy_rule` populated |
| `test_audit_dolt_commit_created` | Audit INSERT triggers a Dolt commit (visible in `dolt_log`) |
| `test_audit_dolt_history_queryable` | `dolt_diff_audit_log` is queryable and non-empty |
| `test_audit_no_delete` | `harness` DB user cannot DELETE from `audit_log` |
| `test_opa_allow_architect_tool` | OPA returns `true` for architect + codebase_search |
| `test_opa_deny_cross_role` | OPA returns `false` for architect + shell_exec |
| `test_token_expiry` | Expired JWT returns 401 |

### Phase 2 — Persistent Memory Layer (27 tests)

| Test | What it proves |
|---|---|
| `test_checkpointer_saves_state` | LangGraph checkpoint written to PostgreSQL after a graph step |
| `test_checkpointer_resumes` | Graph resumed from checkpoint skips already-run nodes |
| `test_checkpointer_thread_isolation` | Thread A checkpoint not visible from thread B |
| `test_memory_write_and_read` | `write()` + `read()` round-trip within a session |
| `test_memory_namespace_isolation` | Writes to `architect/` invisible from `sre/` |
| `test_memory_cross_session_persistence` | Item survives closing and reopening the store |
| `test_memory_ttl_expiry` | Expired item not returned by `read()` |
| `test_memory_redis_hot_read` | Second read served from Redis (cache_hits counter) |
| `test_memory_semantic_search` | `search()` ranks most-relevant item first via pgvector |
| `test_memory_overwrite` | Re-writing same key updates value |
| `test_memory_delete` | `delete()` removes item; subsequent read returns None |
| `test_memory_interface_compliance` | `PostgresMemoryStore` satisfies `MemoryStore` Protocol |
| `test_sre_runbook_namespace` | SRE namespace isolated from architect and code_reviewer |
| `test_episodic_memory_write` | Write with `memory_type='episodic'` stores `consolidated=False` |
| `test_semantic_memory_written_by_consolidation` | `run_pass()` creates semantic items; source episodes marked consolidated |
| `test_consolidation_clusters_similar_episodes` | Two similar episodes merge into one semantic item |
| `test_consolidation_preserves_distinct_episodes` | Two distinct episodes produce two separate semantic items |
| `test_consolidation_prunes_expired_items` | Expired episodes deleted by `run_pass()` |
| `test_formula_quality_score_updated` | 8/10 successful pours → quality_score ≥ 0.8 after consolidation |
| `test_formula_graduates_to_proven` | ≥10 pours, ≥80% success → status='proven' |
| `test_formula_flagged_for_review` | ≥10 pours, <30% success → status='review' |
| `test_formula_write_creates_dolt_commit` | `propose()` creates a Dolt commit containing the formula id |
| `test_formula_lookup_by_task` | `lookup()` returns best-matching formula for a task description |
| `test_formula_lookup_no_match` | `lookup()` returns None for unmatched task |
| `test_formula_version_history` | Two `propose()` calls → two Dolt commits; both versions queryable |
| `test_formula_deprecate` | Deprecated formula excluded from `list_active()` and `lookup()` |
| `test_formula_interface_compliance` | `DoltFormulaStore` satisfies `FormulaStore` Protocol |

### Phase 3 — Specialised Agent Nodes (4 tests)

| Test | What it proves |
|---|---|
| `test_agent_node_contract_compliance` | All three agents satisfy `AgentNode` Protocol |
| `test_architect_tool_calls_go_via_gateway` | ArchitectAgent calls tools through GatewayClient |
| `test_architect_denied_shell_exec` | Architect role is blocked from `shell_exec` by OPA |
| `test_sre_shell_exec_blocked_without_approval` | SRE `shell_exec` blocked without `X-Human-Approval-Token` header |

### Phase 4 — Agent Orchestration (27 tests)

| Test | What it proves |
|---|---|
| `test_classify_design_task` | LLM classifier → `task_type='design'` |
| `test_classify_review_task` | LLM classifier → `task_type='review'` |
| `test_classify_incident_task` | LLM classifier → `task_type='incident'` |
| `test_classify_llm_routes_ambiguous_task` | Task with no routing keywords is classified by the LLM |
| `test_classify_llm_overrides_keyword_match` | LLM verdict wins over a misleading surface keyword |
| `test_classify_falls_back_to_keywords_when_llm_unavailable` | LLM outage degrades to keyword heuristic |
| `test_classify_unparseable_llm_output_defaults_to_review` | Garbage LLM output + no keywords → safe default `review` |
| `test_classify_strips_think_blocks` | Thinking-model `<think>…</think>` output parsed correctly |
| `test_route_to_architect` | task_type='design' → architect node |
| `test_route_to_reviewer` | task_type='review' → code_reviewer node |
| `test_route_to_sre` | task_type='incident' → sre node |
| `test_error_handler_on_gateway_403` | 403 from gateway triggers error_handler node |
| `test_formula_lookup_hit` | `lookup()` matches formula by task description |
| `test_formula_lookup_miss` | `lookup()` returns None for unmatched task |
| `test_formula_outcome_recorded` | Formula pours recorded after synthesise node |
| `test_agent_executes_ad_hoc_without_formula` | SRE agent runs freely without formula guidance |
| `test_agent_executes_formula_steps` | Agent follows formula steps in order |
| `test_propose_formula_on_novel_task` | Draft formula created for unmatched ad-hoc run |
| `test_human_gate_pauses_graph` | Graph pauses when `requires_human_approval=True` |
| `test_human_gate_resumes_with_valid_token` | Valid approval token resumes graph |
| `test_human_gate_rejects_expired_token` | Expired token causes error_handler |
| `test_human_gate_rejects_wrong_scope` | Token with wrong `thread_id` causes error_handler |
| `test_checkpoint_survives_human_pause` | Graph state survives pause + resume from PostgreSQL |
| `test_otel_spans_emitted` | All nodes emit OpenTelemetry spans |
| `test_full_design_task_e2e` | Design task → final_response via architect |
| `test_full_review_task_e2e` | Review task → final_response via code_reviewer |
| `test_full_incident_task_no_shell_e2e` | Incident task → final_response without human gate |

### Phase 5 — Production Hardening (8 tests)

| Test | What it proves |
|---|---|
| `test_owasp_memory_write_requires_auth` | `POST /memory/write` returns 401 without a Bearer token |
| `test_owasp_prompt_injection_blocked` | Injected instruction in a tool response cannot alter agent tool calls |
| `test_cost_otel_tag_present` | Agent OTel spans carry `agent_role` and `thread_id` attributes |
| `test_token_budget_enforced` | Graph terminates with `budget_exceeded` when `tokens_used ≥ token_budget` |
| `test_rate_limit_tool_calls` | N+1 tool calls in one minute returns 429 from governance |
| `test_contextforge_tool_group_parity` | Phase 1 tools work through the ContextForge gateway |
| `test_contextforge_audit_log_parity` | Dolt audit rows are written regardless of which backend is active |
| `test_gateway_rollback` | MCPJungle backend (the default) passes Phase 1 tests after CF migration |

### Eval suite (7 tests) — `pytest -m eval -v -s`

Scores the `CodeReviewerAgent` against 6 labeled diffs with known security bugs (SQL injection, hardcoded secrets, shell injection, missing auth, path traversal, clean refactor). Uses a mock gateway — no Docker stack needed, only Ollama.

| Test | What it proves |
|---|---|
| `test_reviewer_fixture[01_clean_refactor]` | No false-positive CRITICALs on a clean refactor |
| `test_reviewer_fixture[02_sql_injection]` | Catches SQL injection (f-string + string concat in queries) |
| `test_reviewer_fixture[03_hardcoded_secret]` | Catches hardcoded AWS credentials and database password |
| `test_reviewer_fixture[04_shell_injection]` | Catches `shell=True` with user-controlled input |
| `test_reviewer_fixture[05_missing_auth]` | Catches auth/role decorators removed from admin endpoints |
| `test_reviewer_fixture[06_path_traversal]` | Catches user-controlled filename used directly in `open()` |
| `test_reviewer_aggregate_score` | Asserts verdict accuracy ≥ 80% and recall ≥ 60% across all fixtures |

### Token usage unit tests (9 tests) — `pytest packages/harness-tests/test_token_usage.py`

| Test | What it proves |
|---|---|
| `test_llm_response_has_token_fields` | `LLMResponse` carries `prompt_tokens` and `completion_tokens` |
| `test_llm_response_defaults_to_zero` | Fields default to 0 when not supplied |
| `test_ollama_provider_captures_token_counts` | `OllamaProvider` maps `prompt_eval_count`/`eval_count` to response |
| `test_ollama_provider_none_counts_become_zero` | `None` eval counts (cached Ollama response) default to 0 |
| `test_agent_state_accepts_token_fields` | `AgentState` TypedDict accepts `token_usage` and `token_budget` |
| `test_reviewer_accumulates_token_usage` | Reviewer returns accumulated token counts in result state |
| `test_reviewer_accumulates_across_retries` | Token counts sum across all retry iterations |
| `test_reviewer_budget_exceeded_on_retry` | Reviewer aborts with `token_budget_exceeded` when completion tokens exceed budget after a failed parse |
| `test_reviewer_no_budget_runs_to_completion` | `token_budget=None` never triggers budget check |

## Connect Claude Code

MCPJungle exposes itself as an MCP server. Add to Claude Code settings:

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

Claude Code will see all registered tools including `review_server__review_diff`.

> Note: Claude Code connects directly to MCPJungle at `:8080/mcp`. The governance layer at `:8090` is for agent-to-agent tool calls — it handles auth, policy, and audit before forwarding to MCPJungle.

## Project layout

```
├── packages/
│   ├── harness-gateway/   # GatewayClient + ContextForgeGatewayClient
│   ├── harness-agents/    # CodeReviewerAgent, ArchitectAgent, SREAgent, LLM providers
│   ├── harness-memory/    # PostgresMemoryStore, DoltFormulaStore, ConsolidationWorker
│   ├── harness-supervisor/# LangGraph supervisor graph, HarnessState, OTel spans
│   └── harness-tests/     # Integration tests (Phases 0–5) + load test + eval suite
├── services/
│   ├── governance/        # OAuth (RS256) + OPA + Dolt audit + /metrics + /jwks (port 8090)
│   ├── contextforge_setup/# Init script: registers MCP stubs with ContextForge, creates virtual server
│   ├── grafana/           # Provisioned cost-per-role dashboard
│   ├── prometheus/        # Prometheus scrape config (governance /metrics)
│   ├── dolt/              # Dolt init — audit_log, formulas, formula_pours, seed data
│   ├── postgres/          # postgres init — enables pgvector extension
│   └── review_server/     # review_diff MCP tool (wraps the agent)
├── stub_servers/          # git_diff, run_linter, architect, sre MCP servers
├── prompts/               # LLM system prompts (classify.md, synthesise.md, code_reviewer.md, architect.md, sre.md)
├── eval-fixtures/         # Labeled diffs for reviewer quality benchmarking (diffs/ + labels/)
├── test-fixtures/         # Committed test RSA key (jwt-test-key.pem) — dev/CI only
├── policies/              # OPA policy (harness.rego)
├── security/              # owasp-review.md — OWASP Agentic AI Top 10 review
├── docs/runbooks/         # 4 operational runbooks (agent-unresponsive, policy-rollback, cost-spike, bad-formula)
├── docker-compose.yml
└── .env.example
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full request flow and design decisions, and [CLAUDE.md](CLAUDE.md) for operational notes and gotchas.
