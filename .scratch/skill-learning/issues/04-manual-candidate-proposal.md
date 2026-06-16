---
title: "Manual candidate proposal"
status: ready-for-agent
type: AFK
---

## What to build

Add `POST /candidates` to the governance service. A human (or operator script) calls this with a set of episode IDs and a proposed skill procedure, creating a candidate row in Dolt.

Request body:
```json
{
  "episode_ids": ["uuid1", "uuid2", "..."],
  "cluster_key": "sre.observability_query:latency-spike",
  "proposed_procedure": { "...": "skill body (§4 of spec)" }
}
```

Validation:
- All `episode_ids` must exist and have `outcome = RESOLVED` and a non-null `outcome_labeled_at`. Return 422 listing any that don't qualify.
- `episode_ids` must have at least `N_min = 5` members (spec §5 default). Return 422 with the count if below threshold.
- Episodes must span at least `K = 2` distinct `agent_principal` values (independence criterion). Return 422 if not met.
- At least `M = 2` episodes must have `outcome_labeled_at` within the trailing 90 days (recency criterion). Return 422 if not met.

`support_stats` is computed and stored automatically from the episode set — the human doesn't supply it.

On success: write a `candidates` row with `status = PROPOSED`, `support_stats` populated, and `CALL DOLT_COMMIT`. Return 201 with the candidate ID.

Also add `GET /candidates/{id}` returning the candidate with its `support_stats` and `member_episode_ids`, so the HITL reviewer (issue 05) has the full picture.

OPA policy: `POST /candidates` requires a `candidate:propose` scope. Add to `sre` and `code_reviewer` roles.

## Acceptance criteria

- [ ] `POST /candidates` with 5+ qualified, independent, recent RESOLVED episodes returns 201 and commits to Dolt
- [ ] Returns 422 with a clear reason for each of: below N_min, below K distinct principals, below M recent episodes, any episode not RESOLVED+labeled
- [ ] `support_stats` in the stored row reflects the computed criterion values
- [ ] `GET /candidates/{id}` returns full candidate including member episodes
- [ ] OPA rejects principals without `candidate:propose` scope
- [ ] Integration tests cover the happy path and all four rejection cases

## Blocked by

- [03 — Independent outcome labeling endpoint](03-outcome-labeling-endpoint.md)
