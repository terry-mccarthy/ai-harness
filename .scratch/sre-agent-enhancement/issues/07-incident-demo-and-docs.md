---
title: "SRE enhancement doc reconciliation"
status: ready-for-agent
type: AFK
---

## Implementation status (audited 2026-08-02, revised 2026-08-03)

Demo requirement removed from this issue (2026-08-03) — `scripts/demo_sre.py`
already exists (146 lines) and isn't tracked here anymore; if it needs
updating once 02/04/06 land, that's incidental to those issues, not this one.
This issue is now doc reconciliation only.

`CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, and `docs/dev/memory-agents.md`
already reference `DynamicSREAgent` / the dynamic SRE flow. `PROGRESS.md`
does not exist anywhere in this repo, so that reconciliation AC can't be
satisfied until it's created (or the AC is dropped). Still blocked in
substance by the open gaps in 02 (log_search's threshold/counts), 04 (runbook
no-match threshold), and 06 (`run_skill` wiring, skill↔runbook report
linkage) — docs can't accurately describe a final state that doesn't exist
yet.

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — capstone.

## What to build

Reconcile `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, and `PROGRESS.md` so
they describe the final dynamic-SRE flow, the seeded fixtures, and the
skills↔runbooks guidance model, once 02/04/06 are closed out.

## Acceptance criteria

- [ ] `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, `PROGRESS.md` reconciled to the final SRE flow and the prior/posterior guidance model
- [ ] Test counts / config tables in the docs reflect the slices delivered

<details>
<summary>Original acceptance criteria (2026-08-02, superseded — kept for history)</summary>

- [ ] A scripted demo runs an incident end-to-end and emits a schema-valid report referencing the retrieved runbook
- [ ] The demo visibly exercises non-linear tool selection, bounded logs, semantic runbook retrieval, and (with a seeded skill) skill discovery + execution
- [ ] The demo is repeatable from documented setup (seed fixtures + stack up) and does not depend on any real external monitoring/CI system
- [ ] `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, `PROGRESS.md` reconciled to the final SRE flow and the prior/posterior guidance model
- [ ] Test counts / config tables in the docs reflect the slices delivered

</details>

## Blocked by

- 02
- 04
- 06

Issue 02 — Bounded log_search over a seeded log source. Issue 04 — Semantic
runbook_read over the seeded corpus. Issue 06 — Skill-aware guidance and
precedence in the SRE agent.
