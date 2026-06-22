---
title: "Semantic runbook_read over the seeded corpus"
status: ready-for-agent
type: AFK
---

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

## Acceptance criteria

- [ ] An incident signature returns the most semantically similar runbook with a similarity score
- [ ] The returned match exposes a stable identifier (slug) usable as `runbook_ref`
- [ ] A signature with no good match returns the "no matching runbook" result below the relevance threshold
- [ ] Ranking/threshold logic is unit-testable with a fake store; store-backed paths are marked `integration`
- [ ] Docs updated when green

## Blocked by

- [03 — Runbook ingestion seed into pgvector](03-runbook-ingestion-seed.md)
