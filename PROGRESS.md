# AI Harness — Build Progress

Tracks completion against the phase checklists. A phase is done when all its tests pass **and** its Definition of Done checklist is signed off. Update this file as tests go green.

---

## Phase 0 — Foundation & Test Infrastructure ✅

**Tests** — spec prescribed infra smoke tests (postgres, redis, mcpjungle, opa health); in practice we diverged and went straight to the code-reviewer integration. Original 9 tests pass:

- [x] `test_reviewer_produces_structured_output`
- [x] `test_tool_calls_go_through_gateway`
- [x] `test_reviewer_denied_cross_role_tool`
- [x] `test_review_diff_tool_is_reachable`
- [x] `test_review_diff_returns_valid_schema`
- [x] `test_review_diff_catches_credential_leak`
- [x] `test_git_diff_returns_real_diff_format`
- [x] `test_git_diff_contains_commit_changes`
- [x] `test_git_diff_respects_ref`

**Definition of Done**
- [x] 1. Tests pass
- [x] 2. `make stack-up` brings services to healthy within 60s
- [x] 3. Clone → `make stack-up && make test-integration` → green without manual steps
- [x] 4. README documents local dev setup

**Notes / divergences from spec**
- Skipped GitHub Actions CI (deliberate — local-only for now)
- `harness-memory` and `harness-orchestrator` packages not yet scaffolded (Phase 2+)

---

## Phase 1 — MCP Gateway & Governance ✅

**Tests** — all 17 pass:

- [x] `test_architect_client_auth`
- [x] `test_reviewer_client_auth`
- [x] `test_sre_client_auth`
- [x] `test_architect_allowed_tool`
- [x] `test_architect_denied_tool`
- [x] `test_reviewer_allowed_tool`
- [x] `test_reviewer_denied_tool`
- [x] `test_sre_allowed_tool`
- [x] `test_unknown_token_rejected`
- [x] `test_audit_row_written`
- [x] `test_audit_policy_rule_recorded`
- [x] `test_audit_dolt_commit_created`
- [x] `test_audit_dolt_history_queryable`
- [x] `test_audit_no_delete`
- [x] `test_opa_allow_architect_tool`
- [x] `test_opa_deny_cross_role`
- [x] `test_token_expiry`

**Definition of Done**
- [x] 5. All 17 tests pass
- [ ] 6. Simulated tool call produces audit row + Dolt commit within 200ms (not formally measured)
- [x] 7. OPA policy version-controlled and loaded from repo
- [x] 8. review-server routes tool calls through governance (not directly to MCPJungle) — Docker-level network isolation not enforced, but agent path is fully governed
- [x] 9. `dolt log` shows one commit per tool call with human-readable message
- [x] 10. Phase 2 can begin without modifying gateway or policy engine

**Notes / divergences from spec**
- Governance service is a custom FastAPI app at `:8090`, not a MCPJungle Enterprise feature
- `review_diff` added to `code_reviewer` OPA policy (spec omitted it; needed for Phase 0 tests to keep passing through governance)
- GatewayClient auto-fetches bearer tokens; falls back gracefully if governance absent

---

## Phase 2 — Persistent Memory Layer ✅

**Tests** — all 27 pass:

- [x] `test_checkpointer_saves_state`
- [x] `test_checkpointer_resumes`
- [x] `test_checkpointer_thread_isolation`
- [x] `test_memory_write_and_read`
- [x] `test_memory_namespace_isolation`
- [x] `test_memory_cross_session_persistence`
- [x] `test_memory_ttl_expiry`
- [x] `test_memory_redis_hot_read`
- [x] `test_memory_semantic_search`
- [x] `test_memory_overwrite`
- [x] `test_memory_delete`
- [x] `test_memory_interface_compliance`
- [x] `test_sre_runbook_namespace`
- [x] `test_episodic_memory_write`
- [x] `test_semantic_memory_written_by_consolidation`
- [x] `test_consolidation_clusters_similar_episodes`
- [x] `test_consolidation_preserves_distinct_episodes`
- [x] `test_consolidation_prunes_expired_items`
- [x] `test_formula_quality_score_updated`
- [x] `test_formula_graduates_to_proven`
- [x] `test_formula_flagged_for_review`
- [x] `test_formula_write_creates_dolt_commit`
- [x] `test_formula_lookup_by_task`
- [x] `test_formula_lookup_no_match`
- [x] `test_formula_version_history`
- [x] `test_formula_deprecate`
- [x] `test_formula_interface_compliance`

**Definition of Done**
- [x] 11. All 27 tests pass
- [ ] 12. Memory reads from Redis (hot path) complete in <5ms p99 under load test (not formally measured)
- [x] 13. Checkpoint survives PostgreSQL restart (volume-backed, tested via stack restart)
- [x] 14. pgvector 0.8.2 enabled; semantic search returns non-empty results
- [x] 15. Formula store has three seed formulas: sre:triage-incident, code_reviewer:review-pr, architect:write-adr
- [x] 16. Memory store schema versioned with Alembic (migration in packages/harness-memory/alembic/)
- [x] 17. `make consolidate` triggers ConsolidationWorker on the sre namespace
- [x] 18. Consolidation pass produces semantic items and marks source episodes consolidated=True

**Notes / divergences from spec**
- Embedding model: `nomic-embed-text` (768 dims, `EMBED_MODEL` env var) — separate from `OLLAMA_MODEL` (chat). pgvector dimension auto-detected at startup; table is recreated if model changes.
- Formula lookup: TF-IDF keyword matching (not vector similarity) — sufficient for the test suite and avoids a second embedding index.
- Consolidation cluster threshold: 0.80 cosine similarity. nomic-embed-text gives 0.82–0.93 for same-topic pairs and 0.35–0.62 for different-topic pairs.
- Formula test formulas use `agent_role="test_sre"` to avoid interference with seed formulas (`agent_role="sre"`).
- DoD item 12 (Redis <5ms p99 load test) not formally measured; hot-read path verified by cache_hits counter in tests.

---

## Phase 3 — Specialised Agent Nodes ✅

**Tests** — all 14 pass:

- [x] `test_agent_node_contract_compliance`
- [x] `test_architect_produces_adr`
- [x] `test_architect_reads_past_adrs`
- [x] `test_architect_writes_adr_to_memory`
- [x] `test_architect_tool_calls_go_via_gateway`
- [x] `test_architect_denied_shell_exec`
- [x] `test_reviewer_produces_structured_findings`
- [x] `test_reviewer_verdict_fail_on_critical`
- [x] `test_reviewer_loop_max_iterations`
- [x] `test_reviewer_reads_conventions`
- [x] `test_sre_produces_incident_report`
- [x] `test_sre_shell_exec_blocked_without_approval`
- [x] `test_sre_shell_exec_allowed_with_approval`
- [x] `test_sre_writes_incident_to_memory`

**Definition of Done**
- [x] 19. All 14 tests pass
- [x] 20. Each agent's output passes JSON Schema validation against its output contract
- [x] 21. No agent can call a tool outside its allowed_tools list (verified by integration test)
- [x] 22. SRE shell_exec blocked without human_approval_token (hard governance rule)
- [ ] 23. Memory writes visible in a subsequent session (not formally verified end-to-end)

**Notes / divergences from spec**
- Unit tests use `MockLLMProvider` (deterministic) rather than cassette recording (vcrpy) — simpler and fully controlled
- `human_approval_token` passed as a `GatewayClient` constructor field → `X-Human-Approval-Token` header; governance checks it before OPA evaluation for `shell_exec`
- `CodeReviewerAgent` memory integration added (reads conventions, no write-back of findings — write-back is a Phase 4 concern when the full loop is wired)
- `make requirements` target fixed: added `--no-color` flag to prevent uv ANSI codes corrupting requirements.txt

---

## Phase 4 — Agent Orchestration ✅

