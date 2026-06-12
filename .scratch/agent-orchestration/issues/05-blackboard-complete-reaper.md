Status: done

# 05 — Blackboard: task_complete + lease reaper

## What to build

Complete the task lifecycle with `task_complete` and a lease reaper that returns stale claimed tasks to the pool.

`task_complete` closes a task by transitioning it to `status=done` and recording the result. It must be idempotent: re-submitting the same `idempotency_key` must return the original result without writing a second row. The `uq_idem` unique key on the tasks table (from issue 01) is the enforcement mechanism — a duplicate key violation is caught and resolved by returning the stored result, not an error.

The lease reaper prevents a crashed worker from permanently holding a task. On each `task_claim` call (or as a periodic sweep), reset any `claimed` rows whose `lease_expires < now` back to `pending`. This requires no new infrastructure — an on-claim sweep is sufficient for v1.

Each transition (complete, reaper reset) produces a Dolt commit.

## Acceptance criteria

- [ ] `task_complete` transitions status to `done`, stores result, writes Dolt commit, returns `{status: "done"}`
- [ ] `test_task_complete_idempotent`: submitting the same `idempotency_key` twice returns the original result; Dolt log shows exactly one completion commit for that task
- [ ] `test_lease_expiry_returns_task_to_pool`: a task claimed with a short lease, left untouched past expiry, becomes `status=pending` again on next claim or sweep
- [ ] A worker that is not the claimer of a task cannot complete it
- [ ] Reaper does not affect tasks with future `lease_expires` or tasks in `done`/`failed` status

## Blocked by

- Issue 03 (task_post + task_claim must exist)
