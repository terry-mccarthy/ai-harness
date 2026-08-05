Status: done

# 03 — Blackboard: task_post + task_claim (atomic)

## What to build

Add `task_post` and `task_claim` governance endpoints backed by the `tasks` Dolt table.

`task_post` writes a new row with `status=pending` and commits. `task_claim` must be atomic — two concurrent workers must never receive the same task. The correct implementation is a conditional UPDATE (set `status=claimed` WHERE `status=pending AND id=:id`) followed by checking the affected-row count. A result of 0 means another worker won the race; the caller retries from the SELECT. This is the critical correctness requirement in §6 of `AGENT-ORCHESTRATION-SPEC.md`.

Each successful post and claim produces a Dolt commit, making the task lifecycle reconstructable from `dolt_log`.

## Acceptance criteria

- [ ] `task_post` creates a row with `status=pending` and returns `{task_id, status: "pending"}`; a Dolt commit is recorded
- [ ] `task_claim` returns `{task_id, payload}` for the highest-priority pending task matching the caller's role; a Dolt commit is recorded
- [ ] `task_claim` returns `{task_id: null}` (no error) when no matching task is available
- [ ] `test_task_claim_atomic_no_double_grab`: N concurrent claimers against M tasks (N > M) — every task is claimed exactly once, no double-grabs
- [ ] `test_task_claim_returns_null_when_empty` passes
- [ ] `lease_expires` is set on the claimed row to `now + lease_seconds`
- [ ] A worker of the wrong role cannot claim a task requiring a different role

## Blocked by

- Issue 01 (Dolt tasks table must exist)
