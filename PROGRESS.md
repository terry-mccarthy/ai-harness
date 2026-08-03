# AI Harness — Build Progress

Tracks completion of feature-level work against its originating PRD/issue set.
A slice is recorded here once its tests are green and its own doc corner
(README/CLAUDE.md/ARCHITECTURE.md) has already been updated by the issue that
built it — this file is the durable, cross-cutting record, not a duplicate of
per-issue doc updates.

A `PROGRESS.md` existed earlier in this repo's history and was deleted
(commit `6359df1`, 2026-06-24) without a replacement. It is not being
restored — for phases before the one below, see `ARCHITECTURE.md`'s Decision
Log (the canonical ADR index) and `docs/tests.md`'s `## Phase N` headers,
which is where that history now lives. This file restarts as of the SRE
Agent Enhancement PRD.

---

## SRE Agent Enhancement — Dynamic ReAct Loop, Bounded Logs, Semantic Runbooks, Skill-Aware Guidance ✅ (2026-08-03)

PRD: [`.scratch/sre-agent-enhancement/PRD.md`](.scratch/sre-agent-enhancement/PRD.md).
Landed as issues 01, 02, 03, 04, 05, 06 on branch `feature/sre-agent-enhancement`,
plus issue 08 as a related cross-cutting addition. Issue 07 (this file) is the
capstone doc-reconciliation pass.

An earlier, less rigorous pass at this same feature shipped around
2026-06-22 (auto-executing formula steps, single best-match `skill_search`
with no score, no relevance threshold on runbook/log retrieval). The 2026-08
PRD process re-audited that work, found the earlier pass didn't meet its own
acceptance criteria, and re-scoped issues 02/04/05/06 to fix it properly
rather than accept the shortcuts. The entries below describe the corrected,
current state.

### Slice 1 — `DynamicSREAgent` ReAct loop (issue 01) ✅

Replaced the old linear "fire all three observability tools once, reason in
one pass" `SREAgent` with a ReAct tool-use loop modelled on
`DynamicCodeReviewerAgent`: the LLM emits one JSON action per turn
(`call_tool` or `respond`), the agent dispatches it and feeds the result
back, up to `MAX_TURNS = 16`, then produces a schema-validated incident
report (`SRE_OUTPUT_SCHEMA`). The static `SREAgent` was fully retired — no
references remain. The supervisor routes `task_type == "incident"` to
`DynamicSREAgent`.

Also covers: semantic response cache (two-tier Redis exact-key + pgvector
near-match, threshold 0.92), past-incident memory context injected into the
opening message, resolved reports written back to memory, per-turn token
accounting with a `token_budget` abort path, and corrective re-prompting on
malformed JSON / schema-invalid `respond` actions.

- `packages/harness-agents/harness_agents/dynamic_sre.py`
- `prompts/react_sre.md`
- Tests: `test_unit_dynamic_sre.py` (see Slices 4 below — the file is shared)

### Slice 2 — Bounded `log_search` (issue 02) ✅

`log_search` returns real, ranked, bounded matches instead of dumping the
raw log volume into the prompt: capped at `MAX_LOG_RESULTS = 5`, filtered to
`score >= LOG_MIN_SCORE (0.55)`, with `returned_count`/`total_count` fields
so the agent can tell "all relevant lines" from "truncated." Seeded from
`docs/logs/*.jsonl` via `make seed-logs`.

- `packages/harness-memory/harness_memory/log_retriever.py`
- Tests: `test_unit_log_retriever.py` (11), `test_log_retriever_integration.py` (3, against real seeded fixtures)

### Slice 3 — Semantic `runbook_read` (issue 03 + 04) ✅

Runbooks are ingested into the existing `PostgresMemoryStore` (pgvector +
Ollama embeddings) rather than looked up by exact name, and retrieved by
cosine similarity to the incident signature: top-3 matches, filtered to
`score >= RUNBOOK_MIN_SCORE (0.80)`; below threshold returns an empty
`runbooks` list (no separate "no match" error shape to check for) so the
agent falls back to `recommended_steps` instead of citing an irrelevant
runbook. Ingestion (issue 03) extracts each runbook's `**When to use:**`
line as its retrieval signature, skips and warns on malformed files, and is
idempotent (`make seed-runbooks`).

- `packages/harness-memory/harness_memory/runbook_retriever.py`, `runbook_seed.py`
- Tests: `test_unit_runbook_retriever.py` (9), `test_unit_runbook_seed.py` (5)

### Slice 4 — Skill-aware guidance and precedence (issue 05 + 06) ✅

`skill_search` (issue 05) is a read-only discovery tool: `DoltFormulaStore.list_matches()`
returns every ACTIVE, above-threshold TF-IDF match ranked by score, not just
a single winner — the shape the SRE agent needs to reason about "which skill
fits best," excluding deprecated/revoked/expired skills.

