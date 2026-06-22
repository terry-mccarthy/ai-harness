---
title: "skill_search discovery tool"
status: ready-for-agent
type: AFK
---

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — Slice 4 (discovery surface).

## What to build

`run_skill` executes a learned skill by ID, but there is no tool exposed to the
SRE agent to *find* the matching skill. Add a read-only discovery tool
(`skill_search`) backed by `DoltFormulaStore.lookup(agent_role="sre",
task=signature)` that returns matching skills with a match score and the skill
id — the bridge that lets the agent obtain an id to pass to `run_skill`.

Behaviour: given an incident signature, return ACTIVE, non-expired skills for the
`sre` role ranked by match score, each with its id. Exclude
`deprecated`/`revoked`/expired skills entirely, so stale learned procedures are
never surfaced. A signature with no qualifying skill returns a well-formed empty
result.

This slice consumes the skills produced by the separate skill-learning pipeline;
it does not build that pipeline. With no skills seeded, the tool returns empty —
which is the cold-start signal that drives runbook fallback in issue 06.

## Acceptance criteria

- [ ] `skill_search` returns ACTIVE, non-expired `sre` skills matching the signature, each with a score and id
- [ ] `deprecated`, `revoked`, and expired skills are excluded from results
- [ ] A signature with no qualifying skill returns a well-formed empty result
- [ ] The tool is read-only — it never executes a skill or mutates skill state
- [ ] Tests cover match, exclusion-of-stale, and empty cases without the full Docker stack
- [ ] Docs updated when green

## Blocked by

None - can start immediately.