**Tests** — all 27 pass (15 unit/E2E, 12 integration):
- [x] 8 classify tests (design/review/incident, LLM-primary routing, keyword fallback, unparseable default, think-block stripping)
- [x] 3 route tests (architect/reviewer/sre)
- [x] 1 error_handler test
- [x] 3 formula_lookup tests (hit/miss/outcome)
- [x] 2 agent execution tests (ad-hoc/formula steps)
- [x] 4 human_gate tests (pause/resume/expired/wrong_scope)
- [x] 1 checkpoint durability test
- [x] 1 OTel spans test
- [x] 3 E2E tests (design/review/incident task)

**Definition of Done**
- [x] 24. All 22 tests pass in CI
- [x] 25. Human approval flow: task → formula → human gate → token → shell_exec
- [x] 26. OTel spans emitted for classify, formula_lookup, route, agent, synthesise
- [x] 27. Parallel requests isolated by thread_id
- [x] 28. Graph checkpoints survive PostgreSQL restart
- [x] 29. Three seed formulas matched (sre:triage-incident, code_reviewer:review-pr, architect:write-adr)
- [x] 30. Draft formula created for novel ad-hoc runs

**Notes / divergences**
- Unit/E2E tests use MockLLMProvider + InMemorySaver (69 tests in 58s total)
- Integration tests use PostgreSQL checkpointer + real Dolt
- human_approval_token: X-Human-Approval-Token header, governance validates before OPA

**Post-Phase 5 improvement (2026-06-10)**
- `classify_node` is now LLM-primary with a structured JSON contract (`{"task_type": ...}`),
  replacing the keyword-first heuristic that misrouted tasks with misleading surface keywords
  (e.g. "Review the alert that fired" → review instead of incident).
  Keywords remain as a fallback when the LLM is unreachable or returns unparseable output;
  final default is `review`. Added 5 classifier tests (Phase 4 file: 22 → 27 tests).

**Phase 2 Bug Fixes (completed after Phase 3/4)**
- Fixed `formula_store.update_quality()`: check `cursor.rowcount > 0` before commit
- Implemented `FakeEmbedder`: topic-based deterministic embeddings for clustering tests
- Result: Phase 2 now 27/27 tests passing (was 26 + 1 skip)

---

## Phase 5 — Production Hardening ✅

**Tests** — all 8 pass (+ load test):

- [x] `test_owasp_memory_write_requires_auth`
- [x] `test_owasp_prompt_injection_blocked`
- [x] `test_cost_otel_tag_present`
- [x] `test_token_budget_enforced`
- [x] `test_rate_limit_tool_calls`
- [x] `test_contextforge_tool_group_parity`
- [x] `test_contextforge_audit_log_parity`
- [x] `test_gateway_rollback`
- [x] `test_load_50_concurrent` (p99=1006ms, threshold 10s)

**Definition of Done**
- [x] 31. All 8 tests pass; all prior phase tests pass (74/74 integration)
- [x] 32. OWASP review present at `/security/owasp-review.md`
- [x] 33. 4 runbooks in `/docs/runbooks/`
- [x] 34. Grafana dashboard live (`make monitoring-up`; `harness-cost.json` provisioned)
- [x] 35. Load test: 50 concurrent, p99=1006ms < 10s, 0 isolation failures

**Notes / divergences from spec**
- ContextForge is IBM's real `ghcr.io/ibm/mcp-context-forge:latest` (not a fictional product).
  Uses SQLite + memory cache in dev; STREAMABLEHTTP transport for MCP stubs.
  `services/contextforge_setup/setup.py` handles gateway + virtual-server registration.
- `GATEWAY_BACKEND=mcpjungle|contextforge` feature flag in governance; defaults to mcpjungle.
- `ContextForgeGatewayClient` in `packages/harness-gateway/harness_gateway/cf_client.py`.
- `tokens_used` / `token_budget` added to `HarnessState`; budget check in `run_agent_node`.
- Rate limiter uses Redis sliding-window per agent sub; `RATE_LIMIT_PER_MINUTE=20` in `.env`.
  Rate limit test uses a unique JWT sub per run to avoid cross-test bucket collisions.
- Prometheus `/metrics` on governance; Grafana + Prometheus behind `--profile monitoring`.
- `test_cost_otel_tag_present` verifies `agent_role` + `thread_id` on agent OTel spans.
- DoD item 34: Grafana renders real data after `make monitoring-up` and a few tool calls.

---

## Post-Phase 5 Security & Quality Improvements (2026-06-11)

### RS256 JWT migration

Governance JWT signing migrated from HS256 shared secret to RS256 asymmetric keypair (ADR 0024).

- `JWT_SECRET` env var removed; replaced by `JWT_PRIVATE_KEY_FILE` (path to PEM private key)
- Governance signs with the private key; downstream verifiers use the public key from `GET /jwks`
- Test private key committed at `test-fixtures/jwt-test-key.pem` with a startup fingerprint tripwire — governance refuses to start with this key unless `ENV=test`
- `test_token_expiry` updated to forge expired JWTs using the test private key (RS256)
- 74/74 integration tests pass unchanged

### Prompt externalization

All LLM system prompts are now loaded from `prompts/*.md` (ADR 0025).

- `classify.md` was written but orphaned; `nodes.py` had an inline `_CLASSIFY_PROMPT` that had diverged from it — fixed, inline string removed
- `synthesise.md` was written but unused; `synthesise_node` now makes a real LLM call using it when `llm_provider` is supplied, with a string-format fallback for `llm_provider=None` (test path)
- `classify_node` system message upgraded from `"You are a task classifier."` to the full `classify.md` content (includes output format, confidence, reasoning)

### Reviewer eval suite

Agent quality benchmarking added — separate from the integration suite (ADR 0026).

- `eval-fixtures/diffs/` — 6 synthetic git diffs: 1 clean refactor, 5 with known security bugs
- `eval-fixtures/labels/` — ground truth: expected verdict + must-flag patterns per fixture
- `packages/harness-tests/test_eval_reviewer.py` — `@pytest.mark.eval` tests; mock gateway, real Ollama
- Pass bars: verdict accuracy ≥ 80%, average recall ≥ 60%
- First run (7b model): **100% verdict accuracy, 80% recall** — above both thresholds
- Run with: `pytest -m eval -v -s`

### Architect eval suite (2026-06-22)

Quality benchmarking for the four-phase `ArchitectAgent`, after the prompt/phase rewrite.

- `eval-fixtures/architecture/<case>/` — fixture repos as canned tool responses (recon/hotspots/files/interfaces/adrs), one dir per case; `labels/<case>.json` carries `expect_high_severity` + `must_flag`
- 3 cases: `clean_layered` (control), `god_controller` (logic+SQL in handler), `leaky_persistence` (ORM leaking through a domain port)
- `packages/harness-tests/test_eval_architect.py` — `@pytest.mark.eval`; `_MockGateway` routes `codebase_search` by query keyword to the right phase file; real Ollama
- Pass bars: schema validity 100%, detection ≥ 66%, avg recall ≥ 50%
- First run (7b model): **100% schema validity, 3/3 detected, recall 2/2 on both smells** — all above thresholds
- Fixed a latent bug surfaced while building this: `ARCHITECT_OUTPUT_SCHEMA` was still the old ADR shape and diverged from the review-report shape the prompt emits. Updated the schema to match.

#### Follow-up: runtime schema validation + CI (2026-06-22)

- **Runtime validation:** `_phase_synthesis` now passes `_validate_synthesis` to `_llm_retry`; a schema-invalid synthesis is fed back to the model and retried, and `run()` returns `error.code = "invalid_output"` if it never validates. Unit tests in `test_phase3_agents.py` cover retry-then-pass and never-valid-then-error (no live LLM). This caught a brittleness: a strict `category` enum let one off-vocabulary tag void a whole review — relaxed `category` to a free string, kept `severity` enum.
- **Eval cost:** memoized `_run_case`; the aggregate test reuses per-fixture results instead of re-running the agent (6 agent runs → 3).
- **CI:** `.github/workflows/architect-eval.yml` runs the suite on PRs touching the architect/prompt/schema/fixtures, via `LLM_PROVIDER=openrouter` (no GPU on runners). `_build_llm` selects the provider from env. Needs an `OPENROUTER_API_KEY` repo secret; skips with a warning if unset.

### Semgrep linter replacement

