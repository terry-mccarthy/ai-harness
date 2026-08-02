# PRD: SRE Agent Enhancement — Dynamic ReAct Loop, Bounded Logs, Semantic Runbooks

Status: partially implemented (audited 2026-08-02, revised 2026-08-03)

## Implementation Status (audited 2026-08-02, revised 2026-08-03)

Code exists on `main` for every slice, but coverage against this PRD's acceptance
criteria is uneven. None of the 7 issue files below had ever been flipped to
`done` in the tracker before this audit — the tracker was accurate; work had
simply landed without status updates. Findings, per issue:

| Issue | Status | Notes |
|---|---|---|
| [01 — DynamicSREAgent ReAct loop](issues/01-dynamic-sre-react-loop.md) | `done` | Matches spec closely. `packages/harness-agents/harness_agents/dynamic_sre.py` + `prompts/react_sre.md`; 17 unit tests in `test_unit_dynamic_sre.py`, all passing. Static `SREAgent` fully retired (no references left). Supervisor routes `incident` → `DynamicSREAgent`. |
| [02 — Bounded log_search](issues/02-bounded-log-search.md) | `ready-for-agent` (revised scope, now `Blocked by` 04) | **Deviates from spec, decision made to keep it.** `log_search` uses `PostgresMemoryStore.search()` (pgvector/Ollama semantic similarity) instead of the PRD's dependency-free substring/term-overlap scheme — accepted, since Postgres+Ollama is already mandatory for `runbook_read`/memory on this agent, and semantic recall is a real improvement over keyword matching. The 5-line cap *is* enforced (the MCP tool only exposes `query`, not `top_k` — an earlier pass of this audit wrongly called it bypassable). Remaining gap: `returned_count`/`total_count` truncation-detection fields need a relevance threshold that doesn't exist yet — issue 04 now owns building it (`min_score` on `PostgresMemoryStore.search()`) since it needs the identical primitive; 02 consumes it rather than building its own, to avoid two parallel worktrees colliding on the same method. Also needs a test against the real seeded `docs/logs/*.jsonl` fixtures, not just a fake store. |
| [03 — Runbook ingestion seed](issues/03-runbook-ingestion-seed.md) | `done` | `harness_memory/runbook_seed.py` matches spec: extracts `**When to use:**` as signature, skips+warns on malformed files, idempotent upsert, exposed as both `make seed-runbooks` and an importable function. |
| [04 — Semantic runbook_read](issues/04-semantic-runbook-read.md) | `ready-for-agent` (partial, no blockers) | Returns top-3 runbooks with id/signature/score (`harness_memory/runbook_retriever.py`), but there is no relevance threshold anywhere in the codebase — the "no matching runbook" sentinel result required when nothing scores above ≈0.80 was never built. Always returns whatever it finds. Now the designated owner of the shared `min_score` primitive on `PostgresMemoryStore.search()` (issue 02 depends on it landing here first). |
| [05 — skill_search discovery tool](issues/05-skill-search-discovery-tool.md) | `ready-for-agent` (partial) | Tool exists (`sre_stub.skill_search` → `harness_memory/skill_retriever.py` → `DoltFormulaStore.lookup`), and `lookup` already excludes non-ACTIVE/expired skills. But it returns a single best match with no `score` field, not the ranked list-with-scores the AC calls for. |
| [06 — Skill-aware guidance and precedence](issues/06-skill-aware-guidance.md) | `ready-for-agent` (revised scope) | **Not an open design fork — unfinished wiring.** A full `run_skill` execution engine already exists (`GatewayClient.execute_skill()` → `SkillRunner`, with per-step OPA checks and `on_failure` ABORT/CONTINUE/ROLLBACK policy) and is already exposed as an LLM-callable tool on `review_server`. The SRE agent doesn't use it: `DynamicSREAgent._execute_formula_steps()` hand-rolls a weaker duplicate that **ignores `on_failure` entirely** (a step marked ABORT still lets the loop continue) and isn't reachable as a tool call. Per-step OPA enforcement itself does hold either way — this is a correctness/reuse gap, not a security hole. Also confirmed: `Formula` has no `runbook_ref` field and `SRE_OUTPUT_SCHEMA` has no field to cite an executed skill id — the skill↔runbook report linkage has no data-model support yet, not just missing wiring. |
| [07 — SRE enhancement doc reconciliation](issues/07-incident-demo-and-docs.md) | `ready-for-agent` (partial, rescoped 2026-08-03) | Rescoped to doc reconciliation only — the demo requirement was dropped (`scripts/demo_sre.py` already exists and is no longer tracked by this issue). `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, and `docs/dev/memory-agents.md` all reference the dynamic SRE flow. `PROGRESS.md` does not exist anywhere in the repo, so that reconciliation AC is unmet. |

**Bottom line:** Slice 1 (the ReAct loop — the hardest, highest-value piece) is
solid and complete. Slice 3's ingestion half is solid. Slices 2 and 6's
apparent "deviations" both turned out, on closer inspection, not to be open
architecture forks: slice 2's semantic search is a reasonable improvement over
the original spec, and slice 6 has a `run_skill` execution engine already
built and used elsewhere (`review_server`) — the SRE agent just isn't wired to
it. No issue in this PRD is actually blocked on a maintainer *decision*
anymore; every remaining item is concrete, scoped engineering work:
- a shared relevance threshold (blocks issue 02's truncation counts and issue
  04's "no matching runbook" result)
- issue 05's missing `score` field
- issue 06's reuse of `SkillRunner`/`execute_skill` (fixes a real `on_failure`
  correctness gap) plus a `run_skill` tool for the `sre` role, and a
  `runbook_ref` field on `Formula` + a skill-id field on `SRE_OUTPUT_SCHEMA`
  for report linkage
- issue 07's `PROGRESS.md`, blocked in substance until 02/04/06 land

39 unit tests exist across the touched modules and all pass; they just don't
cover the requirements above, because none of those requirements exist yet to
test.

## Problem Statement

As an operator relying on the harness to diagnose production incidents, I get
shallow, brittle incident reports from the SRE agent. The agent fires all three
observability tools once, up front, using the raw incident description as the
query for every tool, then reasons in a single LLM pass. It cannot form a
hypothesis, check one signal, find it wrong, and look somewhere else — which is
how real incident triage works. Worse, its "reasoning loop" only re-prompts on
*JSON schema* failures, never to gather more evidence.

Two downstream problems make this concrete:

- **Logs flood the context.** `log_search` results are dumped verbatim into the
  prompt via `json.dumps(...)`. The moment the tool returns realistic volume,
  the model's context window is exhausted and the report degrades.
- **Runbook lookup is a keyword guess.** `runbook_read` is called with the
  *entire incident description* as the `runbook_name`. Against anything other
  than a stub, that matches nothing. The agent has no way to find the runbook
  whose *signature* resembles the incident.

The SRE agent's safety scaffolding (OPA RBAC, scoped `human_approval_token`
HITL, Dolt audit) is already strong and is **not** the problem. The reasoning
loop and the evidence-gathering tools are.

## Why these slices without a production environment

This harness has no live production estate — no real Datadog/Prometheus, no real
CI/CD, no real cluster. That does **not** make these slices unproductive,
because the deliverable here is not "an agent that runs our prod." It is a
**demonstrable reference architecture for governed, learning, tool-using
agents**: non-linear orchestration (slice 1), context-safe evidence handling
(slice 2), retrieval over an authored knowledge corpus (slice 3), and a
governed self-learning loop (slice 4). Every one of those is real, tested code
exercised against **realistic seeded fixtures** — and the safety/learning
machinery, not the integrations, is the distinctive part.

Guardrails that follow from this:

- **Realistic fixtures, not real prod, are the dependency.** The value comes from
  the agent reasoning over plausible data: a small but real-looking seeded log
  source, the runbooks already in `docs/runbooks/`, and a handful of seeded
  ACTIVE skills. A stub returning `{"result": "stub"}` produces impressive
  scaffolding with no payload — that is the failure mode to avoid. Invest in seed
  data and a scripted end-to-end incident demo; treat the demo as a first-class
  deliverable alongside the tests.
- **Do not build untestable cloud adapters.** Real Datadog/Prometheus/CloudWatch,
  Elasticsearch/Splunk, `kubectl`/EC2 integrations stay out (see Out of Scope).
  `observability_query` remains a stub deliberately. Building adapters that can
  never be run or tested here would be hollow effort.
- **Recommended build order:** slice 1 → slice 3 → slice 2 (minimal) → slice 4.
  This yields a working, demonstrable agent early (1 + 3), keeps it from
  collapsing on log volume (2), then lands the differentiator (4). Slice 2 is
  deliberately minimal — its only job is to stop a realistic `log_search` from
  flooding the context window that slice 1 depends on.
- **Slice 4 degrades gracefully.** With no skills seeded yet, guidance falls back
  to runbook-only (cold-start) and the skill-precedence paths are proven via the
  fake skill store in tests — so the slice is buildable before any real incident
  volume exists.

## Solution

As an operator, I want the SRE agent to drive its own non-linear investigation:
decide which tool to call next based on what it just observed, gather evidence
across several turns, and only then deliver a structured incident report. When
it pulls logs, I want bounded, ranked results so a noisy log stream never blows
the context window. When it looks for a runbook, I want it found by semantic
similarity to the incident signature, using the vector store the harness already
runs — not a new database.

Concretely, four end-to-end slices:

1. **`DynamicSREAgent`** — a ReAct tool-use loop, modelled exactly on the
   existing `DynamicCodeReviewerAgent`. The LLM emits one JSON action per turn
   (`call_tool` or `respond`); the agent dispatches it, feeds the tool result
   back, and loops up to a turn cap before producing a schema-validated incident
   report. This replaces the linear gather-then-summarise flow.

2. **Bounded `log_search`** — the `sre_stub` `log_search` tool returns real,
   bounded, ranked matches against a seeded sample log source baked into the
   container, mirroring how `linter_stub` runs semgrep against seeded input.

3. **Semantic `runbook_read`** — runbooks are ingested into the existing
   `PostgresMemoryStore` (pgvector + `nomic-embed-text`) and retrieved by
   semantic similarity to the incident signature, replacing the by-name keyword
   lookup. No new vector database is introduced.

4. **Skill-aware guidance** — when the agent looks for "what to do," it consults
   *both* the runbook store (advisory text) and the learned-skill store
   (`DoltFormulaStore`, executable governed procedures). An ACTIVE, non-expired,
   confidently-matching skill outranks a runbook — the agent is steered to
   `run_skill` (whose per-step OPA re-check still applies) rather than improvise.
   Runbooks are the human-authored *prior* that solves cold-start; skills are the
   learned, human-approved *posterior* the agent's own resolved investigations
   eventually mint. This slice consumes the skills produced by the separate
   skill-learning pipeline; it does not build that pipeline.

## User Stories

1. As an operator, I want the SRE agent to choose its next tool based on the
   last tool's output, so that investigation follows the evidence instead of a
   fixed script.
2. As an operator, I want the SRE agent to call `observability_query`,
   `log_search`, and `runbook_read` in whatever order the incident demands, so
   that a metrics-first incident and a logs-first incident are both handled well.
3. As an operator, I want the SRE agent to re-query a tool with a refined query
   after seeing an initial result, so that it can narrow in on a root cause.
4. As an operator, I want the SRE agent to stop and deliver a report once it has
   enough evidence, so that it does not burn turns or tokens needlessly.
5. As an operator, I want the SRE agent to give up cleanly after a bounded
   number of turns, so that a confused model cannot loop forever.
6. As an operator, I want the final incident report to satisfy the existing SRE
   output schema (`timeline`, `likely_cause`, `severity`, `recommended_steps`,
   `runbook_ref`, `requires_human_approval`), so that downstream synthesis and
   the human gate keep working unchanged.
7. As an operator, I want the agent to mark `requires_human_approval=true`
   whenever a recommended step involves `shell_exec`, so that mutating actions
   still route through the human gate.
8. As a security reviewer, I want the agent to treat tool output (logs,
   runbooks) as data, never as instructions, so that an injected log line cannot
   make it call a forbidden tool.
9. As a security reviewer, I want a `shell_exec` attempt provoked by injected
   evidence to surface as a `tool_access_denied` error from the gateway, so that
   OPA remains the structural backstop and the attempt is auditable.
10. As an operator, I want the agent's token usage accumulated across every turn
    into `token_usage`, so that cost tracking stays accurate for multi-turn runs.
11. As an operator, I want the agent to abort when `token_budget` is exceeded,
    so that a runaway investigation cannot exceed its cost ceiling.
12. As an operator, I want a malformed (non-JSON) LLM turn to be met with a
    corrective re-prompt rather than a crash, so that a single bad turn does not
    fail the incident.
13. As an operator, I want a final `respond` action that violates the SRE schema
    to be rejected with a corrective re-prompt, so that only valid reports are
    returned.
14. As an operator, I want past incident summaries from memory injected as
    context at the start of the loop, so that recurring incidents resolve faster.
15. As an operator, I want a resolved incident's summary written back to memory,
    so that future similar incidents benefit from it.
16. As the supervisor graph, I want `incident`-classified tasks routed to the new
    dynamic SRE agent, so that the enhancement is live without other graph
    changes.
17. As an operator, I want `log_search` to return only the lines relevant to my
    query, so that an unrelated log volume does not drown the signal.
18. As an operator, I want `log_search` results ranked by relevance to the
    query, so that the most likely culprit lines appear first.
19. As an operator, I want `log_search` output capped at a maximum number of
    lines, so that the agent's context window is protected regardless of how
    large the underlying log source is.
20. As an operator, I want `log_search` to report how many total matches existed
    versus how many were returned, so that the agent knows when it is seeing a
    truncated view.
21. As an operator, I want `log_search` to return an empty, well-formed result
    when nothing matches, so that the agent can reason about "no evidence found."
22. As an operator, I want runbooks ingested into the existing memory store, so
    that runbook retrieval reuses infrastructure already running in the stack.
23. As an operator, I want `runbook_read` to accept an incident signature and
    return the most semantically similar runbook, so that the agent finds the
    right procedure without knowing its exact name.
24. As an operator, I want `runbook_read` to return a similarity score with the
    match, so that the agent can decide whether the runbook is relevant enough to
    follow.
25. As an operator, I want `runbook_read` to return a clear "no matching
    runbook" result below a relevance threshold, so that the agent describes
    remediation in `recommended_steps` instead of citing an irrelevant runbook.
26. As an operator, I want `runbook_ref` in the report populated from the matched
    runbook's identifier, so that the report cites the procedure it followed.
27. As an operator, I want to add a runbook by dropping a markdown file in
    `docs/runbooks/` and running one seed command, so that I do not have to learn
    a new format or hand-edit a database.
28. As an operator, I want runbook matching to key on each runbook's "When to
    use" description rather than its whole body, so that retrieval keys on the
    symptom, not on incidental wording deep in the procedure.
29. As an operator, I want re-running the seed to update edited runbooks and skip
    unchanged ones without creating duplicates, so that ingestion is safe to run
    repeatedly (e.g. on every deploy).
30. As an operator, I want a runbook file missing its "When to use" signature to
    be skipped with a warning (not silently ingested with an empty signature), so
    that a malformed runbook cannot poison retrieval.
31. As a maintainer, I want the seed step exposed both as a `make` target and an
    importable function, so that the stack can seed runbooks on startup and tests
    can seed a fixture corpus directly.
32. As an operator, I want the SRE agent to discover approved skills matching the
    current incident, so that it can reuse a vetted procedure instead of
    re-deriving remediation from scratch.
33. As an operator, I want an ACTIVE, non-expired skill that confidently matches
    the incident to take precedence over a runbook, so that the agent prefers the
    higher-trust, executable procedure over advisory text.
34. As an operator, I want the agent to fall back to runbook guidance when no
    skill matches, so that cold-start incidents (before any skill is learned) are
    still handled.
35. As an operator, I want the agent to execute a chosen skill via `run_skill`
    rather than re-implement its steps, so that the governed procedure (per-step
    OPA re-check, `on_failure`, success criteria) is the thing that runs.
36. As a security reviewer, I want every step of an executed skill re-checked
    against OPA with the SRE token, so that running an approved skill grants no
    authority the agent did not already have — a `shell_exec` step still hits the
    human gate.
37. As an operator, I want an expired or revoked skill to be ignored by guidance
    and to fall back to runbooks, so that stale learned procedures cannot be run.
38. As an operator, I want the report to cite both the executed skill id and its
    linked `runbook_ref`, so that I can trace what ran and read the human-readable
    procedure behind it.
39. As an operator, I want my resolved investigations to be captured as episodes
    eligible for the skill-learning pipeline, so that recurring remediations can
    later be promoted into skills and close the learning loop.
40. As a security reviewer, I want a learned skill to reach the agent only after
    human promotion (`skill:promote`), so that no machine-discovered procedure
    becomes executable without an operator's approval.
41. As a maintainer, I want the dynamic SRE agent's loop, the bounded
    `log_search`, and the semantic `runbook_read` each covered by unit tests that
    run without the Docker stack, so that the behaviour is fast to verify and
    regression-proof.
42. As a maintainer, I want at least one integration test that drives the new
    agent through the live gateway on an `incident` task, so that the wiring is
    proven end-to-end.
43. As a maintainer, I want the existing static `SREAgent` either replaced or
    clearly retired, so that there is one obvious SRE agent and no dead path.
44. As a maintainer, I want docs (`CLAUDE.md`, `ARCHITECTURE.md`, `README.md`,
    `PROGRESS.md`) updated when the slices go green, so that the next cold
    session understands the new SRE flow.

## Implementation Decisions

### Slice 1 — `DynamicSREAgent` (ReAct loop)

- Add a new agent class modelled on `DynamicCodeReviewerAgent`: an LLM-directed
  loop where each turn is exactly one JSON object, either
  `{"action": "call_tool", "tool": ..., "params": {...}}` or
  `{"action": "respond", "result": {...}}`.
- Reuse the established loop shape: `_init_token_usage`, `_llm_chat` (catching
  provider errors → `provider_error`), `_handle_respond_action` (schema-validate
  the final report, re-prompt on `ValidationError`), `_handle_tool_call` (call
  the gateway, append the tool result as the next user turn, convert
  `ToolAccessDenied` → `tool_access_denied` error state), and `_dispatch_action`.
- Validate the final `respond` payload against the existing `SRE_OUTPUT_SCHEMA`,
  not the reviewer schema.
- `allowed_tools` stays `["observability_query", "log_search", "runbook_read",
  "shell_exec"]`. The agent never calls `shell_exec` itself — it proposes it in
  `recommended_steps` with `requires_approval=true`; the existing human gate and
  OPA enforce it. (Confirmed: the prompt already states this contract.)
- Cap the loop with a `MAX_TURNS` constant (the reviewer uses 8; choose a value
  appropriate to three tools — start at 8). On exhaustion, return an
  `error.code = "max_turns_exceeded"` state.
- Accumulate `prompt_tokens`/`completion_tokens` into `token_usage` every turn.
  Honour `token_budget`: when completion tokens exceed the budget, abort with
  `error.code = "token_budget_exceeded"`, consistent with the static reviewer's
  budget check.
- Preserve memory behaviour: load top-k past incidents at loop start (injected
  into the opening user message) and write the resolved report back under the
  `incident:<thread_id[:8]}` key in the `sre` namespace.
- Author a `prompts/react_sre.md` system prompt analogous to
  `prompts/react_code_reviewer.md`: declare the per-turn JSON action contract,
  the available SRE tools, what to look for, the `shell_exec`/HITL rule, and the
  strict `SRE_OUTPUT_SCHEMA` for the final `respond` result.
- Wire the supervisor so `incident` tasks route to the dynamic SRE agent. The
  static `SREAgent` is retired (preferred) or kept only if a consumer still
  needs it; no silent dual path.

#### Prototype: per-turn action contract (from the reviewer ReAct loop)

```
# call a tool
{"action": "call_tool", "tool": "log_search", "params": {"query": "5xx checkout"}}

