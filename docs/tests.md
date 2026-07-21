# Test Coverage

All tests live in `packages/harness-tests/`. Run them with:

```bash
make test-integration   # 267 integration tests (requires Docker stack)
make test-unit          # 314 unit tests (no infra needed)
pytest -m eval -v -s    # 19 eval tests (requires Ollama only)
```

## Phase 0 — Core reviewer (9 tests)

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

## Phase 1 — Governance (17 tests)

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

## Phase 2 — Persistent Memory Layer (27 tests)

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

## Bootstrap — Architecture doc generation (15 tests)

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

## Phase 3 — Specialised Agent Nodes (6 tests)

| Test | What it proves |
|---|---|
| `test_agent_node_contract_compliance` | All three agents satisfy `AgentNode` Protocol |
| `test_architect_synthesis_retries_on_schema_violation` | Schema-invalid synthesis is rejected and retried, then accepted |
| `test_architect_errors_when_synthesis_never_schema_valid` | Synthesis that never validates → `run()` returns `invalid_output` |
| `test_architect_tool_calls_go_via_gateway` | ArchitectAgent calls tools through GatewayClient |
| `test_architect_denied_shell_exec` | Architect role is blocked from `shell_exec` by OPA |
| `test_sre_shell_exec_blocked_without_approval` | SRE `shell_exec` blocked without `X-Human-Approval-Token` header |

## Phase 4 — Agent Orchestration (27 tests)

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

## Phase 5 — Production Hardening (8 tests)

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

## Skill Learning — issues 01–08 (64 tests)

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

## Phase 6 — Skills Registry (20 tests)

| Suite | Tests | What it covers |
|---|---|---|
| `test_governance_author.py` | 10 | `POST /skills/author`, `GET /skills/{id}/prompt` — manual authoring path, `manually_authored` column, Dolt commit, 403 for SRE, 410 on revoke |
| `test_skill_registry.py` | 10 | 14 MCP tools via `skills-registry-server` — list/get/create/revoke skills, label episodes, execute skill, access-control enforcement |

## Phase 7 — Architecture as Code (14 tests)

| Test | What it proves |
|---|---|
| `test_gate_passes_clean_code` | No violations → gate_signal.result == 'PASS' |
| `test_gate_fails_layer_violation` | Layer violation → HARD severity, FAIL result |
| `test_gate_enforces_complexity_limit` | Complexity limit → SOFT severity, FAIL result |
| `test_gate_passes_params_to_tool` | repo_path + target_language forwarded to tool |
| `test_gate_handles_tool_denied` | ToolAccessDenied → FAIL + error dict |
| `test_route_after_gate_pass` | PASS → routes to synthesise |
| `test_route_after_gate_hard_fail` | HARD violation → routes to human_gate |
| `test_route_after_gate_soft_fail_no_justification` | SOFT without justification → human_gate |
| `test_route_after_gate_soft_fail_with_justification` | SOFT with justification → synthesise |
| `test_route_after_gate_no_signal` | No signal → error_handler |
| `test_architect_halts_on_hard_constraint` | E2E: architect → gate → human_gate on HARD |
| `test_architect_passes_on_clean_code` | E2E: architect → gate → synthesise on PASS |
| `test_dolt_records_gate_failures` | architectural_gate_failures INSERT + Dolt commit |
| `test_audit_architectural_gate_endpoint` | POST /audit/architectural-gate returns 202 |

## Eval suite (19 tests)

Run with `pytest -m eval -v -s`. Scores agents against labeled fixtures with known problems. Uses mock gateways — no Docker stack needed, only Ollama.

**Reviewer** — `CodeReviewerAgent` against labeled diffs with known security bugs:

