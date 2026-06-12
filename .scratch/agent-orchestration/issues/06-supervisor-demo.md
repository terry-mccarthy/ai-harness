Status: done

# 06 — Supervisor demo: chained reviewer→architect workflow

## What to build

A thin Python coordinator that chains `code-reviewer → architect` over structured artifacts using `agent_invoke`. This is the first demonstration that multi-agent orchestration works end-to-end with zero new infrastructure beyond what issues 02–04 delivered.

The coordinator (a new module or script in `packages/harness-agents`) should: receive a diff as input, invoke `code-reviewer` via `agent_invoke` to get structured findings, pass those findings as the artifact payload to invoke `architect` (e.g. to generate a remediation ADR or design note), and return both outputs. Each agent runs under its own credentials. No shared context window.

The demo proves the typed-artifact contract: `code-reviewer`'s output schema is the input to the `architect` call. If schemas are incompatible the coordinator fails loudly rather than silently passing garbage.

## Acceptance criteria

- [ ] A coordinator script or integration test chains reviewer → architect via `agent_invoke` and returns both structured outputs
- [ ] Each agent call is visible in the governance audit log under the correct `agent_role`
- [ ] Reviewer output is validated against its schema before being passed as architect input; schema mismatch raises a clear error
- [ ] The coordinator does not forward the reviewer's token to the architect call — each agent is invoked with its own credentials
- [ ] The demo runs against the live Docker stack with `make test-integration` (or a clearly documented `pytest -m` invocation)

## Blocked by

- Issue 04 (agent_invoke must be in place)