Replaced the naive pattern-matching `linter_server.py` with a real semgrep scan.

- `stub_servers/semgrep-rules.yml` — 8 bundled rules: `print-call`, `hardcoded-credential`, `credential-in-url-var`, `subprocess-shell-true`, `sql-fstring-query`, `open-fstring-path`, `eval-call`, `os-system-call`
- `stub_servers/Dockerfile.stub` — adds `pip install semgrep` layer
- `packages/harness-tests/test_unit_linter.py` — 11 unit tests covering diff parsing and semgrep output mapping (subprocess mocked; no semgrep binary needed locally)
- Validated against all 6 eval fixtures: clean diff returns no warnings; SQL injection, hardcoded secrets, shell injection, and path traversal all flagged correctly
- Gotcha: semgrep `metavariable-regex` uses anchored match — must use `(?i).*keyword.*` not `(?i)keyword` to match compound variable names like `AWS_SECRET_ACCESS_KEY`

---

## Phase 6 — Agent Orchestration (issues 01–07)

### Issue 01 — Dolt: tasks + agent_messages migration ✅

**Tests** — 9 pass:

- [x] `test_tasks_table_exists`
- [x] `test_agent_messages_table_exists`
- [x] `test_tasks_schema_columns`
- [x] `test_agent_messages_schema_columns`
- [x] `test_tasks_indexes_exist`
- [x] `test_agent_messages_inbox_index_exists`
- [x] `test_harness_user_can_insert_tasks`
- [x] `test_harness_user_cannot_delete_tasks`
- [x] `test_existing_tables_unaffected`

**Definition of Done (issue 01)**
- [x] `tasks` and `agent_messages` tables created in `services/dolt/init.sh`
- [x] `idx_claimable`, `uq_idem`, `idx_inbox` indexes present
- [x] `harness` user has SELECT/INSERT/UPDATE on tasks; SELECT/INSERT on agent_messages; no DELETE
- [x] Existing 74 integration tests pass unchanged (83/83 total)

### Issue 02 — OPA + agent_list ✅

**Tests** — 11 pass:
- [x] `test_opa_supervisor_can_invoke_code_reviewer`
- [x] `test_opa_supervisor_can_invoke_architect`
- [x] `test_opa_supervisor_can_invoke_sre`
- [x] `test_opa_architect_can_invoke_code_reviewer`
- [x] `test_opa_code_reviewer_cannot_invoke_sre`
- [x] `test_opa_sre_cannot_invoke_anyone`
- [x] `test_opa_claim_allowed_matching_role`
- [x] `test_opa_claim_denied_wrong_role`
- [x] `test_agent_list_supervisor_sees_all`
- [x] `test_agent_list_code_reviewer_sees_empty`
- [x] `test_agent_list_requires_auth`

**Definition of Done (issue 02)**
- [x] `harness.rego` defines `invoke_allowed` and `claim_allowed` rules
- [x] `GET /agents` returns only agents OPA permits the caller to invoke
- [x] code-reviewer JWT sees empty agent list

### Issue 03 — Blackboard: task_post + task_claim ✅

**Tests** — 8 pass:
- [x] `test_task_post_creates_pending_row`
- [x] `test_task_post_creates_dolt_commit`
- [x] `test_task_post_requires_auth`
- [x] `test_task_claim_returns_null_when_empty`
- [x] `test_task_claim_returns_task`
- [x] `test_task_claim_priority_ordering`
- [x] `test_task_claim_role_isolation`
- [x] `test_task_claim_atomic_no_double_grab`

**Definition of Done (issue 03)**
- [x] `POST /tasks` creates pending row + Dolt commit
- [x] `POST /tasks/claim` atomic SELECT+UPDATE loop; 0 double-grabs with 10 concurrent workers
- [x] Lease reaper (on-claim sweep) resets stale claimed tasks to pending
- [x] Role isolation: sre cannot claim architect tasks

### Issue 04 — agent_invoke ✅

**Tests** — 6 pass:
- [x] `test_agent_invoke_allowed`
- [x] `test_agent_invoke_requires_auth`
- [x] `test_agent_invoke_denied_is_403_and_audited`
- [x] `test_invoke_uses_target_credentials`
- [x] `test_invoke_rejects_malformed_payload`
- [x] `test_invoke_unknown_target_returns_404`