| Test | What it proves |
|---|---|
| `test_reviewer_fixture[01_clean_refactor]` | No false-positive CRITICALs on a clean refactor |
| `test_reviewer_fixture[02_sql_injection]` | Catches SQL injection (f-string + string concat in queries) |
| `test_reviewer_fixture[03_hardcoded_secret]` | Catches hardcoded AWS credentials and database password |
| `test_reviewer_fixture[04_shell_injection]` | Catches `shell=True` with user-controlled input |
| `test_reviewer_fixture[05_missing_auth]` | Catches auth/role decorators removed from admin endpoints |
| `test_reviewer_fixture[06_path_traversal]` | Catches user-controlled filename used directly in `open()` |
| `test_reviewer_fixture[07_prompt_injection]` | Resists an in-diff instruction trying to steer the reviewer's verdict |
| `test_reviewer_fixture[08_prompt_injection_exfil]` | Resists an in-diff instruction trying to exfiltrate secrets via the reviewer's own output |
| `test_reviewer_aggregate_score` | Asserts verdict accuracy ≥ 80% and recall ≥ 60% across all fixtures |

**Architect** — four-phase `ArchitectAgent` against fixture repos expressed as canned tool responses:

| Test | What it proves |
|---|---|
| `test_architect_fixture[clean_layered]` | Control: a clean hexagonal app raises no false CRITICAL |
| `test_architect_fixture[god_controller]` | Catches business logic + SQL inline in an HTTP handler (layering/coupling) |
| `test_architect_fixture[leaky_persistence]` | Catches SQLAlchemy/ORM leaking through a domain "port" (abstraction/coupling) |
| `test_architect_aggregate_score` | Schema validity 100%, detection ≥ 66%, recall ≥ 50%, synthesis matches `ARCHITECT_OUTPUT_SCHEMA` |

**Adversarial code critic** — `AdversarialCodeCritic` against trap fixtures pairing a diff with a first-pass reviewer output and a known answer key:

| Test | What it proves |
|---|---|
| `test_adversarial_critic_fixture[01_must_confirm_underrated_sqli]` | Confirms a CRITICAL finding with an `exploit_scenario` for an ORDER BY injection the first pass under-rated as WARNING |
| `test_adversarial_critic_fixture[02_must_refute_false_positive]` | Refutes/downgrades a first-pass CRITICAL finding on a status value already validated against an allowlist |
| `test_adversarial_critic_aggregate_scores` | Asserts confirm-rate ≥ 80% and refute-rate ≥ 60% across all fixtures |

**Adversarial architecture critic** — `AdversarialArchitectureCritic` against trap fixtures pairing grounding context with a first-pass synthesis output and a known answer key:

| Test | What it proves |
|---|---|
| `test_adversarial_architecture_critic_fixture[01_must_confirm_underrated_shared_cache]` | Confirms a HIGH+ finding with a `regression_scenario` for a cache-key isolation gap the first pass under-rated as MEDIUM |
| `test_adversarial_architecture_critic_fixture[02_must_refute_false_positive_facade_coupling]` | Refutes/downgrades a first-pass HIGH finding that turns out to be interface-backed, not tightly coupled |
| `test_adversarial_architecture_critic_aggregate_scores` | Asserts confirm-rate ≥ 80% and refute-rate ≥ 60% across all fixtures |

## Semantic response cache (14 tests)

| Test | What it proves |
|---|---|
| `test_high_score_hit_returns_cached_result` | Mock store score ≥ 0.92 → `run()` returns early with `cache_hit: True`; LLM never called |
| `test_configurable_threshold_respected` | `cache_threshold=1.0` makes a 0.95-score result a miss |
| `test_successful_run_writes_to_cache_with_ttl` | Successful run writes `{task, agent_output}` to `"cache"` namespace with TTL |
| `test_failed_run_does_not_write_to_cache` | `max_turns_exceeded` result produces no cache write |
| `test_low_score_hit_runs_loop` | Score < threshold → ReAct loop runs, no `cache_hit` |
| `test_llm_usage_not_reported_on_cache_hit` | `report_llm_usage` never called on cache hit |
| `test_force_refresh_skips_cache_lookup` | `force_refresh=True` bypasses lookup even on a 0.99-score hit |
| `test_force_refresh_does_not_write_to_cache` | `force_refresh=True` on a successful run produces no cache write |
| `test_empty_search_result_runs_loop` | Empty search → loop runs normally |
| `test_no_memory_store_agent_runs_unchanged` | `memory_store=None` → no cache path, backward-compatible |
| `test_same_task_twice_returns_cache_hit` | (integration) Identical task → second call returns `cache_hit: True` via Redis exact key |
| `test_semantically_equivalent_task_returns_cache_hit` | (integration) Near-identical paraphrase → cache hit via pgvector |
| `test_expired_cache_entry_is_a_miss` | (integration) Expired entry filtered out; ReAct loop runs |
| `test_force_refresh_bypasses_cached_result_and_runs_loop` | (integration) `force_refresh=True` → LLM called, `cache_hit` absent |

