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
- [ ] 8. Network policy blocks agents from reaching MCP servers directly (Docker Compose network isolation not enforced yet)
- [x] 9. `dolt log` shows one commit per tool call with human-readable message
- [x] 10. Phase 2 can begin without modifying gateway or policy engine

**Notes / divergences from spec**
- Governance service is a custom FastAPI app at `:8090`, not a MCPJungle Enterprise feature
- `review_diff` added to `code_reviewer` OPA policy (spec omitted it; needed for Phase 0 tests to keep passing through governance)
- GatewayClient auto-fetches bearer tokens; falls back gracefully if governance absent

---

## Phase 2 — Persistent Memory Layer ⬜

27 tests. Not started.

- [ ] `test_checkpointer_saves_state`
- [ ] `test_checkpointer_resumes`
- [ ] `test_checkpointer_thread_isolation`
- [ ] `test_memory_write_and_read`
- [ ] `test_memory_namespace_isolation`
- [ ] `test_memory_cross_session_persistence`
- [ ] `test_memory_ttl_expiry`
- [ ] `test_memory_redis_hot_read`
- [ ] `test_memory_semantic_search`
- [ ] `test_memory_overwrite`
- [ ] `test_memory_delete`
- [ ] `test_memory_interface_compliance`
- [ ] `test_sre_runbook_namespace`
- [ ] `test_episodic_memory_write`
- [ ] `test_semantic_memory_written_by_consolidation`
- [ ] `test_consolidation_clusters_similar_episodes`
- [ ] `test_consolidation_preserves_distinct_episodes`
- [ ] `test_consolidation_prunes_expired_items`
- [ ] `test_formula_quality_score_updated`
- [ ] `test_formula_graduates_to_proven`
- [ ] `test_formula_flagged_for_review`
- [ ] `test_formula_write_creates_dolt_commit`
- [ ] `test_formula_lookup_by_task`
- [ ] `test_formula_lookup_no_match`
- [ ] `test_formula_version_history`
- [ ] `test_formula_deprecate`
- [ ] `test_formula_interface_compliance`

---

## Phase 3 — Specialised Agent Nodes ⬜

14 tests. Blocked on Phase 1 + Phase 2 both complete.

---

## Phase 4 — Agent Orchestration ⬜

23 tests. Blocked on Phase 3.

---

## Phase 5 — Production Hardening ⬜

8 tests + all prior phases against ContextForge. Blocked on Phase 4.
