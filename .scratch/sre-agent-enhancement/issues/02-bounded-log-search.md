---
title: "Bounded log_search over a seeded log source"
status: ready-for-agent
type: AFK
---

## Implementation status (audited 2026-08-03, revised)

Decision made: keep the semantic (pgvector/Ollama) approach over the PRD's
original dependency-free substring/term-overlap design. Postgres+Ollama is
already mandatory infra for this same agent's `runbook_read` and memory, so
this isn't a new dependency, and semantic matching genuinely finds relevant
lines a keyword scheme would miss (e.g. "checkout errors" → a log line reading
"payment-svc 502" with zero lexical overlap). This issue's ACs are revised
below to match; the "seeded log source baked into the container image" /
"dependency-free ranking" language from the original PRD no longer applies.

**Corrected finding on the cap:** an earlier pass of this audit claimed the
line cap was caller-overridable and therefore unenforced. That was wrong —
the MCP tool `log_search(query: str)` (`stub_servers/sre_server.py:87`) only
exposes `query`; `top_k` is never threaded through from the tool call, so the
5-line cap in `retrieve_logs(store, query, top_k=5)` is in fact
non-bypassable by the agent today. Two real gaps remain, both narrower than
originally scoped:

1. **No relevance threshold, so "returned vs. total" is undefined.**
   `PostgresMemoryStore.search()` (`memory_store.py:159`) is a pure top-K
   nearest-neighbor query — `ORDER BY embedding <=> $1 LIMIT $3` — with no
   `WHERE score >= threshold` filter. It always returns the 5 closest rows in
   the namespace, however irrelevant. Before `returned_count`/`total_count`
   can mean anything, "match" needs a similarity floor to be defined against.
   **Ownership (2026-08-03): issue 04 builds this** (a `min_score` param on
   `PostgresMemoryStore.search()`, since it needs the identical primitive for
   its "no matching runbook" result) — this issue is now `Blocked by` 04 and
   just consumes what lands there, rather than both issues building it
   independently in parallel worktrees and colliding on merge.
2. **No test against the real seeded fixtures.** `test_unit_log_retriever.py`
   only exercises `retrieve_logs()` against a `_FakeStore` — nothing proves
   ranking behaves sensibly against the actual `docs/logs/*.jsonl` seed data
   through a real (or realistically faked) embedding path. Given the PRD's
   own "realistic fixtures are the dependency, don't ship hollow scaffolding"
   principle, this is worth closing before calling the slice done.

Minor, optional: name the `top_k=5` default as an explicit, documented module
constant (e.g. `MAX_LOG_RESULTS`) rather than an implicit kwarg default —
functionally identical, just more discoverable/tunable.

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — Slice 2.

## What to build (revised 2026-08-03 — supersedes original wording below)

`log_search` keeps its current semantic (pgvector/Ollama) retrieval via
`harness_memory/log_retriever.py` — that decision is made, see Implementation
status above. Remaining work is narrow:

1. Consume the `min_score` param issue 04 adds to `PostgresMemoryStore.search()`
   — do not reimplement it here.
2. Use that threshold in `retrieve_logs()` to report `returned_count` (rows
   actually returned, ≤ the cap) and `total_count` (rows scoring above the
   threshold in the namespace) so the agent can detect truncation.
3. Add a test that seeds `docs/logs/*.jsonl` into a real or realistically
   faked store and asserts ranking/truncation behaviour against it, not just
   against a hand-built `_FakeStore`.
4. Optional: name the line cap as an explicit module constant instead of an
   implicit `top_k=5` kwarg default.

## Revised acceptance criteria

- [ ] `retrieve_logs()` reports `returned_count` and `total_count` (matches
      above the relevance threshold), so truncation is detectable
- [ ] Uses the `min_score` param issue 04 adds to `PostgresMemoryStore.search()`
      — no independent threshold implementation
- [ ] A no-match query returns a well-formed empty result (empty `logs`, zero
      counts), not an error — verify this still holds once the threshold
      lands
- [ ] At least one test exercises `log_search` against the real seeded
      `docs/logs/*.jsonl` fixtures, not only a fake store
- [ ] Docs updated when green

<details>
<summary>Original acceptance criteria (2026-08-02, superseded — kept for history)</summary>

- [ ] A query returns only lines relevant to it, drawn from the seeded log source
- [ ] Results are ranked most-relevant first
- [ ] Output is capped at the max-line constant even when more lines match
- [ ] The result reports returned-count and total-match-count so truncation is detectable
- [ ] A no-match query returns a well-formed empty result (empty matches, zero counts), not an error
- [ ] Tests exercise the tool against the seeded source without the full Docker stack
- [ ] Docs updated when green

Original "What to build" called for a dependency-free substring/term-overlap
scheme over a container-baked log source, mirroring `linter_stub`. Superseded
by the semantic-search decision above.

</details>

## Blocked by

- 04

Issue 04 — Semantic runbook_read over the seeded corpus — owns the shared
`min_score` primitive on `PostgresMemoryStore.search()` this issue consumes.