# deliver the final incident report (validated against SRE_OUTPUT_SCHEMA)
{"action": "respond", "result": {
  "timeline": "...", "likely_cause": "...", "severity": "P2",
  "recommended_steps": [{"action": "...", "rationale": "...", "requires_approval": false}],
  "runbook_ref": "RB-014", "requires_human_approval": false
}}
```

### Slice 2 — Bounded `log_search`

- The `log_search` tool in the `sre_stub` FastMCP server returns real matches
  against a seeded sample log source baked into the container image, mirroring
  the `linter_stub`/semgrep seeding pattern (rules/sample shipped in the image,
  not fetched at runtime).
- Tool contract: input is a `query` string; output is a structured dict with at
  least the matched lines (ranked most-relevant first), the count returned, and
  the total count of matches found, so the agent can detect truncation.
- Output is capped at a maximum number of lines (a module constant) regardless
  of how many lines match, protecting the agent's context window. Ranking is by
  relevance of the line to the query (a simple, explainable scheme — substring /
  term overlap — not an embedding model; keep it dependency-free per project
  rules).
- A no-match query returns a well-formed empty result (empty matches, zero
  counts), never an error.
- Parameter naming must avoid the MCPJungle `name` collision (already satisfied —
  the param is `query`).

### Slice 3 — Semantic `runbook_read`

- Runbooks are ingested into the existing `PostgresMemoryStore` (pgvector,
  `nomic-embed-text`, 768-dim) under a dedicated runbook namespace. No new vector
  database — Pinecone/Milvus/Chroma are explicitly rejected; the harness already
  runs pgvector.
- `runbook_read` accepts an incident signature (the param stays `runbook_name`
  for the flat-API contract, but its meaning becomes "incident signature to match
  against") and returns the most semantically similar runbook plus a similarity
  score.
- Below a relevance threshold, return a structured "no matching runbook" result
  so the agent sets `runbook_ref` to null and describes remediation in
  `recommended_steps`. The threshold should align with the memory layer's
  existing cluster threshold conventions (≈0.80) but is tunable.
- The matched runbook exposes a stable identifier that the agent surfaces as
  `runbook_ref` in the report.
- Ingestion is idempotent: re-running it does not duplicate runbook entries.

#### Ingestion mechanism

- **Source of truth:** the existing `docs/runbooks/*.md` files. They are
  currently orphaned (no code reads them); this slice makes them the canonical
  runbook corpus instead of inventing a new data format. Adding a runbook = drop
  a markdown file in `docs/runbooks/` and re-run ingestion.
- **Trigger:** a dedicated seed step, run after the stack is up — mirroring the
  existing "seed formulas" pattern for `DoltFormulaStore`. Expose it both as a
  `make` target (e.g. `make seed-runbooks`) and as an importable function so
  tests can call it directly. Re-running it is safe (idempotent, see below).
  Ingestion is *not* done lazily inside `runbook_read` — the read path only
  searches; it never writes.
- **What gets embedded vs stored:** each runbook's `**When to use:**` line is the
  embedded *signature* (the text matched against the incident signature). The
  full markdown body is stored as the entry value so the agent can read the
  procedure. This keeps retrieval matching on the symptom description, not on the
  whole document.
- **Identifier:** the runbook's stable id is its filename slug (e.g.
  `cost-spike`), used as the memory `key` and surfaced as `runbook_ref`.
- **Namespace:** a dedicated runbook namespace (e.g. `runbooks`), separate from
  the `sre` incident-memory namespace, so runbook retrieval and past-incident
  recall do not cross-contaminate.
- **Idempotency:** writes are keyed by filename slug; the store's
  `ON CONFLICT (namespace, key)` upsert means re-ingesting an unchanged file is a
  no-op and an edited file is updated in place — never duplicated.
- **Malformed runbook:** a file missing a `**When to use:**` line is skipped with
  a logged warning rather than failing the whole seed run (or, if stricter
  behaviour is preferred, fails loudly — decide during implementation, but do not
  silently ingest a runbook with an empty signature).

### Slice 4 — Skill-aware guidance

- **Two-tier guidance model.** Runbooks (slice 3) are the *advisory prior*:
  human-authored text the agent reads and reasons over. Learned skills
  (`DoltFormulaStore`, the skill-learning pipeline) are the *executable
  posterior*: governed `Formula` procedures that cleared a high bar (≥5
  independent RESOLVED episodes + human `skill:promote` + 90-day expiry) and run
  via `run_skill` with per-step OPA re-check. This slice makes the SRE agent
  *consume* both; it does **not** build the skill-learning pipeline (separate
  PRD, `.scratch/skill-learning/`).
- **Skill discovery surface (the likely gap).** `run_skill` executes a skill by
  ID, but there is no tool exposed to the SRE agent to *find* the matching skill.
  Add a read-only discovery tool (e.g. `skill_search`) backed by
  `DoltFormulaStore.lookup(agent_role="sre", task=signature)`, returning matching
  ACTIVE, non-expired skills with a match score and the skill id. This is the
  bridge that lets the agent get an id to pass to `run_skill`.
- **Unified guidance step + precedence.** When investigating an incident
  signature the agent consults both stores and is handed a single ranked, typed
  list — `{type: "skill"|"runbook", id/ref, score, executable}`. Precedence: a
  confidently-matching ACTIVE skill outranks a runbook (it has cleared a far
  higher trust bar). The prompt steers the agent to `run_skill(<id>)` when such a
  skill exists, and to read the runbook and reason when no skill matches
  (cold-start fallback). Confidence is a tunable threshold, separate from the
  runbook relevance threshold.
- **Safety is preserved, not bypassed.** Executing a skill is not a shortcut past
  authorization: `run_skill` re-checks every step against OPA with the SRE token
  (per skill-learning issue 06 — "promotion grants no authority"). A skill step
  that calls `shell_exec` still routes through the existing human gate. Including
  skills therefore *strengthens* the HITL story rather than weakening it.
- **Expired/revoked skills are invisible to guidance.** `lookup` must exclude
  `deprecated`/`revoked`/expired skills; a revoked skill becomes un-executable at
  the next invocation (issue 06). When the only match is stale, guidance falls
  back to runbooks.
- **Skill ↔ runbook link.** A skill may carry an optional `runbook_ref` (the
  human-readable runbook it documents/derived from). When the agent runs a skill,
  the report's `runbook_ref` is satisfied from the skill's link, so the report
  cites both *what executed* (skill id) and *the documented procedure*.
- **Closing the loop.** The dynamic SRE agent's resolved investigations are the
  episodes the skill-learning pipeline consumes (captured on the governance audit
  path, skill-learning issue 02). No new capture mechanism is built here; this
  slice only ensures the agent's runs produce the audit/outcome signal that
  pipeline already expects, so recurring remediations can later be promoted into
  skills the agent then discovers via `skill_search`.

### Cross-cutting

- `AgentState` is unchanged (it is `total=False` and already carries
  `token_usage`/`token_budget`/`memory_context`). The dynamic agent returns the
  same `agent_output`/`error`/`token_usage` shape the supervisor already handles.
- OPA policy, the `human_gate` node, and `human_approval_token` scoping are
  untouched — the enhancement deliberately does not modify the safety layer.
- Tool registration / `TOOL_NAME_MAP` entries for `log_search` and
  `runbook_read` already exist; only the tool bodies change.

## Testing Decisions

A good test here asserts **external behaviour at the highest existing seam**, not
internal structure. Test what an operator or the supervisor observes: the
sequence of tool calls, the returned report/error shape, the boundedness of
results — never private helper methods.

### Slice 1 seam — `agent.run(state)`

- Prior art is exact: `test_redteam_prompt_injection.py` tests
  `DynamicCodeReviewerAgent` at `agent.run(state)` using a scripted
  `_MockLLM`/`MockLLMProvider` that returns one JSON turn at a time, plus a
  recording mock gateway that appends each `call_tool` name. `test_phase3_agents.py`
  shows the SRE-specific `MockLLMProvider` (list-of-responses) and `_mock_gateway`
  (per-tool response dict, records `last_calls`).
- Unit tests (no Docker) to add against the dynamic SRE agent:
  - Multi-turn happy path: scripted turns drive
    `observability_query` → `log_search` → `runbook_read` → `respond`; assert the
    recorded `call_tool` sequence and a schema-valid `agent_output`.
  - Non-linear path: a turn re-queries a tool with a refined query after seeing a
    result; assert the second call carries the refined params.
  - `max_turns_exceeded`: never-respond LLM yields the bounded-loop error.
  - Malformed-JSON turn → corrective re-prompt → eventual valid report.
  - Invalid final `respond` (schema violation) → corrective re-prompt.
  - Injected-evidence safety: an LLM that "obeys" an injected log line by
    requesting `shell_exec` against a denying gateway yields
    `error.code == "tool_access_denied"` (mirrors the reviewer red-team test).
  - Token budget: a low `token_budget` aborts with `token_budget_exceeded`.
  - Memory: past-incident context is loaded into the opening message and a
    summary is written back on success.
- Integration test (`@pytest.mark.integration`): drive the dynamic SRE agent on
  an `incident` task through the live `GatewayClient`/gateway, mirroring
  `test_dynamic_reviewer_injection_blocked_and_dolt_audited`.

### Slice 2 seam — the `log_search` tool function

- Prior art: `test_unit_linter.py` exercises the linter stub's tool behaviour
  against seeded input without the full stack.
- Tests: a query returns only relevant lines; results are ranked most-relevant
  first; output is capped at the max-line constant even when more lines match;
  returned-vs-total counts are reported so truncation is detectable; a no-match
  query returns a well-formed empty result.

### Slice 3 seam — `runbook_read` over the memory store

- Prior art: `test_phase2_memory.py` covers `PostgresMemoryStore` semantic
  search behaviour and dimension handling.
- Tests: an incident signature returns the most similar runbook with a score; a
  signature with no good match returns the "no matching runbook" result below
  threshold; ingestion is idempotent (re-ingest does not duplicate). Where these
  require Postgres/Ollama, mark them `integration`; keep ranking/threshold logic
  unit-testable with a fake store where practical.
- Ingestion tests (seam: the importable seed function over a fixture runbook
  dir): the `**When to use:**` line is what gets embedded and the body is stored
  as the value; the filename slug becomes the key/`runbook_ref`; re-running the
  seed over an unchanged dir adds no rows and over an edited file updates in
  place; a runbook file missing its `**When to use:**` line is skipped (asserted
  via the warning / absence of an empty-signature entry). Drive these against a
  small fixture `docs/runbooks/`-shaped directory so they do not depend on the
  real corpus.

### Slice 4 seam — `agent.run(state)` with a stubbed skill store + recording gateway

- Prior art: the same `agent.run(state)` seam as slice 1 (scripted
  `MockLLMProvider` + recording mock gateway), plus `test_phase4_supervisor.py`
  and `test_hitl_promotion.py` for how `DoltFormulaStore`/skills are faked.
- Unit tests (no Docker), with a fake skill store returning canned matches and a
  recording gateway:
  - Skill precedence: when an ACTIVE skill confidently matches, guidance ranks it
    above the runbook and the agent drives a `run_skill` call (assert the
    recorded call carries the skill id), not an improvised tool sequence.
  - Cold-start fallback: no matching skill → the agent uses the runbook and the
    recorded calls show `runbook_read`, never `run_skill`.
  - Stale-skill fallback: the only match is expired/revoked → excluded from
    guidance → runbook fallback.
  - Safety: an executed skill whose step the gateway denies surfaces
    `tool_access_denied` (the per-step OPA backstop), proving execution is not a
    shortcut past authorization.
  - Report linkage: a run via a skill with a linked `runbook_ref` produces a
    report whose `runbook_ref` is populated from the skill.
- Integration test (`@pytest.mark.integration`): seed an ACTIVE skill (as in the
  skill-learning integration tests), drive the SRE agent on a matching incident,
  and assert it discovers and executes the skill end-to-end through the live
  gateway with OPA in the path.

All tests follow the project's red-before-green rule: write the failing test,
confirm it fails for the right reason, then implement.

## Out of Scope

- Any change to OPA policy, the `human_gate`, or `human_approval_token` scoping —
  the safety layer is already strong and stays as-is.
- Real production observability integrations (Datadog/Prometheus/CloudWatch,
  Elasticsearch/Splunk, `kubectl`/EC2). `observability_query` remains a stub in
  this PRD; only `log_search` and `runbook_read` gain realism.
- Actual sandboxed `shell_exec` execution (Docker-in-DinD). `shell_exec` stays a
  stub gated by HITL; the agent only ever *proposes* it.
- Map-reduce / embedding-based summarisation of logs. Boundedness is achieved at
  the tool boundary (ranked + capped); summarisation is a possible later PRD only
  if capping proves insufficient.
- Slack (or any external) approval webhook. HITL already works via the scoped
  approval token; a chat integration is a separate concern.
- Introducing a new vector database. Explicitly rejected — reuse pgvector.
- Building the skill-learning *production* pipeline (episode capture, outcome
  labeling, candidate clustering/proposal, the HITL promotion gate, `run_skill`
  execution itself). That is the separate `.scratch/skill-learning/` PRD and is a
  **dependency** of slice 4, not in-scope work. Slice 4 only adds the SRE agent's
  *consumption* of skills (discovery + precedence + report linkage) and assumes
  `run_skill` and a populated skill store exist. If they do not yet, slice 4's
  guidance degrades gracefully to runbook-only (cold-start) and the skill-precedence
  paths are exercised via the fake skill store in tests.

## Further Notes

- The static `SREAgent` and the static `CodeReviewerAgent` share the same
  linear "gather once, retry only on schema failure" shape. This PRD fixes the
  SRE side only; if the static reviewer is also retired in favour of
  `DynamicCodeReviewerAgent`, that is a separate PRD.
- Watch the documented gotchas while building: never name a tool parameter
  `name` (MCPJungle flat-API collision); FastMCP stubs must keep
  `enable_dns_rebinding_protection=False`; `PostgresMemoryStore` re-detects
  embedding dimension on model change and may drop/recreate its table.
- Maintain code-health ≥ 9 and run `/forensics` before committing, per project
  rules. The dynamic reviewer's small single-responsibility helpers
  (`_handle_*`, `_dispatch_action`) are the model to copy for keeping CCN low.

### Hosted deployment considerations

The core data-model and ingestion decisions are host-agnostic (idempotent,
slug-keyed upserts in shared Postgres; an importable seed function callable from
a deploy-time job). But moving off a single dev laptop surfaces three things this
PRD assumes and a hosted deployment must provide. They are deployment/infra
concerns, recorded here so they are not discovered the hard way — full
hosted-readiness is **out of scope** for this feature PRD.

- **Runbook source files must travel into the deployment.** Ingestion reads
  `docs/runbooks/*.md`; in a container those files must be baked into the image
  (`COPY`) or mounted (volume/ConfigMap). A repo-relative path that was never
  copied in will silently find zero runbooks.
- **The seed runs as a deploy-time job, not `make`.** `make seed-runbooks` is a
  dev affordance. Hosted, ingestion runs once per deploy via an init container /
  k8s Job / entrypoint hook — run once, not per-replica, to avoid concurrent
  seeds racing (the upsert is per-row safe, but a single seed job is cleaner).
  This is exactly why the seed is specified as an importable function, not only a
  `make` target.
- **Embeddings need a hosted endpoint — this is the real gap.** Both ingestion
  and `runbook_read` search call `PostgresMemoryStore._embed()`, which is
  hardwired to an Ollama host and, per CLAUDE.md, reached via
  `host.docker.internal:11434` (Docker Desktop only — does not resolve in hosted
  Linux/k8s). The chat-provider abstraction (`OllamaProvider`/`GeminiProvider`/
  `OpenRouterProvider`) is **chat-only**; the memory store has no provider
  seam for embeddings. Hosted options: (a) deploy Ollama as its own service/pod
  (GPU) and point `OLLAMA_HOST`/`EMBED_MODEL` at its service DNS — smallest
  change, keeps `nomic-embed-text`; or (b) introduce an `EmbeddingProvider`
  protocol with a hosted API backend (OpenAI/Voyage/Cohere/Gemini/OpenRouter —
  **correction, 2026-08-03: OpenRouter added an OpenAI-shaped
  `POST /api/v1/embeddings` endpoint since this was originally written, so it's
  now a real option, not excluded as the original note here claimed**). Filed
  as [issue 08 — pluggable embedding provider](issues/08-pluggable-embedding-provider.md):
  build the seam + `OllamaEmbeddingProvider` now, add Gemini/OpenRouter
  implementations when a hosted deployment actually needs a second backend.
- **Embedding-dimension table recreate is a hosted footgun.** `PostgresMemoryStore`
  drops and recreates its table when the embedding model/dimension changes. On a
  shared hosted DB with multiple replicas calling `setup()`, a model swap could
  wipe live runbooks and incident memory and race across replicas. A hosted
  deployment should pin `EMBED_MODEL` and gate dimension changes behind an
  explicit migration, not the auto-recreate path.
