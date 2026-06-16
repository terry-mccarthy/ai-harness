---
title: "HITL promotion gate"
status: ready-for-human
type: HITL
---

## What to build

Add `POST /candidates/{id}/promote` to the governance service. This is the human gate: a reviewer inspects the candidate (via `GET /candidates/{id}` from issue 04) and explicitly promotes it to a governed skill.

The promotion action:
1. Re-validates all eligibility criteria against the current episode set (criteria may have changed since proposal).
2. If re-promoting an existing skill (same `cluster_key`), computes a diff of the `proposed_procedure` against the prior version and includes it in the response for review.
3. Creates a `skills` row: `version = prior_version + 1` (or 1 if new), `promoted_by` = the human principal from the request JWT, `source_candidate_id` = the candidate ID, `status = ACTIVE`, `expires_at = NOW() + 90 days`.
4. Transitions the candidate to `status = PROMOTED`.
5. `CALL DOLT_COMMIT` with message `skill: promoted from candidate <id> by <human>`.

Rejection path: `POST /candidates/{id}/reject` with a required `reason` field. Sets candidate `status = REJECTED`, records the reason, commits to Dolt. Rejected candidates remain queryable but are excluded from future clustering passes.

OPA policy: both promote and reject require a `skill:promote` scope. This scope is intentionally NOT in any agent role — it must be held only by named human operators. Add a `human-operator` client to governance's OAuth config with this scope.

The endpoint is HITL: the implementation can be built by an agent, but each actual promotion or rejection requires a human to call it with their operator credentials.

## Acceptance criteria

- [ ] `POST /candidates/{id}/promote` with `skill:promote` scope creates an ACTIVE skill row with `promoted_by` non-null and `expires_at` set 90 days out
- [ ] Promotion commits to Dolt with reviewer identity in the commit message
- [ ] Re-promotion creates a new version and returns a procedure diff
- [ ] `POST /candidates/{id}/reject` records reason and sets status REJECTED
- [ ] Agent-role tokens (architect, sre, code_reviewer) are rejected with 403
- [ ] `human-operator` OAuth client exists and can obtain a token with `skill:promote` scope
- [ ] Integration test: full episode → candidate → promote flow with a human-operator token

## Blocked by

- [04 — Manual candidate proposal](04-manual-candidate-proposal.md)
