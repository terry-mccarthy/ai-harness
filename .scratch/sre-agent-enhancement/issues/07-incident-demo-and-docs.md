---
title: "End-to-end incident demo and doc reconciliation"
status: ready-for-agent
type: AFK
---

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — capstone.

## What to build

A scripted, repeatable end-to-end incident demo that exercises the whole enhanced
SRE flow on realistic seeded fixtures — the PRD's named first-class deliverable —
plus a final reconciliation of the cross-cutting docs.

The demo drives a representative `incident` task through the supervisor into the
dynamic SRE agent and shows a non-linear investigation: querying observability,
pulling **bounded** logs from the seeded source (slice 2), retrieving the
matching runbook **semantically** (slice 4), and — when a seeded ACTIVE skill
matches — discovering it (slice 5) and executing it via `run_skill` with OPA in
the path (slice 6). It produces a schema-valid incident report citing the
`runbook_ref` (and skill id when one ran). The point is to prove the architecture
works together on plausible data, not on a real production estate.

Then reconcile `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, and `PROGRESS.md` so
they describe the final dynamic-SRE flow, the seeded fixtures, and the
skills↔runbooks guidance model.

## Acceptance criteria

- [ ] A scripted demo runs an incident end-to-end and emits a schema-valid report referencing the retrieved runbook
- [ ] The demo visibly exercises non-linear tool selection, bounded logs, semantic runbook retrieval, and (with a seeded skill) skill discovery + execution
- [ ] The demo is repeatable from documented setup (seed fixtures + stack up) and does not depend on any real external monitoring/CI system
- [ ] `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, `PROGRESS.md` reconciled to the final SRE flow and the prior/posterior guidance model
- [ ] Test counts / config tables in the docs reflect the slices delivered

## Blocked by

- [01 — DynamicSREAgent ReAct loop](01-dynamic-sre-react-loop.md)
- [02 — Bounded log_search over a seeded log source](02-bounded-log-search.md)
- [04 — Semantic runbook_read over the seeded corpus](04-semantic-runbook-read.md)
- [06 — Skill-aware guidance and precedence in the SRE agent](06-skill-aware-guidance.md)