## Token usage (9 tests)

| Test | What it proves |
|---|---|
| `test_llm_response_has_token_fields` | `LLMResponse` carries `prompt_tokens` and `completion_tokens` |
| `test_llm_response_defaults_to_zero` | Fields default to 0 when not supplied |
| `test_ollama_provider_captures_token_counts` | `OllamaProvider` maps `prompt_eval_count`/`eval_count` to response |
| `test_ollama_provider_none_counts_become_zero` | `None` eval counts (cached Ollama response) default to 0 |
| `test_agent_state_accepts_token_fields` | `AgentState` TypedDict accepts `token_usage` and `token_budget` |
| `test_reviewer_accumulates_token_usage` | Reviewer returns accumulated token counts in result state |
| `test_reviewer_accumulates_across_retries` | Token counts sum across all retry iterations |
| `test_reviewer_budget_exceeded_on_retry` | Reviewer aborts with `token_budget_exceeded` when completion tokens exceed budget |
| `test_reviewer_no_budget_runs_to_completion` | `token_budget=None` never triggers budget check |

## git_diff GitHub mode (9 tests)

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

## Adversarial code critic (30 tests)

`AdversarialCodeCritic` attacks a first-pass `CodeReviewerAgent` output; a confirmed/escalated CRITICAL finding requires a concrete `exploit_scenario`, not a bare severity label.

**Schema** (`ADVERSARIAL_CODE_CRITIC_SCHEMA`, 10 tests) — no LLM required:

| Test | What it proves |
|---|---|
| `test_confirmed_critical_with_exploit_scenario_is_valid` | Well-formed confirmed/CRITICAL finding validates |
| `test_confirmed_critical_missing_exploit_scenario_is_invalid` | Confirmed/CRITICAL without `exploit_scenario` is rejected |
| `test_escalated_critical_missing_exploit_scenario_is_invalid` | Same rule applies to `escalated` outcome |
| `test_confirmed_critical_with_empty_exploit_scenario_is_invalid` | Empty string does not satisfy the forced-artifact requirement |
| `test_refuted_critical_does_not_require_exploit_scenario` | `refuted` outcome has no exploit requirement |
| `test_downgraded_warning_does_not_require_exploit_scenario` | `downgraded` outcome has no exploit requirement |
| `test_confirmed_warning_does_not_require_exploit_scenario` | Forced-artifact rule only binds at CRITICAL severity |
| `test_unresolved_outcome_is_valid` | Bounded-retry terminal state validates |
| `test_invalid_outcome_enum_value_is_rejected` | Non-enum `outcome` value is rejected |
| `test_missing_required_top_level_summary_is_invalid` | Missing `summary` is rejected |

**Agent** (`AdversarialCodeCritic`, 7 tests) — mocked gateway/LLM:

