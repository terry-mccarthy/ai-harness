---
title: "Extract agents router (`/agent/invoke`, `/agents`, registry)"
status: ready-for-agent
type: AFK
---

## What to build

Pull the **agent orchestration endpoints** out of `services/governance/server.py` into `services/governance/routers/agents.py`.

Endpoints in scope:

- `POST /agent/invoke` — synchronous governed handoff: payload validation → OPA invoke check → mint target creds → call MCPJungle entry tool → audit
- `GET /agents` — list agents the calling role is permitted to invoke

These move together along with their supporting data and helpers:

- `_AGENT_REGISTRY` (the known agents: `code-reviewer`, `architect`, `sre` with their `client_id`, `secret_env`, `role`, `entry_tool`, `input_schema`)
- `_KNOWN_AGENTS`
- `MCPJUNGLE_URL` env var (or move to `core/config.py`)
- `_call_mcpjungle` — JSON-RPC wrapper around MCPJungle's flat invoke API
- `_validate_payload` — required-field check against the registry schema

The OPA invoke check (`_check_opa_invoke`) is called through the unified `check_opa("harness/invoke_allowed", ...)` from `core/opa.py` after slice 01.

Token-minting still uses `core/auth.py` and `_private_key` from `core/config.py`.

## Acceptance criteria

- [ ] `services/governance/routers/agents.py` exists with an `APIRouter`
- [ ] Both endpoints respond at unchanged paths
- [ ] `_AGENT_REGISTRY` lives in this module (single source of truth for agent discovery)
- [ ] `server.py` no longer defines `agent/invoke`, `agents`, `_AGENT_REGISTRY`, `_call_mcpjungle`, or `_validate_payload`
- [ ] `make test-integration` passes — in particular `test_phase6_agent_invoke.py`, `test_phase6_opa_agent_list.py`, `test_phase6_supervisor_demo.py`
- [ ] Invoke check still writes deny audits synchronously and allow audits via `BackgroundTasks`

## Blocked by

- [01 — Extract governance core infrastructure into a `core/` package](01-extract-governance-core-infra.md)
