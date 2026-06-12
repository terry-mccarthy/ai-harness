Status: done

# 01 — Dolt: tasks table migration

## What to build

Add the `tasks` and `agent_messages` tables to the Dolt init script so the blackboard and message-passing surfaces have a schema to write to. Both tables are append-only by design — task lifecycle is managed via status transitions, never DELETEs. The existing no-DELETE grant on the harness user must be preserved.

The `tasks` table needs an index on `(status, required_role, priority)` to make the atomic claim query fast, and a unique key on `idempotency_key` to support duplicate-safe completion. The `agent_messages` table needs an index on `(to_role, created_at)` for inbox reads.

After the migration, rebuild the Dolt container and confirm both tables exist with correct schema and grants.

## Acceptance criteria

- [ ] `services/dolt/init.sh` creates `tasks` and `agent_messages` tables matching the schema in §5 of `AGENT-ORCHESTRATION-SPEC.md`
- [ ] `idx_claimable`, `uq_idem`, and `idx_inbox` indexes exist
- [ ] The harness DB user retains no-DELETE access and can SELECT/INSERT/UPDATE on both new tables
- [ ] `docker compose build dolt && docker compose up -d --no-deps dolt` succeeds and `SHOW TABLES` inside the container lists both tables
- [ ] Existing integration tests pass unchanged after the migration

## Blocked by

None — can start immediately.
