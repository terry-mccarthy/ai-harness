Status: done

# 02 — OPA + agent_list: policy-filtered agent discovery

## What to build

Extend `policies/harness.rego` with an `invoke_allowed` rule and an `allowed_targets` topology map, then expose a governance endpoint that returns the agents a caller is permitted to invoke.

The topology for v1 (from §7 of `AGENT-ORCHESTRATION-SPEC.md`):
- `supervisor` may invoke: `code-reviewer`, `architect`, `sre`
- `architect` may invoke: `code-reviewer`
- `code-reviewer`, `sre` may invoke: nobody

The `agent_list` endpoint (on the governance service) should: fetch raw tool registrations from MCPJungle (`tools/list`), filter each candidate through OPA using `{caller_role, action: "invoke", target}`, drop any agent failing a health check, and return the filtered list. A caller never sees agents it may not invoke — discovery itself is governed.

## Acceptance criteria

- [ ] `harness.rego` defines `invoke_allowed` and `claim_allowed` rules matching §7
- [ ] `GET /agents` (or `POST /agents` — pick one, stay consistent) on governance returns only agents OPA permits the calling role to invoke
- [ ] A `code-reviewer` JWT calling `agent_list` receives an empty or reviewer-only list (not sre, not architect)
- [ ] A `supervisor` JWT receives all three agents (assuming all healthy)
- [ ] `test_agent_list_filters_by_policy` passes
- [ ] OPA policy change is covered by at least one unit-level rego test or direct OPA query in the test suite

## Blocked by

None — can start immediately (parallel with issue 01).
