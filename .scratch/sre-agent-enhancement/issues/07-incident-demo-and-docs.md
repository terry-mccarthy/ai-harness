---
title: "SRE enhancement doc reconciliation"
status: done
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

- [x] `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, `PROGRESS.md` reconciled to the final SRE flow and the prior/posterior guidance model
- [x] Test counts / config tables in the docs reflect the slices delivered

## Closed 2026-08-03

02/04/06 all landed, unblocking this issue. `PROGRESS.md` created at the repo
root with the SRE Agent Enhancement PRD recorded as a completed phase
(4 slices + issue 08 as a related cross-cutting addition), including the
deliberate divergences from spec (semantic search over the PRD's original
keyword scheme; `run_skill` as a native in-agent dispatch rather than reuse
of either pre-existing "run a skill" mechanism, both of which had real
authorization problems).

Authoritative test counts from this session's `pytest --collect-only` runs:
**700 total / 394 unit / 285 integration / 19 eval.** `README.md`,
`docs/tests.md`, and `ARCHITECTURE.md` previously disagreed (699/393 vs.
700/394) and reconciled to these numbers. `README.md`'s `EMBED_MODEL` config
row gained a sibling `EMBEDDING_PROVIDER` row (issue 08 config knob was
undocumented there).

Stale docs found and fixed:
- `README.md` project-layout line still said `SREAgent` (the retired static
  agent) instead of `DynamicSREAgent`.
- `ARCHITECTURE.md`'s OPA policy table for the `sre` role was missing
  `skill_search` (present in `policies/harness.rego` since issue 05) and had
  no mention of `run_skill`'s OPA-exempt native-dispatch status.
- `ARCHITECTURE.md`'s "Test Coverage" section headline counts (Integration:
  221, Eval: 13) were stale relative to reality (285 / 19) and, in the
  Integration table's own case, relative to its own row sum (~197) — a
  pre-existing drift that predates this PRD. Corrected the headline numbers,
  added the two SRE-PRD-specific integration rows, added the eval suite's
  missing `test_eval_architect.py` row, and pointed to `docs/tests.md` as
  the exhaustive source of truth rather than silently re-deriving a table
  that was already known-incomplete before this PRD started.
- `ARCHITECTURE.md`'s request-flow sequence diagram and narrative didn't
  distinguish `run_skill`'s native dispatch from the normal
  governance→MCPJungle→tool path every other SRE tool takes — added a
  clarifying note.
- `docs/sre.md` (the primary "how the SRE agent works" doc, linked from
  README) still described the **deleted** auto-execute-formula-steps
  behavior ("its steps are injected... the agent follows the steps rather
  than reasoning from scratch") and didn't mention `run_skill`, the
  relevance thresholds, or the truncation-count fields at all. Rewrote the
  "Skill-guided investigation" section and the tools table to match
  `dynamic_sre.py`'s actual current behavior.

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
