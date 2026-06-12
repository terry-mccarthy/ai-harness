Status: done

# 07 — Correlation ID threading

## What to build

Thread a `correlation_id` (uuid) through all audit rows belonging to one logical multi-agent workflow so the full chain is reconstructable with a single query.

The correlation_id is generated at the entry point of a workflow (first `agent_invoke` or first `task_post` call) and passed along implicitly — callers should not need to manage it manually. Every audit row written during that chain (invocations, task transitions, denials) carries the same `correlation_id`. This makes `SELECT * FROM audit_log WHERE correlation_id = :id ORDER BY created_at` reconstruct the complete interaction, including any denied hops.

This requires: adding `correlation_id` to the audit_log schema (nullable, to preserve backwards compatibility with existing rows), propagating it through the governance service's request context, and writing it on every audit INSERT in the orchestration path.

## Acceptance criteria

- [ ] `audit_log` table has a `correlation_id` column (nullable VARCHAR/CHAR(36)); existing rows unaffected
- [ ] `test_correlation_id_threads_chain`: a multi-step workflow (at minimum: supervisor invokes reviewer, reviewer result returned) produces multiple audit rows all sharing the same `correlation_id`, in timestamp order
- [ ] A single-step tool call (non-orchestration path) still produces an audit row; `correlation_id` is null or absent — not an error
- [ ] `correlation_id` is included in the audit row for denied invocations (the attempt must be traceable within its chain)
- [ ] No existing integration tests broken by the schema addition

## Blocked by

- Issue 04 (agent_invoke — needs at least one multi-hop path to thread through)
- Issue 05 (task_complete — task lifecycle transitions should also carry the id)