Skill *execution* (issue 06) is `run_skill`, a **native dispatch inside
`DynamicSREAgent`, not a gateway/MCP tool** — there is no `TOOL_NAME_MAP`
entry and no MCP server hosts it. `_handle_tool_call` intercepts
`tool == "run_skill"` before it would reach `self.gateway.call_tool()` and
instead drives `SkillRunner(self.gateway).execute(skill_id, inputs)`
directly, so every step of the skill still gets its own per-step OPA check
under the agent's real credentials — running a skill grants no more
authority than calling its steps by hand, and each step's `on_failure`
policy (ABORT/CONTINUE/ROLLBACK) is honoured. When a formula is preloaded,
its name/id/description (not raw steps) are injected as a steer toward
calling `run_skill(id)` — the LLM decides for itself rather than the harness
auto-executing steps server-side (the old `_execute_formula_steps` loop,
which ignored `on_failure` entirely, was deleted). `Formula.runbook_ref` and
`SRE_OUTPUT_SCHEMA.skill_ref` were added so a resolved report can cite both
the runbook and the skill it followed.

- `packages/harness-memory/harness_memory/skill_retriever.py`
- `packages/harness-agents/harness_agents/dynamic_sre.py` (`_run_skill`, `_resolve_run_formula`)
- Tests: `test_unit_skill_retriever.py` (12); `test_unit_dynamic_sre.py` (25 total across slices 1 and 4 — run_skill-specific: 7 unit + 2 integration, incl. `test_sre_skill_discovery_and_execution_through_live_gateway`)

### Related cross-cutting addition — Pluggable embedding provider (issue 08) ✅

Not one of the PRD's 4 numbered slices — flagged in the PRD's "hosted
deployment considerations" as a real gap and filed separately since it's
cross-cutting to `harness_memory`, not SRE-specific (`PostgresMemoryStore`
also backs the semantic cache, consolidation, and the code-reviewer's
memory namespace). `PostgresMemoryStore._embed()` was hardwired to Ollama;
introduced an `EmbeddingProvider` protocol + `OllamaEmbeddingProvider` +
`build_embedding_provider_from_env()` (same kwarg > config > env > default
resolution order as `build_llm_from_env()`), with `EMBEDDING_PROVIDER` added
alongside `EMBED_MODEL`. Only `ollama` is implemented; unknown provider names
raise `ValueError` naming what's actually supported. Gemini/OpenRouter
embedding backends are deliberately deferred until a hosted deployment
needs them.

- `packages/harness-memory/harness_memory/embedding_provider.py`
- Tests: `test_unit_embedding_provider.py` (12)

### Deliberate divergences from the PRD

1. **`log_search`/`runbook_read` use semantic pgvector search, not the PRD's
   originally-specified dependency-free keyword scheme.** The PRD's
   solution text called for "a simple, explainable scheme — substring / term
   overlap — not an embedding model; keep it dependency-free." Both tools
   instead call `PostgresMemoryStore.search()` (pgvector cosine similarity
   via Ollama embeddings). Accepted as an improvement: Postgres+Ollama is
   already a mandatory dependency for this agent (`runbook_read`, memory,
   the semantic cache), so "dependency-free" bought nothing in practice, and
   semantic recall is materially better than keyword/substring matching for
   free-text incident descriptions.

2. **`run_skill` is a native in-agent dispatch, not a registered MCP tool.**
   Two pre-existing "run a skill" mechanisms already existed in the
   codebase — `skills-registry-server`'s `registry_execute_skill` (trusts the
   skill's own declared `agent_role` rather than the caller's identity) and
   `review_server`'s `run_skill` (wired to unset `SKILL_CLIENT_ID`/
   `SKILL_CLIENT_SECRET` env vars) — and both had real authorization
   decoupling/misconfiguration problems, so neither was safe to reuse or
   extend for the SRE agent. `DynamicSREAgent` instead calls
   `SkillRunner(self.gateway).execute(...)` directly in Python, using its
   own already-authenticated `GatewayClient`, so every step still gets a
   fresh per-step OPA check under the agent's real credentials with no
   authority decoupling. See `docs/dev/memory-agents.md` for the full
   comparison.

### Test counts (authoritative, from this session's `pytest --collect-only` runs)

```
.venv/bin/python -m pytest packages/harness-tests/ -q --collect-only -m "not integration and not e2e and not live"
.venv/bin/python -m pytest packages/harness-tests/ -q --collect-only -m integration
.venv/bin/python -m pytest packages/harness-tests/ -q --collect-only -m eval
```

| Suite | Count |
|---|---|
| Total (`packages/harness-tests/`) | 700 |
| Unit (`make test-unit`) | 394 |
| Integration (`make test-integration`) | 285 |
| Eval (`pytest -m eval`) | 19 |

`README.md`, `docs/tests.md`, and `ARCHITECTURE.md` were reconciled to these
numbers as part of this pass (issue 07) — they previously disagreed with
each other (699/393 vs. 700/394) and, in `ARCHITECTURE.md`'s case, with
reality (a stale 221/13 breakdown table last updated several phases ago).