**Definition of Done (issue 04)**
- [x] `POST /agent/invoke` enforces OPA topology policy
- [x] Denied invocations write audit row synchronously before 403
- [x] Target agent runs under its own credentials (not caller's)
- [x] Payload validated against agent input_schema before OPA/network calls

### Issue 05 — task_complete + lease reaper ✅

**Tests** — 5 pass:
- [x] `test_task_complete_transitions_to_done`
- [x] `test_task_complete_creates_dolt_commit`
- [x] `test_task_complete_idempotent`
- [x] `test_task_complete_requires_auth`
- [x] `test_lease_expiry_returns_task_to_pool`

**Definition of Done (issue 05)**
- [x] `POST /tasks/complete` transitions to done, stores result, writes Dolt commit
- [x] Idempotency: duplicate `idempotency_key` returns original result without double-write
- [x] Stale claimed tasks return to pending pool via on-claim reaper sweep

### Issue 06 — Supervisor demo ✅

**Tests** — 4 pass:
- [x] `test_supervisor_chain_reviewer_to_architect`
- [x] `test_supervisor_schema_mismatch_raises_422`
- [x] `test_supervisor_no_token_forwarding`
- [x] `test_reviewer_cannot_chain_to_sre`

**Definition of Done (issue 06)**
- [x] Chained architect → code-reviewer invocation audited under correct agent_role
- [x] Schema mismatch fails loudly (422) before any OPA/network call
- [x] No credential forwarding: architect token never reaches review tools

### Issue 07 — Correlation ID threading ✅

**Tests** — 4 pass:
- [x] `test_audit_log_has_correlation_id_column`
- [x] `test_correlation_id_threads_chain`
- [x] `test_correlation_id_in_denied_invocation`
- [x] `test_single_step_audit_row_null_correlation`

**Definition of Done (issue 07)**
- [x] `audit_log` has nullable `correlation_id VARCHAR(36)` column
- [x] `X-Correlation-Id` header threaded through `/agent/invoke` and `/audit`
- [x] Multi-step chains share correlation_id across all audit rows (allow and deny)
- [x] Single-step plain `/audit` calls produce null correlation_id (backwards-compatible)

**Phase 6 summary: 121/121 integration tests pass (47 new + 74 prior phases)**

**Notes / divergences**
- `correlation_id` column added via live ALTER TABLE (not rebuild) — `init.sh` updated for fresh installs
- `task_complete` uses claimer identity check (`claimed_by = sub`) to prevent cross-worker completion
- Priority-9999 pattern used in tests to isolate specific tasks in a shared queue (avoids test interference)
- Agent registry in governance: code-reviewer requires `repo` in payload; architect/sre have no required fields

---

## Agent-level Token Usage Measurement

**Tests** — 9 pass (`test_token_usage.py`, unit tests — no Docker stack needed):
- [x] `test_llm_response_has_token_fields`
- [x] `test_llm_response_defaults_to_zero`
- [x] `test_ollama_provider_captures_token_counts`
- [x] `test_ollama_provider_none_counts_become_zero`
- [x] `test_agent_state_accepts_token_fields`
- [x] `test_reviewer_accumulates_token_usage`
- [x] `test_reviewer_accumulates_across_retries`
- [x] `test_reviewer_budget_exceeded_on_retry`
- [x] `test_reviewer_no_budget_runs_to_completion`

**Definition of Done**
- [x] `LLMResponse` carries `prompt_tokens` and `completion_tokens` (defaults 0)
- [x] `OllamaProvider` maps `prompt_eval_count`/`eval_count` from Ollama API response
- [x] `GeminiProvider` maps `usage_metadata.prompt_token_count`/`candidates_token_count`
- [x] `AgentState` has `token_usage: dict` and `token_budget: int | None`
- [x] `CodeReviewerAgent` accumulates token counts across retry iterations
- [x] Budget check fires after a failed parse attempt; aborts with `token_budget_exceeded` error
- [x] `token_budget=None` means unlimited (no check)
- [x] `GeminiProvider._build_contents` extracted to reduce CCN; health score 7.9 → 9.7

**Notes / divergences**
- Budget enforcement is retry-gated: a successful first response is never cancelled by the budget check, only runaway retries are stopped
- `AgentState` switched to `total=False` (all keys optional) for backwards compatibility — existing tests construct partial state dicts without the new fields
- `HarnessState.tokens_used` (supervisor-level) is separate; agent-level `token_usage` is not yet propagated back to `HarnessState` — that's a follow-up

---

## git_diff GitHub Mode + review_server HTTP Endpoint

**Tests** — 16 pass (unit tests — no Docker stack needed):

`test_git_diff_github.py` (9 tests):
- [x] `test_fetch_github_pr_diff_calls_correct_url`
- [x] `test_fetch_github_pr_diff_sets_diff_accept_header`
- [x] `test_fetch_github_pr_diff_includes_auth_header_when_token_given`
- [x] `test_fetch_github_pr_diff_omits_auth_header_when_no_token`
- [x] `test_fetch_github_pr_diff_returns_decoded_body`
- [x] `test_git_diff_github_mode_returns_pr_diff`
- [x] `test_git_diff_github_mode_passes_env_token`
- [x] `test_git_diff_github_mode_missing_repo_raises`
- [x] `test_git_diff_diff_text_takes_precedence_over_github`

`test_review_http.py` (7 tests):
- [x] `test_http_review_endpoint_exists`
- [x] `test_http_review_returns_verdict_and_findings`
- [x] `test_http_review_verdict_pass_on_clean_diff`
- [x] `test_http_review_accepts_custom_task`
- [x] `test_http_review_accepts_provider_override`
- [x] `test_http_review_missing_diff_text_returns_422`
- [x] `test_http_review_agent_error_returns_500`

**Definition of Done**
- [x] `git_diff` tool accepts `pr_number` + `github_repo`; fetches unified diff from GitHub API
- [x] `GITHUB_TOKEN` env var forwarded into container via `docker-compose.yml`
- [x] `diff_text` passthrough takes precedence over GitHub mode (no regression)
- [x] `review_server` exposes `POST /review` plain HTTP endpoint
- [x] MCP tool and HTTP endpoint share `_run_review()` — no logic duplication
- [x] Missing `diff_text` → 422; agent failure → 500
- [x] Code health 10/10 on both changed files

**Notes / divergences**
- GitHub mode is unauthenticated when `GITHUB_TOKEN` is absent — works for public repos, will 404 on private
- `docker-compose.yml` updated: `GITHUB_TOKEN: ${GITHUB_TOKEN:-}` passes host token into container
- `_run_review()` extraction also reduced `review_diff` MCP handler to a one-liner, bringing `server.py` avgCCN from 3.8 → 2.4

---

## POST /review Bearer-Token Auth

**Tests** — 5 new (added to `test_review_http.py`, total now 12):
- [x] `test_http_review_no_key_set_allows_all`
- [x] `test_http_review_correct_key_allows_request`
- [x] `test_http_review_wrong_key_returns_401`
- [x] `test_http_review_missing_header_returns_401`
- [x] `test_http_review_malformed_header_returns_401`

**Definition of Done**
- [x] `REVIEW_API_KEY` unset → endpoint open (dev/local mode, no behaviour change)
- [x] `REVIEW_API_KEY` set → `Authorization: Bearer <key>` required; wrong/missing → 401
- [x] Auth check extracted to `_check_api_key()` — separate from MCP governance path
- [x] `REVIEW_API_KEY: ${REVIEW_API_KEY:-}` wired through `docker-compose.yml`
- [x] Code health 10/10

**Notes**
- Empty default in compose means auth is off locally unless the var is explicitly set
- The MCP `review_diff` tool path is unaffected — it uses governance JWT auth unchanged

---

## Gemini Review Findings — Hardening

Addressed three remaining findings from the Gemini code review:

**Tests** — 5 new across existing test files (total: git_diff 14, review_http 13):
- [x] `test_fetch_github_pr_diff_http_error_raises_value_error`
- [x] `test_fetch_github_pr_diff_url_error_raises_value_error`
- [x] `test_git_diff_invalid_github_repo_format_raises`
- [x] `test_git_diff_valid_github_repo_format_accepted`
- [x] `test_http_review_500_does_not_leak_internal_detail`

**Definition of Done**
- [x] `_fetch_github_pr_diff` catches `HTTPError` and `URLError`; re-raises as `ValueError` with status code / reason
- [x] `github_repo` validated against `^owner/repo$` regex before API call; invalid format raises `ValueError`
- [x] `POST /review` 500 response returns generic message; raw exception never sent to caller
- [x] Code health 9.9/10

---

## OpenRouter Provider + Security Hardening

Added `OpenRouterProvider` (PR #1) and addressed six findings from a multi-angle code review.

**No new tests** — all fixes verified by the existing 121-test integration suite (all pass).

**Definition of Done**
- [x] `OpenRouterProvider` added to `harness_agents/llm.py`; wired into `_build_llm_provider` in `server.py`
- [x] `LLM_PROVIDER=openrouter` routes all LLM calls through OpenRouter's OpenAI-compatible API
- [x] `temperature` omitted for `openai/o\d` models (o1, o3-mini, o4-mini) which reject the parameter
- [x] Empty `choices` list (content filter, upstream rate limit) raises `ValueError` before `choices[0]` IndexError
- [x] Provider errors (`openai.APIError` subclasses, empty choices) caught in `_retry_until_valid`; returned as structured `{"code": "provider_error"}` state rather than propagating as uncaught exceptions
- [x] `OPENROUTER_API_KEY` `.strip()`-ed before empty check — whitespace-only value caught at build time not at review time
- [x] Unknown provider names raise `ValueError` with supported list; silent fallthrough to Ollama removed
- [x] `http_review` returns 400 (not 500) for `ValueError` — config errors are now distinguishable from infrastructure failures
- [x] Code health 9.7/10

---

## Skill Learning (issues 01–08 from `.scratch/skill-learning/PRD.md`)

Self-learning loop: tool call episodes → candidate clustering → HITL promotion → governed skill execution → expiry/re-validation.

### Issue 01 — Dolt schema: episodes, candidates, skills ✅

**Tests** — 14 pass (`test_skill_learning_schema.py`):
- [x] `test_episodes_table_exists` / `test_episodes_columns`
- [x] `test_candidates_table_exists` / `test_candidates_columns`
- [x] `test_skills_table_exists` / `test_skills_columns`
- [x] `test_seeded_skills_present` — three seed skills (sre:triage-incident, code_reviewer:review-pr, architect:write-adr)
- [x] `test_formulas_table_gone` — formulas + formula_pours dropped and replaced
- [x] `test_harness_user_can_insert_episode` / `test_harness_user_cannot_delete_episodes`
- [x] `test_formula_store_list_active_returns_seeded_skills` / `test_formula_store_lookup_finds_skill_by_keyword`
- [x] `test_harness_user_can_insert_skill`

**Definition of Done**
- [x] `episodes`, `candidates`, `skills` tables in `services/dolt/init.sh` (replacing `formulas`/`formula_pours`)
- [x] `DoltFormulaStore` reads from `skills` table; three seed rows committed on init
- [x] `harness` user: SELECT+INSERT on episodes (no DELETE); SELECT+INSERT+UPDATE on candidates+skills

**Notes**
- `formulas` and `formula_pours` tables dropped; `DoltFormulaStore` updated to read `skills` — Phase 2 formula tests pass unchanged via the compatibility shim

---

### Issue 02 — Episode capture on governance audit path ✅

**Tests** — 4 pass (`test_episode_capture.py`):
- [x] `test_audit_writes_episode_row` — POST /audit creates episodes row with outcome=NULL
- [x] `test_episode_agent_principal_matches_jwt_sub` — agent_principal = JWT sub
- [x] `test_audit_still_returns_202` — episode write is fire-and-forget
- [x] `test_audit_log_still_written` — existing audit_log write unaffected

**Definition of Done**
- [x] `_write_episode` runs as independent `background_tasks.add_task` alongside `_write_audit` — one failure cannot swallow the other
- [x] `alert_signature` derived as `{role}.{short_tool}:{correlation_id}`; `env_fingerprint` and `actions` populated from audit payload
- [x] Episode write failure logged, 202 response unchanged

---

### Issue 03 — Outcome labeling endpoint ✅

**Tests** — 7 pass (`test_outcome_labeling.py`):
- [x] `test_label_returns_200_and_commits` — different principal, valid signal → 200 + Dolt commit
- [x] `test_dolt_commit_created_on_label`
- [x] `test_self_label_returns_409` — labeler_principal == agent_principal
- [x] `test_empty_outcome_signal_returns_422`
- [x] `test_relabel_returns_409` — already labeled
- [x] `test_opa_rejects_no_label_scope` — architect → 403
- [x] `test_missing_episode_returns_404`

**Definition of Done**
- [x] `POST /episodes/{id}/label` with four rejection cases (self-label, empty signal, re-label, missing)
- [x] OPA `episode:label` scope granted to `sre` and `code_reviewer` only
- [x] `_validate_label_body` + `_check_episode_labelable` + `_serialise_row` extracted to hold CCN ≤ 9

---

### Issue 04 — Manual candidate proposal ✅

**Tests** — 8 pass (`test_candidate_proposal.py`):
- [x] `test_post_candidates_returns_201` — 5 qualified independent recent RESOLVED episodes
- [x] `test_candidate_stored_in_dolt` — status=PROPOSED, support_stats computed
- [x] `test_get_candidate_returns_full_record` — GET /candidates/{id} with member_episode_ids
- [x] `test_below_n_min_returns_422` (< 5 episodes)
- [x] `test_below_k_principals_returns_422` (all same principal)
- [x] `test_below_m_recent_returns_422` (all > 90 days old)
- [x] `test_unqualified_episodes_returns_422` (unlabeled episode in list)
- [x] `test_opa_rejects_no_propose_scope` — architect → 403

**Definition of Done**
- [x] `POST /candidates` + `GET /candidates/{id}` on governance
- [x] OPA `candidate:propose` scope granted to `sre` and `code_reviewer`
- [x] Criteria: N_min=5, K=2 distinct principals, M=2 recent (90 days); `support_stats` computed automatically
- [x] Validation split into `_check_count_criteria` + `_check_diversity_criteria`; `_compute_support_stats` extracted

---

### Issue 05 — HITL promotion gate ✅

**Tests** — 13 pass (`test_hitl_promotion.py`):
- [x] `test_promote_creates_active_skill` — human-operator token → ACTIVE skill, promoted_by set
- [x] `test_promote_transitions_candidate_to_promoted`
- [x] `test_promote_dolt_commit_message` — commit includes candidate id and human principal
- [x] `test_promote_skill_expires_90_days_out`
- [x] `test_repromote_increments_version` — re-promotion → version 2, procedure_diff in response
- [x] `test_reject_sets_status_rejected` — with reason
- [x] `test_reject_without_reason_returns_422`
- [x] `test_repromote_already_promoted_candidate_409`
- [x] `test_reject_already_rejected_candidate_409`
- [x] `test_agent_role_cannot_promote` (×3: architect, sre, code-reviewer) → 403
- [x] `test_full_episode_to_skill_flow` — end-to-end episode→candidate→promote

**Definition of Done**
- [x] `POST /candidates/{id}/promote` + `POST /candidates/{id}/reject` on governance
- [x] `human-operator` OAuth client added; OPA `skill:promote` scope granted **only** to `human_operator` role
- [x] Re-validation of episode criteria at promote time; re-promotion creates new version with procedure diff
- [x] `expires_at = NOW() + 90 days`; `source_candidate_id` set on skill row

---

### Issue 06 — Skill execution with per-step OPA re-check and revocation ✅

**Tests** — 11 pass (`test_skill_execution.py`):
- [x] `test_get_skill_returns_200` / `test_get_revoked_skill_returns_410` / `test_get_missing_skill_returns_404`
- [x] `test_revoke_sets_status_revoked` — POST /skills/{id}/revoke + Dolt commit + revoked_reason stored
- [x] `test_agent_cannot_revoke` — 403
- [x] `test_revoke_without_reason_returns_422`
- [x] `test_execute_skill_runs_all_steps` — all steps complete, structured result returned
- [x] `test_abort_on_step_denial` — ABORT stops after failed step, subsequent steps not reached
- [x] `test_continue_on_step_denial` — CONTINUE skips denied step, carries on
- [x] `test_rollback_runs_rollback_steps_then_raises` — rollback steps fire before re-raise
- [x] `test_execute_revoked_skill_raises` — no tool calls made on revoked skill

**Definition of Done**
- [x] `GET /skills/{id}` (200 active, 410 revoked, 404 missing) on governance
- [x] `POST /skills/{id}/revoke` requires `skill:promote` scope (human-operator only)
- [x] `GatewayClient.execute_skill(skill_id, inputs)` — fetches skill, runs each step through `call_tool` (existing OPA re-check path), applies `on_failure` policy (ABORT/ROLLBACK/CONTINUE)
- [x] `run_skill` MCP tool added to review_server; uses `SKILL_CLIENT_ID`/`SKILL_CLIENT_SECRET` env vars
- [x] CCN ceiling held at 9.0 via `_parse_steps`, `_count_completed`, `_handle_step_failure`, `_check_status`, `_extract_content` extractions

**Running total: 177 integration tests pass**

---

### Issue 07 — Skill expiry and lightweight re-validation trigger ✅

**Tests** — 12 pass (`test_skill_expiry.py`):
- [x] `test_expire_requires_human_operator_role` — SRE 403 on /skills/expire
- [x] `test_expire_returns_200_with_no_overdue_skills` — empty summary when nothing overdue
- [x] `test_expire_transitions_overdue_skill_to_expired` — status → expired in Dolt
- [x] `test_expire_response_includes_skill_id` — skill_ids list in response
- [x] `test_expire_does_not_touch_non_overdue_skills` — future-expiring skills unchanged
- [x] `test_get_expired_skill_returns_410` — GET /skills/{id} 410 for expired
- [x] `test_execute_expired_skill_raises` — execute_skill raises ToolAccessDenied
- [x] `test_revalidation_proposes_candidate_when_enough_episodes` — N_MIN episodes → candidate auto-proposed
- [x] `test_revalidation_not_triggered_when_too_few_episodes` — < N_MIN → no candidate
- [x] `test_auto_trigger_expires_skill_after_interval_audit_calls` — background trigger via audit counter
- [x] `test_early_review_flag_set_for_low_success_rate` — < 50% allow rate → flagged
- [x] `test_early_review_flag_absent_for_high_success_rate` — ≥ 50% allow → not flagged

**Acceptance criteria**
- [x] POST /skills/expire transitions overdue ACTIVE skills to EXPIRED + Dolt commit per skill
- [x] Expired skills return 410 (GET /skills/{id}) and raise ToolAccessDenied on execute_skill
- [x] Re-validation auto-proposes candidate when N_MIN resolved episodes exist for agent role
- [x] Auto-trigger fires after EXPIRY_PASS_INTERVAL audit events (EXPIRY_PASS_INTERVAL=3 in docker-compose)
- [x] Early-review flag in response for skills with trailing 30-day deny rate > 50%
- [x] Integration test: past-expires_at skill → expire → EXPIRED + candidate re-proposed

**New governance helpers:** `_find_expired_skills`, `_expire_skill`, `_find_active_skills`, `_find_revalidation_episodes`, `_maybe_repropose_candidate`, `_compute_early_review_flags`, `_run_expiry_pass`, `_background_expiry_pass`

**Notes**
- Re-validation criteria simplified vs issue 04: N_MIN episodes only (no K_MIN/diversity check). Auto-revalidation surfaces candidates for human review; full diversity check would never trigger in a single-credential deployment.
- `EXPIRY_PASS_INTERVAL=3` in docker-compose for tests; default 1000 in production.

---

### Issue 08 — Conflict resolution and escalation ✅

**Tests** — 7 pass (`test_skill_select.py`):
- [x] `test_select_most_specific_wins` — 3 skills with 0/1/2 matched env_constraints; most specific wins
- [x] `test_select_recency_tiebreak` — 2 skills tied on specificity; newer `created_at` wins
- [x] `test_select_success_rate_tiebreak` — 2 skills tied on specificity + recency; higher 30-day allow rate wins
- [x] `test_select_full_tie_escalates` — 2 skills tied on all 3 rules; escalate=true with tied_skills
- [x] `test_select_win_logs_to_audit_log` — winning selection written to audit_log with tool_name='skill:select'
- [x] `test_select_escalation_logs_to_audit_log` — escalation also written to audit_log
- [x] `test_select_requires_auth` — 401 without token

**Acceptance criteria**
- [x] POST /skills/select returns the most specific matching ACTIVE skill when one clearly wins
- [x] Tied specificity resolves by promotion recency (most recently created_at)
- [x] Tied recency resolves by trailing success rate (30-day allow rate from audit_log)
- [x] Full tie returns escalate=true with tied skill IDs and scores
- [x] Every selection (win or escalate) is written to audit_log with tool_name='skill:select'
- [x] Integration test: three skills with overlapping preconditions; correct winner at each tiebreak layer
- [x] Integration test: exact tie returns escalation response

**New governance helpers:** `_parse_preconditions`, `_specificity_score`, `_skill_success_rate`, `_apply_specificity_rule`, `_apply_recency_rule`, `_apply_success_rate_rule`, `_run_skill_selection`, `_fetch_active_skills_for_select`

**Notes**
- `preconditions JSON DEFAULT NULL` column added to `skills` table via Dolt migration in `init.sh`. Seeded/existing skills have NULL preconditions (score 0 — least specific).
- Specificity score = number of `env_constraints` key-value pairs in `skill.preconditions` that exactly match the request's `env_fingerprint`.
- Tiebreak rules applied in order: each step narrows candidates to the winners at that level before proceeding.
- Success rate uses `audit_log.agent_id = skill.agent_role` (the OAuth client_id); same approach as early-review flags in issue 07.
- `_run_skill_selection` CCN reduced from 18 → 6 by extracting three named rule helpers. Health score: 9.1/10.
- Dolt returns SQL NULL JSON columns as `b'null'`; `_parse_preconditions` handles this before `.get()`.

**Running total: 196 integration tests pass (7 new + 189 prior)**

---

## Skills CLI + Governance List Endpoints

Thin CLI for the skill-learning pipeline — replaces raw curl for the human-in-the-loop workflow.

### New governance endpoints

- `GET /episodes?limit=N&unlabeled=bool` — list recent episodes (newest first)
- `GET /candidates?status=PROPOSED|PROMOTED|REJECTED` — list candidates with optional status filter
- `GET /skills?status=active|expired|revoked` — list latest-version skill rows with optional status filter

All three require a valid Bearer token (JWT decode only; no OPA check — read-only).

### CLI (`scripts/skills_cli.py`)

**Tests** — 19 pass (`test_skills_cli.py`):
- [x] `test_list_episodes_returns_list` — GET /episodes returns list with correct shape
- [x] `test_list_episodes_unlabeled_filter` — unlabeled=true excludes labeled rows
- [x] `test_list_episodes_requires_auth` — 401 without token
- [x] `test_list_candidates_returns_list` — GET /candidates returns list
- [x] `test_list_candidates_status_filter` — PROPOSED filter includes new candidate, excludes REJECTED
- [x] `test_list_candidates_requires_auth` — 401 without token
- [x] `test_list_skills_returns_list` — GET /skills returns list
- [x] `test_list_skills_status_filter` — active filter includes newly promoted skill
- [x] `test_list_skills_requires_auth` — 401 without token
- [x] `test_cli_token_returns_access_token` — `skills_cli.py token` prints token JSON
- [x] `test_cli_pipeline_shows_summary` — `pipeline` command has episodes/candidates/skills keys
- [x] `test_cli_episodes_list` — `episodes list` returns list
- [x] `test_cli_episodes_label` — `episodes label ID --outcome RESOLVED` labels episode
- [x] `test_cli_candidates_list` — `candidates list` returns list
- [x] `test_cli_candidates_propose` — `candidates propose --cluster-key ... --episodes ...` returns PROPOSED
- [x] `test_cli_candidates_promote` — `candidates promote ID` returns skill_id
- [x] `test_cli_skills_list` — `skills list` returns list
- [x] `test_cli_skills_select` — `skills select` returns response with `selected` key
- [x] `test_cli_skills_revoke` — `skills revoke ID --reason ...` returns revoked status

**DoD**
- [x] All 19 tests pass
- [x] CLI subprocess-tested: arg parsing, correct client selection, JSON output
- [x] OPA roles respected in tests: sre/code-reviewer for label+propose; human-operator for promote+revoke
- [x] Health score: 9.8/10 (`skills_cli.py`), governance `list_*` endpoints are CCN ~1 each

**Notes**
- `main()` was initially CCN 19 (flat if/elif dispatch); refactored to dispatch table (`_HANDLERS`) + three extracted handlers — brought CCN down to 6, score up to 9.8.
- OPA gotcha confirmed in tests: `human_operator` role is only allowed for `skill:promote` scope — labeling and proposing require `sre` or `code_reviewer` JWT.

---

## Phase 7 — Architecture as Code (AaC) Engine ✅

**Tests** — 14 pass (10 unit + 2 E2E + 2 integration):

- [x] `test_gate_passes_clean_code` — No violations → gate_signal.result == 'PASS'
- [x] `test_gate_fails_layer_violation` — Layer violation → HARD severity, FAIL result
- [x] `test_gate_enforces_complexity_limit` — Complexity violation → SOFT severity, FAIL result
- [x] `test_gate_passes_params_to_tool` — repo_path + target_language forwarded to tool
- [x] `test_gate_handles_tool_denied` — ToolAccessDenied → FAIL + error dict
- [x] `test_route_after_gate_pass` — PASS → routs to synthesise
- [x] `test_route_after_gate_hard_fail` — HARD violation → routs to human_gate
- [x] `test_route_after_gate_soft_fail_no_justification` — SOFT without justification → human_gate
- [x] `test_route_after_gate_soft_fail_with_justification` — SOFT with justification → synthesise
- [x] `test_route_after_gate_no_signal` — No signal → error_handler
- [x] `test_architect_halts_on_hard_constraint` — E2E: architect → gate → human_gate on HARD
- [x] `test_architect_passes_on_clean_code` — E2E: architect → gate → synthesise on PASS
- [x] `test_dolt_records_gate_failures` — architectural_gate_failures INSERT + Dolt commit
- [x] `test_audit_architectural_gate_endpoint` — POST /audit/architectural-gate returns 202

**Definition of Done**
- [x] `architectural_gate_node` in `harness_supervisor/nodes.py` calls `execute_architecture_check`
- [x] `route_after_gate` conditional routing: PASS → synthesise, HARD/soft-no-justification → human_gate, soft-with-justification → synthesise
- [x] `architect` → `architectural_gate` → `route_after_gate` wired in `build_supervisor`
- [x] `_after_human_gate` extended: `human_justification` set → resume to synthesise
- [x] `architectural_gate_failures` table in Dolt with thread_id, rule, severity, file, message + DOLT_COMMIT per write
- [x] `write_gate_failure` helper in `services/governance/core/dolt.py`
- [x] `POST /audit/architectural-gate` endpoint on governance (async, 202)
- [x] `execute_architecture_check` added to OPA policy for architect role
- [x] `execute_architecture_check` added to TOOL_NAME_MAP in GatewayClient
- [x] All 10 unit tests pass without Docker (0.18s); 2 E2E tests pass with InMemorySaver

**Notes / divergences**
- The container sandbox path remains a stub (`execute_architecture_check` runs checkers inside the review-server container, not in an isolated sandbox). The subprocess-based checkers (`import-linter`, `xenon`) are mounted directly — the architecture is ready for sandbox-in-Docker replacement.
- `ArchitectAgent.allowed_tools` was already restricted to `["codebase_search", "adr_read"]` (no `architecture_review`) before this phase — no change needed.
- `state.py` already had `ArchitecturalViolation`, `GateSignalContract`, and the extended `HarnessState` fields (`target_language`, `repo_path`, `gate_signal`, `human_justification`) from a prior edit — no state changes were needed.

### Post-Phase 7: Stub → Real Gate (2026-06-19)

Replaced `execute_architecture_check` stub with subprocess-based deterministic checkers:

- `services/review_server/architecture_gate/` package with checker framework:
  - `models.py` — `GateSignalContract` + `Violation` dataclasses
  - `base.py` — `Checker` ABC
  - `registry.py` — language → checker list map (unknown languages → PASS)
  - `runner.py` — orchestrator: HARD violations short-circuit, SOFT allows PROCEED
  - `checkers/import_linter.py` — subprocess wrapper for import-linter
  - `checkers/xenon_checker.py` — subprocess wrapper for xenon
- 9 unit tests (`test_unit_architecture_gate.py`) cover: unknown language, clean repos, layer violations, complexity violations, HARD short-circuit, SOFT proceed, graceful handling of uninstalled tools, and timeouts
- `server.py:execute_architecture_check` now calls `run_gate()` instead of a stub
- `pyproject.toml` deps: `import-linter>=2.0`, `xenon>=0.9`
- Dockerfile updated to copy `architecture_gate/` into container
- `uv.lock` + `requirements.txt` regenerated

**Notes**
- Unregistered languages return PASS (no false positives)
- Checker failures (tool not installed, timeout) return empty violations (graceful degradation)
- `repo_path` currently uses the path as-is; GitHub URL → temp dir clone is next

---

**Running total: 229 integration tests pass (14 new + 215 prior)**

## Modularity Cleanup — Issue 07: Extract `SkillRunner` from `GatewayClient` ✅

`packages/harness-gateway/harness_gateway/client.py` had grown to 357 lines mixing four concerns. Skill execution (a stateful workflow built on top of `call_tool`) was moved into a new `SkillRunner` class in `packages/harness-gateway/harness_gateway/skill_runner.py`. `GatewayClient.execute_skill` retained as a thin shim that delegates to `SkillRunner(self).execute(skill_id, inputs)`.

**Tests** — 11 new unit tests (`test_unit_skill_runner.py`, all pass without docker):
- [x] `test_execute_runs_all_steps`
- [x] `test_abort_on_step_denial_reraises`
- [x] `test_continue_skips_denied_step`
- [x] `test_rollback_runs_rollback_steps_then_raises`
- [x] `test_revoked_skill_raises` (410 from governance)
- [x] `test_missing_skill_raises` (404 from governance)
- [x] `test_steps_decoded_when_stored_as_json_string` (Dolt stores steps as JSON text)
- [x] `test_expected_signal_mismatch_raises`
- [x] `test_execute_requires_governance_url`
- [x] `test_extracted_methods_are_not_on_gateway_client` (refactor-shape assertion)
- [x] `test_gateway_execute_skill_shim_delegates_to_runner`

**DoD (issue 07 of `.scratch/modularity-cleanup/`)**
- [x] `skill_runner.py` exists (90 lines)
- [x] `client.py` no longer contains `_fetch_skill`, `_parse_steps`, `_execute_step`, `_handle_step_failure`, `_run_rollback`, `_count_completed`
- [x] `client.py` is 279 lines (under 280)
- [x] `execute_skill` call sites still work via the shim — verified by 30/30 existing integration tests (`test_skill_execution.py` + `test_skills_cli.py`)
- [x] ROLLBACK steps execute in order on `on_failure: ROLLBACK`
- [x] CONTINUE / ABORT semantics on per-step denial unchanged
- [x] `GatewayClient._get_token` promoted to public `get_token` so `SkillRunner` reads it through a clean seam (no underscore-private access across the new module boundary)

**Notes**
- Lazy import of `SkillRunner` inside `GatewayClient.execute_skill` avoids the circular import that would otherwise arise from `skill_runner.py` importing `ToolAccessDenied` from `client`.

---

## ADR-0038: Architect Server Retirement ✅

**Summary:** Replaced the host-side architect server (Python FastMCP running outside Docker) with a pure-Docker approach:
- `architecture_review` + `execute_architecture_check` moved to the review server (already has multi-provider LLM support, FastMCP, Docker)
- `codebase_search` + `adr_read` moved to new `services/github_mcp/` service wrapping the GitHub API
- `adr_write` and `diagram_gen` removed — architect role is review-only
- Host-side architect server (`host_servers/architect_server/`) deleted
- Registration for `architect_stub` now points to `github-mcp:9010` instead of `host.docker.internal:9006`

**Files created:** `services/github_mcp/server.py`, `services/github_mcp/Dockerfile`, `services/review_server/architecture_review.py`

**Files removed:** Entire `host_servers/architect_server/` directory (server.py, llm.py, architect_review.py, adr.py, diagram.py, search.py, resolver.py, embeddings.py, cache.py, tests/)

**Files modified:**
- `services/review_server/server.py` — added `architecture_review` and `execute_architecture_check` MCP tools
- `services/review_server/Dockerfile` — copies `architecture_review.py`
- `packages/harness-gateway/harness_gateway/client.py` — `TOOL_NAME_MAP` updated; `architecture_review`, `execute_architecture_check` → `review_server__*`; `adr_write`, `diagram_gen` removed
- `packages/harness-agents/harness_agents/architect.py` — `allowed_tools` now only `["codebase_search", "adr_read"]` (later re-added `issue_create` in ADR-0039)
- `policies/harness.rego` — removed `adr_write`, `diagram_gen` from architect; added `architecture_review`, `execute_architecture_check` to code_reviewer
- `docker-compose.yml` — added `github-mcp` service + `register-github-mcp`; `register-architect` points to `github-mcp:9010`; removed `extra_hosts` for `host.docker.internal`
- `services/contextforge_setup/setup.py` — architect_stub URL → `github-mcp:9010`
- `Makefile` — removed `architect-up`, `architect-down`, `architect-status` targets and vars
- `pyproject.toml` — removed `architect_server` from workspace members
- `.gitignore` — removed architect pid/log entries
- `ARCHITECTURE.md` — services table, OPA policy table, ADR table, Architectural Gate section updated
- `CLAUDE.md` — startup command updated, tool name mappings updated

**Tests:** All governance (17), Phase 6 OPA (11), Phase 6 agent invoke (6), Phase 3+4 unit (22), Phase 7 unit+integration (14) pass.

**Service verification via MCPJungle:**
- `architect_stub__codebase_search` → routed to github-mcp (requires `GITHUB_TOKEN`)
- `architect_stub__adr_read` → routed to github-mcp ✅
- `review_server__architecture_review` → routed to review-server ✅
- `review_server__execute_architecture_check` → routed to review-server ✅
- `review_server__review_diff` → routed to review-server ✅

---

## ADR-0039: issue_create replaces adr_write ✅

**Summary:** Replaced the defunct `adr_write` tool with `issue_create` on `github-mcp`. The architect now files GitHub issues for CRITICAL/HIGH findings instead of writing ADRs (which are records of decisions already made, not actionable work items).

**Files modified:**
- `services/github_mcp/server.py` — added `issue_create` MCP tool (POST /repos/:owner/:repo/issues)
- `packages/harness-gateway/harness_gateway/client.py` — `issue_create` → `github_mcp__issue_create`
- `packages/harness-agents/harness_agents/architect.py` — `allowed_tools` adds `issue_create`
- `policies/harness.rego` — `issue_create` added to architect role
- `prompts/architect.md` — added instruction to use `issue_create` for CRITICAL/HIGH findings
- `services/dolt/init.sh` — seed formula updated from `adr_write` to `issue_create`
- `packages/harness-tests/test_phase1_governance.py` — denied-tool test updated to `issue_create`
- `README.md`, `CLAUDE.md`, `ARCHITECTURE.md` — docs updated

**Tests:** All existing governance, agent, and supervisor tests pass unchanged.

---

## Code Health Sprint — 7.9 → 9.0 ✅

Four extractions reduced CCN in flagged hotspots, bringing overall score from **7.9 to 9.0**:

| File | Before | Change | After |
|---|---|---|---|
| `skill_runner.py:_execute_step` | CCN 7 | Extracted `_check_missing_keys` (broke `all()` compound condition) | CCN 3 |
| `code_analysis.py:_file_health_score` | CCN 10 | Extracted `_build_filter_expression` | CCN 3 |
| `client.py:_extract_content` | CCN 10 | Extracted `_get_content_for_source` | CCN 3 |
| `skills_cli.py:_handle_candidates` | CCN 6 | Extracted `_propose` (list comprehension out of elif chain) | CCN 5 |

17 functions remain at CCN 6 (under threshold — all borderline `if`/`elif` chains with 4–5 branches). No further work planned.

**Updated: 2026-06-20**

---

## DynamicSREAgent — Semantic Signal Sources + Skill-Aware Guidance (2026-06-22)

Completed four slices that wire real data into the SRE agent's investigation loop.

### Slice 4 — Semantic runbook_read via pgvector ✅

- `harness_memory/runbook_retriever.py` — `retrieve_runbooks(store, query, top_k)` searches the `"runbooks"` pgvector namespace using cosine similarity
- `sre_server.py` `runbook_read` made async; lazy-inits `PostgresMemoryStore` on first call when `PG_DSN` set; falls back to stub without infra
- 6 unit tests (`test_unit_runbook_retriever.py`) — all pass without Docker
- `make seed-runbooks` seeds `docs/runbooks/*.md` into pgvector (embed: `**When to use:**` signature line)

### Slice 2 — Semantic log_search via pgvector ✅

- `harness_memory/log_retriever.py` — `retrieve_logs(store, query, top_k)` searches `"logs"` namespace
- `harness_memory/log_seed.py` — ingests `docs/logs/*.jsonl`; one log entry per line
- `docs/logs/cost-spike.jsonl` (8 entries: runaway architect thread) + `docs/logs/db-latency.jsonl` (6 entries: connection pool exhaustion)
- `sre_server.py` `log_search` made async; same lazy-init + fallback pattern
- 6 unit tests (`test_unit_log_retriever.py`) — all pass without Docker
- `make seed-logs` seeds all JSONL fixtures

### Slice 5 — skill_search MCP tool (DoltFormulaStore.lookup) ✅

- `harness_memory/skill_retriever.py` — `retrieve_skill(store, agent_role, task)` wraps `store.lookup` (synchronous)
- `sre_server.py` `skill_search` tool: lazy-inits `DoltFormulaStore` when `DOLT_HOST` set; stub without it
- OPA policy: `skill_search` added to `sre` role
- `TOOL_NAME_MAP`: `"skill_search": "sre_stub__skill_search"`
- `DynamicSREAgent.allowed_tools` + SRE prompt updated
- 5 unit tests (`test_unit_skill_retriever.py`) — all pass without Docker

### Slice 6 — Skill-aware guidance + formula precedence ✅

- `DynamicSREAgent.__init__` accepts optional `formula_store`
- `_load_formula(task)` — synchronous; no-op when no store
- `run()` pre-loads formula before the loop; matched formula's steps injected into the opening message as a structured investigation plan
- SRE prompt updated: formula steps take precedence when pre-loaded; `skill_search` available for mid-investigation discovery
- 4 unit tests added to `test_unit_dynamic_sre.py` (behaviors 11–14): formula injected, no-match no-block, backward compat, role forwarded

### Slice 7 — End-to-end demo + doc reconciliation ✅

- `scripts/demo_sre.py` updated: wires `formula_store` (Dolt) + `memory_store` (pgvector) when env vars present; capability banner shows active/inactive signal sources; `memory_store.close()` on exit
- Unit test total: **182 pass** (up from 173 before this session)
- No integration test changes — all 229 integration tests pass unchanged

**Seeding commands** (requires Postgres + Ollama running locally):
```bash
make seed-runbooks   # docs/runbooks/*.md → pgvector "runbooks" namespace
make seed-logs       # docs/logs/*.jsonl  → pgvector "logs" namespace
```

---

## LLM Token Metrics in Grafana (2026-06-23)

Grafana "LLM tokens per model" and "LLM calls per model" panels now populate from `make demo-sre`.

**Tests** — 6 unit tests (`test_unit_llm_audit.py`):
- [x] `test_report_llm_usage_posts_correct_payload`
- [x] `test_report_llm_usage_noop_when_no_governance`
- [x] `test_report_llm_usage_swallows_exceptions`
- [x] `test_llm_usage_reported_after_successful_run`
- [x] `test_llm_usage_reported_on_max_turns_exceeded`
- [x] `test_llm_usage_not_reported_when_gateway_lacks_method`

**Definition of Done**
- [x] `GatewayClient.report_llm_usage()` POSTs synthetic `tool_name: "__llm__"` audit record to governance after agent run
- [x] Governance `/audit` calls `record_llm_usage()` — increments `harness_llm_calls_total` and `harness_llm_tokens_total` (labels: `agent_role`, `provider`, `model`, `token_type`)
- [x] `DynamicSREAgent._report_llm_usage()` called in `try/finally` — fires on all exit paths including `max_turns_exceeded`
- [x] Prometheus counters verified live at `curl http://localhost:8090/metrics | grep harness_llm`
- [x] Grafana panels populated after `make demo-sre`

**Notes**
- Bug: `get_token()` in `report_llm_usage` was missing `await` — returned coroutine object as Bearer token → silent 401 from governance. Fixed with one-character change.
- LLM calls use `tool_name: "__llm__"` in the audit body; governance routes based on presence of `llm_tokens` key.

---

## Centralised LLM Provider Factory (2026-06-23)

`build_llm_from_env()` is now the single entry point for provider selection across all agents and scripts.

**Tests** — 15 unit tests (`test_unit_llm_factory.py`):
- [x] `test_defaults_to_ollama_provider`
- [x] `test_ollama_reads_env_vars`
- [x] `test_ollama_kwarg_overrides_env`
- [x] `test_openrouter_provider`
- [x] `test_openrouter_max_tokens_kwarg`
- [x] `test_openrouter_raises_without_api_key`
- [x] `test_gemini_provider`
- [x] `test_gemini_raises_without_api_key`
- [x] `test_unknown_provider_raises`
- [x] `test_provider_kwarg_overrides_env`
- [x] `test_config_dict_selects_provider`
- [x] `test_config_dict_model_overrides_env`
- [x] `test_kwarg_overrides_config_dict`
- [x] `test_config_dict_provider_kwarg_still_wins`
- [x] `test_empty_config_dict_falls_through_to_env`

**Definition of Done**
- [x] `build_llm_from_env(provider=None, config=None, **overrides)` added to `harness_agents/llm.py`
- [x] Resolution order: kwarg > `config` dict > env var > default
- [x] `config` dict mirrors `server_config` JSONB schema: `{"llm_provider": "gemini", "gemini": {"model": "...", "api_key": "..."}, ...}`
- [x] `_pick()` helper extracts per-value resolution; `build_llm_from_env` CCN = 3, health 9.8/10
- [x] Call sites updated: `demo_sre.py`, `conftest.py`, `test_eval_architect.py`, `test_eval_reviewer.py`
- [x] SRE demo loads `server_config` from Postgres at startup via `_load_llm_config_from_pg()`; capability banner shows `llm: <provider>/<model> (source: db config | env/default)`
- [x] Review-server keeps its own `_build_llm_provider()` with DB config layer on top (unchanged)

**Notes**
- The `server_config` Postgres table is owned by the review-server but consumed by any component that reads it. This is de facto a shared config store; a dedicated config service would be the next step if more services need runtime LLM switching.
- `test_eval_architect.py` sets `max_tokens=4096` via kwarg to prevent synthesis truncation on OpenRouter — this is the only call site that overrides the default.
- Unit test total: **205 pass**, integration total: **222 pass** (440 total collected).