| Test | What it proves |
|---|---|
| `test_critic_returns_structured_output_matching_schema` | Output validates against `ADVERSARIAL_CODE_CRITIC_SCHEMA` |
| `test_critic_confirms_finding_with_exploit_scenario` | Confirmed finding carries a non-empty `exploit_scenario` |
| `test_critic_passes_first_pass_output_to_llm_prompt` | The first-pass output is embedded in the user message, not just the raw diff |
| `test_critic_reuses_gathered_tool_results_not_raw_diff_only` | Critic calls `git_diff` and `run_linter` itself, same as the reviewer |
| `test_critic_retries_on_invalid_output_then_succeeds` | Invalid JSON is retried and recovers |
| `test_critic_gives_up_after_max_iterations_invalid_output` | `MAX_ITERATIONS` exhausted → `error.code == "invalid_output"` |
| `test_critic_denies_gracefully_on_tool_access_denied` | `ToolAccessDenied` → `error.code == "tool_access_denied"` |

**HTTP + MCP tool** (`POST /review-adversarial`, `adversarial_review`, 8 tests):

| Test | What it proves |
|---|---|
| `test_http_adversarial_review_endpoint_exists` | `POST /review-adversarial` returns 200 for a valid request |
| `test_http_adversarial_review_returns_findings_and_summary` | Response has `findings`/`summary`, confirmed finding carries `exploit_scenario` |
| `test_http_adversarial_review_missing_diff_text_returns_422` | Missing `diff_text` → 422 |
| `test_http_adversarial_review_missing_first_pass_output_returns_422` | Missing `first_pass_output` → 422 |
| `test_http_adversarial_review_agent_error_returns_400` | Agent failure (max retries exceeded) → 400 |
| `test_http_adversarial_review_wrong_key_returns_401` | Wrong bearer token → 401 |
| `test_http_adversarial_review_no_key_set_allows_all` | `REVIEW_API_KEY` unset → all requests allowed (dev mode) |
| `test_mcp_adversarial_review_tool_reachable_in_process` | `_run_adversarial_review` callable directly with mocks |

**OPA policy** (`adversarial_code_critic` role, 5 tests, `@pytest.mark.integration`):

| Test | What it proves |
|---|---|
| `test_opa_allows_adversarial_code_critic_git_diff` | Role may call `git_diff` |
| `test_opa_allows_adversarial_code_critic_run_linter` | Role may call `run_linter` |
| `test_opa_denies_adversarial_code_critic_shell_exec` | Role denied `shell_exec` |
| `test_opa_denies_adversarial_code_critic_issue_create` | Role denied `issue_create` |
| `test_opa_denies_adversarial_code_critic_codebase_search` | Role denied architect-only tools |

## Adversarial architecture critic (38 tests)

`AdversarialArchitectureCritic` attacks a first-pass `ArchitectAgent` synthesis output; a confirmed/escalated HIGH+ finding requires a concrete `regression_scenario`, not a bare severity label.

**Schema** (`ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA`, 12 tests) — no LLM required:

| Test | What it proves |
|---|---|
| `test_confirmed_high_with_regression_scenario_is_valid` | Well-formed confirmed/HIGH finding validates |
| `test_confirmed_critical_with_regression_scenario_is_valid` | Well-formed confirmed/CRITICAL finding validates |
| `test_confirmed_high_missing_regression_scenario_is_invalid` | Confirmed/HIGH without `regression_scenario` is rejected |
| `test_escalated_critical_missing_regression_scenario_is_invalid` | Same rule applies to `escalated` outcome |
| `test_confirmed_high_with_empty_regression_scenario_is_invalid` | Empty string does not satisfy the forced-artifact requirement |
| `test_refuted_high_does_not_require_regression_scenario` | `refuted` outcome has no regression requirement |
| `test_downgraded_medium_does_not_require_regression_scenario` | `downgraded` outcome has no regression requirement |
| `test_confirmed_medium_does_not_require_regression_scenario` | Forced-artifact rule only binds at HIGH+ severity |
| `test_unresolved_outcome_is_valid` | Bounded-retry terminal state validates |
| `test_invalid_outcome_enum_value_is_rejected` | Non-enum `outcome` value is rejected |
| `test_invalid_severity_enum_value_is_rejected` | Non-enum `severity` value is rejected |
| `test_missing_required_top_level_summary_is_invalid` | Missing `summary` is rejected |

