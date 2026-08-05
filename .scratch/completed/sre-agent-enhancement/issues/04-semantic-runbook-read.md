---
title: "Semantic runbook_read over the seeded corpus"
status: done
type: AFK
---

## Implementation status (audited 2026-08-02)

Partial. `harness_memory/runbook_retriever.py` returns the top-3 semantically
similar runbooks with id/signature/score — the core retrieval works and is
covered by 6 passing unit tests in `test_unit_runbook_retriever.py`. Missing:
no relevance threshold exists anywhere in the codebase (grepped for
`threshold`/`0.8` — only `consolidation.py`'s unrelated `CLUSTER_THRESHOLD`
turns up), so there is no "no matching runbook" sentinel result below ≈0.80 —
it always returns whatever it finds, however weak the match. Remaining work:
add the threshold and the below-threshold empty/no-match result the agent
needs to null out `runbook_ref`.

**Ownership note (2026-08-03):** issue 02 (`log_search`) needs the identical
relevance-threshold primitive on `PostgresMemoryStore.search()` to compute its
`returned_count`/`total_count`. To avoid both issues building it independently
in parallel worktrees and colliding on merge, **this issue owns building the
shared primitive** (a `min_score` param on `PostgresMemoryStore.search()`).
Issue 02 is now `Blocked by` this one and just consumes what lands here.

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — Slice 3 (semantic read).

## What to build

Replace the by-name keyword `runbook_read` stub with semantic retrieval over the
runbooks seeded into `PostgresMemoryStore` (slice 3 / issue 03).

Behaviour: `runbook_read` accepts an incident signature (the param stays
`runbook_name` for the flat-API contract, but its meaning becomes "incident
signature to match against") and returns the most semantically similar runbook
plus a similarity score and the runbook's stable identifier (its slug). Below a
relevance threshold it returns a structured "no matching runbook" result so the
agent sets `runbook_ref` to null and describes remediation in `recommended_steps`
instead of citing an irrelevant runbook. The threshold aligns with the memory
layer's existing conventions (≈0.80) but is tunable.

The matched runbook's identifier is what the agent surfaces as `runbook_ref` in
its report.

Build the threshold as a `min_score: float | None = None` param on
`PostgresMemoryStore.search()` (`memory_store.py:159`) — when set, filters rows
by `1 - (embedding <=> $1::vector) >= min_score` server-side rather than
post-filtering in Python. `retrieve_runbooks()` passes ≈0.80; issue 02's
`retrieve_logs()` will pass its own value once this lands.

## Acceptance criteria

- [ ] `PostgresMemoryStore.search()` gains a `min_score` param that filters results server-side
- [ ] An incident signature returns the most semantically similar runbook with a similarity score
- [ ] The returned match exposes a stable identifier (slug) usable as `runbook_ref`
- [ ] A signature with no good match returns the "no matching runbook" result below the relevance threshold
- [ ] Ranking/threshold logic is unit-testable with a fake store; store-backed paths are marked `integration`
- [ ] Docs updated when green

## Blocked by

- 03

Issue 03 — Runbook ingestion seed into pgvector.
