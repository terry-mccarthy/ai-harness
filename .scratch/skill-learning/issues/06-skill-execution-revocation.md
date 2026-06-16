---
title: "Skill execution with per-step OPA re-check and revocation"
status: ready-for-agent
type: AFK
---

## What to build

Give `GatewayClient` the ability to execute a skill by ID. A skill is not a shortcut past authorization — each step is independently re-checked against OPA before the tool call is made, using the invoking principal's scoped token. Promotion grants no authority.

Execution flow:
1. Fetch the skill from governance `GET /skills/{id}`. Return `ToolAccessDenied` if `status != ACTIVE`.
2. For each step in `procedure.steps`:
   a. Call `POST /check` on governance with `{tool: step.tool, required_scope: step.required_scope, token: <invoking_token>}`.
   b. If denied, apply `step.on_failure`: `ABORT` (raise immediately), `ROLLBACK` (execute rollback steps then raise), `CONTINUE` (log and skip).
   c. If allowed, invoke the tool via the gateway.
   d. If `step.expected_signal` is defined, evaluate it against the tool response. Treat a signal mismatch as a step failure and apply `on_failure`.
3. After all steps, evaluate `procedure.success_criteria` against the collected responses. Return structured result.

Revocation: add `POST /skills/{id}/revoke` to governance. Requires `skill:promote` scope. Sets `status = REVOKED`, records `revoked_reason`, commits to Dolt. A revoked skill is immediately un-executable — the fetch in step 1 returns 410 Gone. Revocation takes effect on the next skill invocation; in-flight executions are not interrupted.

Expose skill execution via a new MCP tool `review_server__run_skill` (or equivalent) so agents can invoke skills without knowing the internals.

## Acceptance criteria

- [ ] Executing an ACTIVE skill with valid scoped tokens runs all steps and returns a structured result
- [ ] A step whose OPA check fails with `on_failure=ABORT` stops execution immediately
- [ ] A step whose OPA check fails with `on_failure=ROLLBACK` runs the rollback steps before raising
- [ ] Executing a REVOKED skill returns an error immediately (no tool calls made)
- [ ] `POST /skills/{id}/revoke` sets status REVOKED and commits to Dolt; subsequent execution attempts fail
- [ ] Agent tokens without `skill:promote` scope cannot revoke
- [ ] Integration test: execute a seeded skill end-to-end with stub MCP tools

## Blocked by

- [05 — HITL promotion gate](05-hitl-promotion-gate.md)
