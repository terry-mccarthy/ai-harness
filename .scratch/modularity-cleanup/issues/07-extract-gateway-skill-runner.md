---
title: "Pull skill execution out of `GatewayClient` into a separate `SkillRunner`"
status: ready-for-agent
type: AFK
---

## What to build

`packages/harness-gateway/harness_gateway/client.py` is 357 lines and mixes four concerns:

1. MCPJungle tool invocation
2. ContextForge tool invocation
3. Governance sidecar policy check + audit
4. **Skill execution** — fetching a promoted skill, executing its steps with per-step governance re-check, applying `on_failure` policy (ABORT / CONTINUE / ROLLBACK), and running rollback steps

The first three are tightly coupled (every tool call routes through them). The fourth is a stateful workflow built *on top of* `call_tool` and has no reason to live in the same class.

Pull skill execution into a new file `packages/harness-gateway/harness_gateway/skill_runner.py` exposing a `SkillRunner` (or module-level functions) that takes a `GatewayClient` as a collaborator.

Move these out of `client.py`:

- `execute_skill`
- `_fetch_skill`
- `_parse_steps`
- `_execute_step`
- `_handle_step_failure`
- `_run_rollback`
- `_count_completed`

`GatewayClient` shrinks back to: token caching, `_resolve_name`, `_governance_check`, `_governance_audit`, `_invoke_mcpjungle`, `_invoke_cf`, `_invoke_direct`, `call_tool`. That's its real job.

Callers that today do `gateway.execute_skill(skill_id, inputs)` should now do `SkillRunner(gateway).execute(skill_id, inputs)` (or `run_skill(gateway, skill_id, inputs)` if the function-style API reads better — pick whichever needs fewer test edits).

## Acceptance criteria

- [ ] `packages/harness-gateway/harness_gateway/skill_runner.py` exists
- [ ] `client.py` no longer contains any of the seven listed functions
- [ ] `client.py` is under 280 lines
- [ ] All existing `execute_skill` call sites still work (compatibility shim on `GatewayClient` is acceptable if it just delegates to `SkillRunner`)
- [ ] `make test-integration` passes — in particular `test_skill_execution.py`, `test_skills_cli.py`
- [ ] Rollback steps still execute in order on `on_failure: ROLLBACK`
- [ ] CONTINUE / ABORT semantics on per-step denial unchanged

## Blocked by

None — can start immediately, fully independent of slices 01–06.
