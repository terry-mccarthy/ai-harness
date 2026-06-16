---
title: "Extract skills router (skill CRUD, expire, select)"
status: ready-for-agent
type: AFK
---

## What to build

Pull the **skill lifecycle endpoints** out of `services/governance/server.py` into `services/governance/routers/skills.py`.

Endpoints in scope:

- `GET /skills` — list active skills
- `GET /skills/{skill_id}` — fetch one skill
- `POST /skills/{skill_id}/revoke` — revoke an active skill
- `POST /skills/expire` — expire overdue skills + auto-propose re-validation candidates + flag low-success skills
- `POST /skills/select` — ordered tiebreak (specificity → recency → success-rate) + escalation

These move together along with their private helpers:

- Expiry pass: `_find_expired_skills`, `_expire_skill`, `_find_active_skills`, `_find_revalidation_episodes`, `_maybe_repropose_candidate`, `_compute_early_review_flags`, `_run_expiry_pass`, `_background_expiry_pass`
- Selection: `_fetch_active_skills_for_select`, `_parse_preconditions`, `_specificity_score`, `_skill_success_rate`, `_apply_specificity_rule`, `_apply_recency_rule`, `_apply_success_rate_rule`, `_run_skill_selection`

The `_background_expiry_pass` trigger (called from `/audit` after every `EXPIRY_PASS_INTERVAL` events) must still fire — the policy router (slice 02) will need to import the trigger function from `routers/skills.py` (or, better, both can import a shared `_audit_event_counter` from `core/`).

## Acceptance criteria

- [ ] `services/governance/routers/skills.py` exists with an `APIRouter`
- [ ] All five endpoints respond at unchanged paths
- [ ] All helper functions listed above live in the same module
- [ ] Auto-trigger from `/audit` still fires after `EXPIRY_PASS_INTERVAL` events
- [ ] `make test-integration` passes — in particular `test_skill_expiry.py`, `test_skill_select.py`, `test_skills_cli.py`, `test_skill_execution.py`
- [ ] Expire endpoint still commits per-skill state transitions to Dolt
- [ ] Select endpoint still writes the rationale audit row via `BackgroundTasks`

## Blocked by

- [01 — Extract governance core infrastructure into a `core/` package](01-extract-governance-core-infra.md)
