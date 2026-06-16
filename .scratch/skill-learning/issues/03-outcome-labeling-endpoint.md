---
title: "Independent outcome labeling endpoint"
status: ready-for-agent
type: AFK
---

## What to build

Add `POST /episodes/{episode_id}/label` to the governance service. This endpoint sets the `outcome`, `outcome_signal`, and `outcome_labeled_at` on an episode — and enforces the `[HARD]` rule that the labeler cannot be the same principal that performed the actions.

Request body:
```json
{
  "outcome": "RESOLVED | FAILED | ROLLED_BACK | HUMAN_OVERRIDE | INCONCLUSIVE",
  "outcome_signal": { "...": "machine-checkable post-action metrics" },
  "labeler_principal": "human-or-independent-agent-id"
}
```

Validation:
- `episode_id` must exist and have `outcome_labeled_at = NULL` (no re-labeling).
- `labeler_principal` must differ from the episode's `agent_principal`. Return 409 with a clear error if the same principal attempts to self-label.
- `outcome_signal` must be non-empty JSON (not `{}`). Return 422 if missing or empty — an empty signal is the self-declared-success antipattern.
- `outcome` must be one of the five enum values.

On success: write all three fields to Dolt and `CALL DOLT_COMMIT` with message `episode: <id> labeled <outcome>`. Return 200 with the updated episode record.

OPA policy: labeling requires a `episode:label` scope. Add this scope to the `sre` and `code_reviewer` roles (they are the natural outcome observers).

## Acceptance criteria

- [ ] `POST /episodes/{id}/label` with a different principal returns 200 and commits to Dolt
- [ ] Same-principal label attempt returns 409
- [ ] Empty `outcome_signal` returns 422
- [ ] Re-labeling an already-labeled episode returns 409
- [ ] `dolt_log` shows a commit per labeling event
- [ ] OPA policy rejects principals without `episode:label` scope
- [ ] Integration tests cover all four rejection cases

## Blocked by

- [02 — Episode capture on governance audit path](02-episode-capture.md)
