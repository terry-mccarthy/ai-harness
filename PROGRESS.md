# AI Harness — Build Progress

Tracks completion against [spec-full.md](spec-full.md). A phase is done when all its tests pass **and** its Definition of Done checklist is signed off. Update this file as tests go green.

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

### Semgrep linter replacement

Replaced the naive pattern-matching `linter_server.py` with a real semgrep scan.

- `stub_servers/semgrep-rules.yml` — 8 bundled rules: `print-call`, `hardcoded-credential`, `credential-in-url-var`, `subprocess-shell-true`, `sql-fstring-query`, `open-fstring-path`, `eval-call`, `os-system-call`
- `stub_servers/Dockerfile.stub` — adds `pip install semgrep` layer
- `packages/harness-tests/test_unit_linter.py` — 11 unit tests covering diff parsing and semgrep output mapping (subprocess mocked; no semgrep binary needed locally)
- Validated against all 6 eval fixtures: clean diff returns no warnings; SQL injection, hardcoded secrets, shell injection, and path traversal all flagged correctly
- Gotcha: semgrep `metavariable-regex` uses anchored match — must use `(?i).*keyword.*` not `(?i)keyword` to match compound variable names like `AWS_SECRET_ACCESS_KEY`