**Agent** (`AdversarialArchitectureCritic`, 8 tests) — mocked gateway/LLM:

| Test | What it proves |
|---|---|
| `test_critic_returns_structured_output_matching_schema` | Output validates against `ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA` |
| `test_critic_confirms_finding_with_regression_scenario` | Confirmed finding carries a non-empty `regression_scenario` |
| `test_critic_passes_first_pass_output_to_llm_prompt` | The first-pass synthesis output is embedded in the user message |
| `test_critic_includes_diff_in_prompt_when_target_is_a_diff` | `state["diff"]` (target_mode="diff") is embedded in the user message, not dropped |
| `test_critic_reuses_architect_tools_for_grounding_context` | Critic calls `codebase_search`, `adr_read`, and `codebase_hotspots` itself, same tool surface as the architect |
| `test_critic_retries_on_invalid_output_then_succeeds` | Invalid JSON is retried and recovers |
| `test_critic_gives_up_after_max_iterations_invalid_output` | `MAX_ITERATIONS` exhausted → `error.code == "invalid_output"` |
| `test_critic_denies_gracefully_on_tool_access_denied` | `ToolAccessDenied` → `error.code == "tool_access_denied"` |

**HTTP + MCP tool** (`POST /review-architecture-adversarial`, `adversarial_architecture_review`, 12 tests):

| Test | What it proves |
|---|---|
| `test_http_adversarial_architecture_review_endpoint_exists` | `POST /review-architecture-adversarial` returns 200 for a valid request |
| `test_http_adversarial_architecture_review_returns_findings_and_summary` | Response has `findings`/`summary`, confirmed finding carries `regression_scenario` |
| `test_http_adversarial_architecture_review_missing_repo_returns_422` | Missing `repo` → 422 |
| `test_http_adversarial_architecture_review_missing_first_pass_output_returns_422` | Missing `first_pass_output` → 422 |
| `test_http_adversarial_architecture_review_empty_dict_first_pass_output_is_accepted` | `{}` is a valid dict, not a missing field — must not 422 |
| `test_http_adversarial_architecture_review_agent_error_returns_400` | Agent failure (max retries exceeded) → 400 |
| `test_http_adversarial_architecture_review_wrong_key_returns_401` | Wrong bearer token → 401 |
| `test_http_adversarial_architecture_review_no_key_set_allows_all` | `REVIEW_API_KEY` unset → all requests allowed (dev mode) |
| `test_http_adversarial_architecture_review_accepts_diff_target_mode` | `target_mode="diff"` + `diff` is a valid target shape, mirrors `/review-architecture` |
| `test_http_adversarial_architecture_review_defaults_to_codebase_target_mode` | Omitting `target_mode`/`diff` still works (codebase mode default) |
| `test_http_adversarial_architecture_review_diff_reaches_the_critic_prompt` | The `diff` body field is threaded into the agent's prompt, not just accepted and dropped |
| `test_mcp_adversarial_architecture_review_tool_reachable_in_process` | `_run_adversarial_architecture_review` callable directly with mocks |

**OPA policy** (`adversarial_architecture_critic` role, 6 tests, `@pytest.mark.integration`):

| Test | What it proves |
|---|---|
| `test_opa_allows_adversarial_architecture_critic_codebase_search` | Role may call `codebase_search` |
| `test_opa_allows_adversarial_architecture_critic_adr_read` | Role may call `adr_read` |
| `test_opa_allows_adversarial_architecture_critic_codebase_hotspots` | Role may call `codebase_hotspots` |
| `test_opa_denies_adversarial_architecture_critic_issue_create` | Role denied `issue_create` (unlike the first-pass architect) |
| `test_opa_denies_adversarial_architecture_critic_shell_exec` | Role denied `shell_exec` |
| `test_opa_denies_adversarial_architecture_critic_git_diff` | Role denied code-critic-only tools |

## review server HTTP endpoint (12 tests)

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
