---
title: "Extract tasks router (`/tasks*`, `/memory/write`)"
status: ready-for-agent
type: AFK
---

## What to build

Pull the **blackboard task queue and memory-proxy endpoints** out of `services/governance/server.py` into `services/governance/routers/tasks.py`.

Endpoints in scope:

- `POST /tasks` — post a pending task to the blackboard for a given role/artifact_type
- `POST /tasks/claim` — atomically claim the highest-priority pending task for the caller's role (with stale-lease reaping)
- `POST /tasks/complete` — idempotently close a claimed task with a result
- `POST /memory/write` — auth-gated memory write proxy (currently just logs)

These move together along with the helper `_resolve_idempotent_complete`.

Stale-lease reaping (the `UPDATE tasks SET status='pending' WHERE lease_expires < NOW()` sweep on every claim) stays in `/tasks/claim`.

After this slice lands, `services/governance/server.py` should consist of: imports, app construction, router mounts, and (optionally) a small startup hook. Everything domain-specific lives under `core/` or `routers/`.

## Acceptance criteria

- [ ] `services/governance/routers/tasks.py` exists with an `APIRouter`
- [ ] All four endpoints respond at unchanged paths
- [ ] `server.py` is now ≤ 100 lines and contains only wiring (app, router mounts, startup tripwire)
- [ ] `make test-integration` passes — in particular `test_phase6_blackboard_post_claim.py`, `test_phase6_blackboard_complete_reaper.py`
- [ ] Atomic claim semantics preserved (the select-then-update retry loop)
- [ ] Idempotency on `/tasks/complete` still returns the cached result on duplicate `idempotency_key`

## Blocked by

- [01 — Extract governance core infrastructure into a `core/` package](01-extract-governance-core-infra.md)
