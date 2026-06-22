---
title: "Skill-aware guidance and precedence in the SRE agent"
status: ready-for-agent
type: AFK
---

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — Slice 4 (consumption + precedence).

## What to build

Make the dynamic SRE agent consult *both* knowledge tiers when it looks for
"what to do," and prefer the higher-trust, executable one. Runbooks (slice 3) are
the advisory prior; learned skills (`DoltFormulaStore`, surfaced via
`skill_search` from issue 05) are the executable posterior, run via `run_skill`
with per-step OPA re-check.

Behaviour: when investigating an incident signature the agent obtains both a
skill match (issue 05) and a runbook match (issue 04). A confidently-matching
ACTIVE skill **outranks** a runbook — the agent is steered to `run_skill(<id>)`
rather than improvise. When no skill matches (cold-start), it falls back to
reading the runbook and reasoning. Confidence is a tunable threshold, separate
from the runbook relevance threshold.

Executing a skill is **not** a shortcut past authorization: `run_skill`
re-checks every step against OPA with the SRE token (promotion grants no
authority). A skill step that calls `shell_exec` still routes through the
existing human gate. The report cites both the executed skill id and the skill's
linked `runbook_ref`, so `runbook_ref` is satisfied from the skill when one ran.

The agent's resolved investigations are the episodes the skill-learning pipeline
consumes (captured on the existing governance audit path) — this slice builds no
new capture mechanism, it only ensures the agent's runs produce that signal so
the learning loop can later mint skills the agent then discovers.

External dependency: `run_skill` is delivered by the skill-learning PRD. Until it
exists, the agent's call to `run_skill` is recorded/mocked in tests; with no
skills seeded, guidance degrades gracefully to runbook-only.

## Acceptance criteria

- [ ] Skill precedence: an ACTIVE, confidently-matching skill ranks above the runbook and the agent drives a `run_skill` call carrying the skill id (not an improvised tool sequence)
- [ ] Cold-start fallback: with no matching skill, the agent uses the runbook and never calls `run_skill`
- [ ] Stale-skill fallback: when the only match is expired/revoked it is excluded and guidance falls back to runbooks
- [ ] Safety backstop: an executed skill whose step the gateway denies surfaces `tool_access_denied`, proving execution is not a shortcut past authorization
- [ ] Report linkage: a run via a skill with a linked `runbook_ref` produces a report whose `runbook_ref` is populated from the skill
- [ ] Unit tests use a fake skill store + recording gateway; one integration test seeds an ACTIVE skill and drives discovery + execution end-to-end through the live gateway with OPA in the path
- [ ] Docs updated when green

## Blocked by

- [01 — DynamicSREAgent ReAct loop](01-dynamic-sre-react-loop.md)
- [04 — Semantic runbook_read over the seeded corpus](04-semantic-runbook-read.md)
- [05 — skill_search discovery tool](05-skill-search-discovery-tool.md)
