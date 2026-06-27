# AI Harness

![AI Harness](docs/ai-harness.jpeg)

A governed, memory-augmented agent harness with production hardening. Every tool call routes through a governance layer: OAuth 2.1 auth, OPA policy enforcement, and a tamper-evident Dolt audit log. Recurring successful remediations are promoted into versioned, HITL-gated skills via a procedural skill-learning pipeline. Supports MCPJungle and ContextForge as MCP gateway backends with a feature-flag rollback.

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
- **LLM providers** — pluggable via `LLM_PROVIDER`: `ollama` (default; local `qwen2.5-coder`, no API key needed), `gemini` (`gemini-2.5-flash`), or `openrouter` (any hosted model). Provider and per-provider settings are switchable at runtime via the review-server `PUT /config` endpoint. Ollama also serves vector embeddings (`nomic-embed-text`)
- **diff-proxy** — real `git diff` on the baked sample repo, or fetches a PR diff from the GitHub API (`pr_number` + `github_repo`; reads `GITHUB_TOKEN` from env)
- **linter-stub** — semgrep-based linter (`semgrep-rules.yml`); catches SQL f-string injection, hardcoded credentials, `subprocess shell=True`, `open()` f-string paths, and `eval()`
- **github-mcp** — MCP server wrapping GitHub API for architect-role tools (`codebase_search`, `adr_read`, `issue_create`)
- **sre-stub** — MCP server for SRE-role tools; `runbook_read` and `log_search` do semantic pgvector search when `PG_DSN` is set (seed with `make seed-runbooks` / `make seed-logs`); `skill_search` looks up proven formulas from Dolt when `DOLT_HOST` is set; all tools fall back to stubs without infra
- **review-server** — FastMCP service wrapping the full code-reviewer agent; callable from Claude Code via MCP and from CI pipelines via `POST /review` (plain HTTP, optional bearer-token auth via `REVIEW_API_KEY`)
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
docker compose build diff-proxy linter-stub github-mcp sre-stub review-server governance dolt
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
| `LLM_PROVIDER` | `ollama` | Active LLM provider: `ollama`, `gemini`, or `openrouter`. Override per-request via the `provider` field or at runtime via review-server `PUT /config`. |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | LLM used by agents for chat/reasoning when provider is `ollama` |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model used when provider is `gemini` (requires `GEMINI_API_KEY`) |
| `OPENROUTER_MODEL` | `anthropic/claude-3.5-sonnet` | Model used when provider is `openrouter` (requires `OPENROUTER_API_KEY`) |
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
LOG_LEVEL=DEBUG docker compose up -d diff-proxy linter-stub review-server governance
```

### Runtime LLM config (shared via `server_config` table)

`GET /config` and `PUT /config` on the review-server (`:9003`) let you change LLM provider settings at runtime without rebuilding or restarting. Changes are persisted in PostgreSQL (`server_config` table) and survive container restarts. Auth via `Authorization: Bearer <REVIEW_API_KEY>` (unset key = open in dev).

The `server_config` table is the **shared LLM config store** — `make demo-sre` reads it at startup via `build_llm_from_env(config=...)`, so the SRE demo always uses the same provider/model as the review-server. The capability banner shows the active provider and config source:

```
llm           : gemini/gemini-2.5-flash (source: db config)
```

| Method | Path | Description |
|---|---|---|
| `GET` | `/config` | Return current runtime overrides (api keys masked). |
| `PUT` | `/config` | Update runtime overrides. Returns merged config. |

**`PUT /config` body:**

```json
{
  "llm_provider": "openrouter",
  "ollama": { "model": "qwen2.5-coder:32b", "num_ctx": 24000 },
  "gemini": { "model": "gemini-2.5-flash", "temperature": 0.2 },
  "openrouter": { "model": "anthropic/claude-sonnet-4-6", "max_tokens": 2048 }
}
```

Supported per-provider keys: `model`, `temperature`, `max_tokens`/`num_predict`, `num_ctx` (Ollama only), `host` (Ollama only). Set a key to `null` to remove the override and fall back to the env var or default. Only changed keys need to be sent.

## Tests

### Integration suite (246 tests: all green) — `make test-integration`

### Unit suite (262 tests: all green) — `make test-unit`

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
| `test_reviewer_denied_tool` | code-reviewer token cannot call `issue_create` (403) |
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

### Bootstrap — Architecture doc generation (15 tests)

| Test | What it proves |
|---|---|
| `test_bootstrap_adds_architecture_md` | `task_type='bootstrap'` → `agent_output['architecture_md']` present |
| `test_non_bootstrap_omits_architecture_md` | Non-bootstrap runs do not add `architecture_md` |
| `test_bootstrap_still_produces_synthesis_output` | Bootstrap run still contains standard synthesis fields |
| `test_bootstrap_continues_when_doc_phase_fails` | LLM failure in bootstrap phase → synthesis returned, no error |
| `test_classify_node_bootstrap_from_llm` | LLM `task_type='bootstrap'` is accepted by classifier |
| `test_classify_node_bootstrap_keyword_fallback` | "generate architecture.md" classifies as bootstrap via keyword fallback |
| `test_route_node_bootstrap_goes_to_architect` | `task_type='bootstrap'` routes to architect node |
| `test_route_after_architect_bootstrap_skips_gate` | Bootstrap bypasses architectural gate → goes straight to synthesise |
| `test_route_after_architect_design_goes_to_gate` | Design tasks still flow through the architectural gate |
| `test_route_after_architect_error_goes_to_error_handler` | Error state always routes to error_handler |
| `test_bootstrap_architecture_returns_md` | `bootstrap_architecture` MCP tool returns `architecture_md` on success |
| `test_bootstrap_architecture_raises_on_agent_error` | Synthesis failure → `RuntimeError` from the MCP tool |
| `test_bootstrap_architecture_uses_architect_credentials` | MCP tool builds `GatewayClient` with `client_id='architect'` |
| `test_bootstrap_architecture_default_task_includes_repo` | Default task string contains the repo URL |
| `test_bootstrap_architecture_repo_passed_to_agent` | Repo URL forwarded to `ArchitectAgent` constructor, not gateway URL |

### Phase 3 — Specialised Agent Nodes (6 tests)

| Test | What it proves |
|---|---|
| `test_agent_node_contract_compliance` | All three agents satisfy `AgentNode` Protocol |
| `test_architect_synthesis_retries_on_schema_violation` | Schema-invalid synthesis is rejected and retried, then accepted |
| `test_architect_errors_when_synthesis_never_schema_valid` | Synthesis that never validates → `run()` returns `invalid_output` |
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
| `test_route_to_architect` (bootstrap) | task_type='bootstrap' → architect node, gate skipped |
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

### Skill Learning — issues 01–08 (64 tests)

| Suite | Tests | What it covers |
|---|---|---|
| `test_skill_learning_schema.py` | 14 | Dolt schema (episodes/candidates/skills), harness user grants, DoltFormulaStore compat |
| `test_episode_capture.py` | 4 | `/audit` writes episode row; fire-and-forget; audit_log unaffected |
| `test_outcome_labeling.py` | 7 | `POST /episodes/{id}/label` — rejection cases + happy path + Dolt commit |
| `test_candidate_proposal.py` | 8 | `POST /candidates` — criteria rejections + happy path + `GET /candidates/{id}` |
| `test_hitl_promotion.py` | 13 | Promote/reject — scope guard, re-promotion versioning, full e2e |
| `test_skill_execution.py` | 11 | `GET`/revoke skills + execute_skill (ABORT/ROLLBACK/CONTINUE) |
| `test_skill_expiry.py` | 12 | `POST /skills/expire`, re-validation auto-proposal, auto-trigger, early-review flag |
| `test_skill_select.py` | 7 | `POST /skills/select` — specificity/recency/success-rate tiebreaks, escalation, audit_log |
| `test_skills_cli.py` | 19 | `GET /episodes`, `/candidates`, `/skills` list endpoints; CLI subprocess for full pipeline |

### Phase 6 — Skills Registry (20 tests)

| Suite | Tests | What it covers |
|---|---|---|
| `test_governance_author.py` | 10 | `POST /skills/author`, `GET /skills/{id}/prompt` — manual authoring path, `manually_authored` column, Dolt commit, 403 for SRE, 410 on revoke |
| `test_skill_registry.py` | 10 | 14 MCP tools via `skills-registry-server` — list/get/create/revoke skills, label episodes, execute skill, access-control enforcement |

### Phase 7 — Architecture as Code (14 tests)

| Test | What it proves |
|---|---|
| `test_gate_passes_clean_code` | No violations → gate_signal.result == 'PASS' |
| `test_gate_fails_layer_violation` | Layer violation → HARD severity, FAIL result |
| `test_gate_enforces_complexity_limit` | Complexity limit → SOFT severity, FAIL result |
| `test_gate_passes_params_to_tool` | repo_path + target_language forwarded to tool |
| `test_gate_handles_tool_denied` | ToolAccessDenied → FAIL + error dict |
| `test_route_after_gate_pass` | PASS → routs to synthesise |
| `test_route_after_gate_hard_fail` | HARD violation → routs to human_gate |
| `test_route_after_gate_soft_fail_no_justification` | SOFT without justification → human_gate |
| `test_route_after_gate_soft_fail_with_justification` | SOFT with justification → synthesise |
| `test_route_after_gate_no_signal` | No signal → error_handler |
| `test_architect_halts_on_hard_constraint` | E2E: architect → gate → human_gate on HARD |
| `test_architect_passes_on_clean_code` | E2E: architect → gate → synthesise on PASS |
| `test_dolt_records_gate_failures` | architectural_gate_failures INSERT + Dolt commit |
| `test_audit_architectural_gate_endpoint` | POST /audit/architectural-gate returns 202 |

### Eval suite (11 tests) — `pytest -m eval -v -s`

Scores the agents against labeled fixtures with known problems. Uses mock gateways — no Docker stack needed, only Ollama.

**Reviewer** — `CodeReviewerAgent` against labeled diffs with known security bugs:

| Test | What it proves |
|---|---|
| `test_reviewer_fixture[01_clean_refactor]` | No false-positive CRITICALs on a clean refactor |
| `test_reviewer_fixture[02_sql_injection]` | Catches SQL injection (f-string + string concat in queries) |
| `test_reviewer_fixture[03_hardcoded_secret]` | Catches hardcoded AWS credentials and database password |
| `test_reviewer_fixture[04_shell_injection]` | Catches `shell=True` with user-controlled input |
| `test_reviewer_fixture[05_missing_auth]` | Catches auth/role decorators removed from admin endpoints |
| `test_reviewer_fixture[06_path_traversal]` | Catches user-controlled filename used directly in `open()` |
| `test_reviewer_aggregate_score` | Asserts verdict accuracy ≥ 80% and recall ≥ 60% across all fixtures |

**Architect** — four-phase `ArchitectAgent` against fixture repos expressed as canned tool responses:

| Test | What it proves |
|---|---|
| `test_architect_fixture[clean_layered]` | Control: a clean hexagonal app raises no false CRITICAL |
| `test_architect_fixture[god_controller]` | Catches business logic + SQL inline in an HTTP handler (layering/coupling) |
| `test_architect_fixture[leaky_persistence]` | Catches SQLAlchemy/ORM leaking through a domain "port" (abstraction/coupling) |
| `test_architect_aggregate_score` | Asserts schema validity 100%, detection ≥ 66%, recall ≥ 50%, and that synthesis output matches `ARCHITECT_OUTPUT_SCHEMA` |

### Semantic response cache (14 tests)

| Test | What it proves |
|---|---|
| `test_high_score_hit_returns_cached_result` | Mock store score ≥ 0.92 → `run()` returns early with `cache_hit: True` and stored `agent_output`; LLM never called |
| `test_configurable_threshold_respected` | `cache_threshold=1.0` makes a 0.95-score result a miss |
| `test_successful_run_writes_to_cache_with_ttl` | Successful run writes `{task, agent_output}` to `"cache"` namespace with `ttl_hours` derived from `cache_ttl_seconds` |
| `test_failed_run_does_not_write_to_cache` | `max_turns_exceeded` result produces no cache write |
| `test_low_score_hit_runs_loop` | Score < threshold → ReAct loop runs, no `cache_hit` |
| `test_llm_usage_not_reported_on_cache_hit` | `report_llm_usage` never called on cache hit |
| `test_force_refresh_skips_cache_lookup` | `force_refresh=True` bypasses lookup even on a 0.99-score hit |
| `test_force_refresh_does_not_write_to_cache` | `force_refresh=True` on a successful run produces no write to the `"cache"` namespace |
| `test_empty_search_result_runs_loop` | Empty search → loop runs normally |
| `test_no_memory_store_agent_runs_unchanged` | `memory_store=None` → no cache path, backward-compatible |
| `test_same_task_twice_returns_cache_hit` | (integration) Identical task submitted twice → second call returns `cache_hit: True` via Redis exact key |
| `test_semantically_equivalent_task_returns_cache_hit` | (integration) Near-identical paraphrase → cache hit via pgvector; requires live embedding + pgvector |
| `test_expired_cache_entry_is_a_miss` | (integration) Entry with `expires_at` in the past is filtered out; ReAct loop runs |
| `test_force_refresh_bypasses_cached_result_and_runs_loop` | (integration) Cached task re-submitted with `force_refresh=True` → LLM called, `cache_hit` absent |

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

### git_diff GitHub mode (9 tests) — `pytest packages/harness-tests/test_git_diff_github.py`

| Test | What it proves |
|---|---|
| `test_fetch_github_pr_diff_calls_correct_url` | API call targets the correct GitHub pulls endpoint |
| `test_fetch_github_pr_diff_sets_diff_accept_header` | `Accept: application/vnd.github.v3.diff` header is set |
| `test_fetch_github_pr_diff_includes_auth_header_when_token_given` | `Authorization: Bearer <token>` added when token present |
| `test_fetch_github_pr_diff_omits_auth_header_when_no_token` | No auth header when token is absent (public repos) |
| `test_fetch_github_pr_diff_returns_decoded_body` | Response body decoded to string and returned |
| `test_git_diff_github_mode_returns_pr_diff` | Tool routes to GitHub mode and returns `source: github` |
| `test_git_diff_github_mode_passes_env_token` | `GITHUB_TOKEN` env var forwarded to API call |
| `test_git_diff_github_mode_missing_repo_raises` | `pr_number` without `github_repo` raises `ValueError` |
| `test_git_diff_diff_text_takes_precedence_over_github` | Pre-supplied `diff_text` short-circuits GitHub fetch |

### review server HTTP endpoint (7 tests) — `pytest packages/harness-tests/test_review_http.py`

| Test | What it proves |
|---|---|
| `test_http_review_endpoint_exists` | `POST /review` returns 200 for a valid diff |
| `test_http_review_returns_verdict_and_findings` | Response has `verdict`, `findings`, `summary` keys |
| `test_http_review_verdict_pass_on_clean_diff` | Clean diff → `verdict: pass` |
| `test_http_review_accepts_custom_task` | Optional `task` field accepted without error |
| `test_http_review_accepts_provider_override` | Optional `provider` field accepted without error |
| `test_http_review_missing_diff_text_returns_422` | Missing `diff_text` → 422 Unprocessable Entity |
| `test_http_review_agent_error_returns_500` | Agent failure (max retries exceeded) → 500 |
| `test_http_review_no_key_set_allows_all` | `REVIEW_API_KEY` unset → all requests allowed (dev mode) |
| `test_http_review_correct_key_allows_request` | Correct bearer token → 200 |
| `test_http_review_wrong_key_returns_401` | Wrong bearer token → 401 |
| `test_http_review_missing_header_returns_401` | No `Authorization` header when key required → 401 |
| `test_http_review_malformed_header_returns_401` | Header present but missing `Bearer ` prefix → 401 |

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

**Long-running tools (e.g. `bootstrap_architecture`)** exceed Claude Code's default 60s MCP timeout. Launch with an extended timeout:

```bash
MCP_TOOL_TIMEOUT=300000 claude   # 5 minutes
```

### Available MCP tools

All tools are visible in Claude Code as `mcp__ai-harness__<mcpjungle-name>`.

| Short name | MCPJungle name | Role | What it does |
|---|---|---|---|
| `review_diff` | `review_server__review_diff` | code_reviewer | Full code review via `CodeReviewerAgent` — lints + analyses diff, returns structured findings |
| `git_diff` | `diff_proxy__git_diff` | code_reviewer | Get a diff: passthrough text, GitHub PR (`pr_number`+`github_repo`), or local git (`repo_path`+base/head refs) |
| `run_linter` | `linter_stub__run_linter` | code_reviewer | Semgrep lint on diff additions; rules in `stub_servers/semgrep-rules.yml` |
| `coverage_report` | `linter_stub__coverage_report` | code_reviewer | Synthetic per-file coverage data (stub — real coverage not wired) |
| `repo_conventions_read` | `github_mcp__repo_conventions_read` | code_reviewer | Fetch `CONTRIBUTING.md`, coding standards, `.editorconfig` from a GitHub repo |
| `codebase_search` | `architect_stub__codebase_search` | architect | Search codebase for file/symbol patterns via GitHub API |
| `adr_read` | `architect_stub__adr_read` | architect | Read ADRs from `docs/adr/` in a GitHub repo |
| `architecture_review` | `review_server__architecture_review` | architect | Four-phase architectural analysis (recon → flow → abstraction → synthesis) |
| `bootstrap_architecture` | `review_server__bootstrap_architecture` | architect | Generate an `ARCHITECTURE.md` via four-phase analysis + doc render; needs `MCP_TOOL_TIMEOUT=300000` |
| `execute_architecture_check` | `review_server__execute_architecture_check` | architect | Run architecture invariant checks (stub — sandbox not yet wired) |
| `code_health_score` | `review_server__code_health_score` | architect | Radon cyclomatic complexity per file from GitHub API; returns 0–10 scores sorted worst-first |
| `codebase_hotspots` | `review_server__codebase_hotspots` | architect | Rank files in a repo by complexity hotspot risk; optional language filter |
| `logical_coupling` | `review_server__logical_coupling` | architect | Find files that historically co-change with a given file (GitHub commits API) |
| `issue_create` | `github_mcp__issue_create` | architect | File a GitHub issue with title, body, and optional labels |
| `runbook_read` | `sre_stub__runbook_read` | sre | Semantic pgvector search over runbooks; seed with `make seed-runbooks` |
| `log_search` | `sre_stub__log_search` | sre | Semantic pgvector search over log events; seed with `make seed-logs` |
| `observability_query` | `sre_stub__observability_query` | sre | Observability query (stub) |
| `shell_exec` | `sre_stub__shell_exec` | sre | Execute a shell command; requires a scoped `human_approval_token` |
| `skill_search` | `sre_stub__skill_search` | sre | TF-IDF lookup of proven remediation formulas from Dolt |

OPA enforces role boundaries: agents only receive a token for their own role and are blocked from cross-role tools at the policy layer.

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
├── eval-fixtures/         # Reviewer fixtures (diffs/ + labels/) and architect fixtures (architecture/)
├── test-fixtures/         # Committed test RSA key (jwt-test-key.pem) — dev/CI only
├── scripts/
│   └── skills_cli.py      # CLI for the skill-learning pipeline (token, pipeline, episodes, candidates, skills)
├── policies/              # OPA policy (harness.rego)
├── security/              # owasp-review.md — OWASP Agentic AI Top 10 review
├── docs/runbooks/         # 4 operational runbooks (agent-unresponsive, policy-rollback, cost-spike, bad-formula)
├── docker-compose.yml
└── .env.example
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full request flow and design decisions, and [CLAUDE.md](CLAUDE.md) for operational notes and gotchas.
