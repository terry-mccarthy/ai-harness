---
title: "Skill conflict resolution and human escalation"
status: ready-for-agent
type: AFK
---

## What to build

When multiple ACTIVE skills match an incident, selection must be deterministic and OPA-shaped — not an agent choice. Add a `POST /skills/select` endpoint to governance that implements the spec's ordered resolution rules and escalates when no winner can be determined.

Request body:
```json
{
  "alert_signature": "sre.observability_query:latency-spike",
  "service_class": "stateless-api",
  "env_fingerprint": { "...": "runtime environment snapshot" },
  "invoking_principal": "sre-agent-1"
}
```

Selection rules (applied in order):
1. **Precondition specificity** — score each ACTIVE skill's `preconditions` against the request. More specific matches (more env_constraints matched) win.
2. **Recency of validation** — among tied skills, the most recently promoted wins.
3. **Trailing success rate** — computed from `audit_log` entries matching the skill's `cluster_key` over the past 30 days. Higher wins.
4. **Escalation** — if no deterministic winner remains after all three rules (e.g. two skills tied on all three), return `{"selected": null, "escalate": true, "reason": "..."}` rather than picking arbitrarily.

On a deterministic win: return `{"selected": "<skill_id>", "rationale": {"rule": "...", "score": ...}}`. Log the selection rationale to `audit_log` with a dedicated `tool_name = "skill:select"` entry so every invocation is auditable.

The escalation response should include enough context (the tied skill IDs and their scores) for an operator to manually pick one and call `POST /skills/{id}/run` directly.

## Acceptance criteria

- [ ] `POST /skills/select` returns the most specific matching ACTIVE skill when one clearly wins
- [ ] Tied specificity resolves by promotion recency
- [ ] Tied recency resolves by trailing success rate
- [ ] Three-way tie (or no winner after all rules) returns `escalate: true` with tied skill IDs
- [ ] Every selection (win or escalate) is written to `audit_log`
- [ ] Integration test: three skills with overlapping preconditions; verify correct winner at each tiebreak layer
- [ ] Integration test: exact tie returns escalation response

## Blocked by

- [06 — Skill execution with per-step OPA re-check and revocation](06-skill-execution-revocation.md)
