---
title: "Extract learning router (episodes, candidates, label, promote, reject)"
status: ready-for-agent
type: AFK
---

## What to build

Pull the **skill-learning pipeline endpoints** out of `services/governance/server.py` into `services/governance/routers/learning.py`.

Endpoints in scope:

- `GET /episodes` — list episodes
- `POST /episodes/{episode_id}/label` — outcome labeling
- `GET /candidates` — list candidates
- `POST /candidates` — propose a candidate from a set of episodes (with N/K/M criteria)
- `GET /candidates/{candidate_id}` — fetch one candidate
- `POST /candidates/{candidate_id}/promote` — HITL promotion to active skill
- `POST /candidates/{candidate_id}/reject` — HITL rejection

These move together along with their private helpers:

- `_VALID_OUTCOMES`, `_validate_label_body`, `_check_episode_labelable`
- `_N_MIN`, `_K_MIN`, `_M_MIN`, `_RECENT_DAYS`
- `_fetch_and_qualify_episodes`, `_compute_support_stats`, `_check_count_criteria`, `_check_diversity_criteria`, `_check_candidate_criteria`
- `_fetch_candidate_or_404`, `_fetch_latest_skill`, `_compute_procedure_diff`, `_insert_skill`

OPA checks (`_check_opa_label`, `_check_opa_propose`, `_check_opa_promote` — now unified in `core/opa.py` after slice 01) are called as `check_opa("harness/label_allowed", ...)` etc. from this router.

## Acceptance criteria

- [ ] `services/governance/routers/learning.py` exists with an `APIRouter`
- [ ] All seven endpoints respond at unchanged paths
- [ ] All helper functions listed above live in the same module (or a sibling `routers/_learning_helpers.py` if it improves readability)
- [ ] `server.py` no longer defines any of these endpoints inline
- [ ] `make test-integration` passes — in particular `test_episode_capture.py`, `test_outcome_labeling.py`, `test_candidate_proposal.py`, `test_hitl_promotion.py`
- [ ] Promotion still writes a new `skills` row + Dolt commit

## Blocked by

- [01 — Extract governance core infrastructure into a `core/` package](01-extract-governance-core-infra.md)
