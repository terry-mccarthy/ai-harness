---
title: "Extract policy router (`/check`, `/audit`, `/oauth/token`, `/jwks`, `/metrics`)"
status: ready-for-agent
type: AFK
---

## What to build

Pull the **policy and identity endpoints** out of `services/governance/server.py` into a new `services/governance/routers/policy.py` using FastAPI's `APIRouter`.

Endpoints in scope:

- `POST /oauth/token` — issue RS256 JWT (client_credentials grant)
- `POST /check` — token + OPA policy decision (including the `shell_exec` human-approval-token guard)
- `POST /audit` — async Dolt audit write (returns 202)
- `GET /jwks` — public key as a JWK set
- `GET /metrics` — Prometheus exposition

These five endpoints form the **policy + identity** surface that every other service in the stack talks to. They share `core/auth.py` helpers and `core/opa.py` helpers from issue 01, so they cluster naturally.

`services/governance/server.py` should:

- Construct the `FastAPI()` app
- Mount the policy router via `app.include_router(policy.router)`
- Keep the other (yet-to-be-extracted) endpoints inline for now — those move in slices 03–06

## Acceptance criteria

- [ ] `services/governance/routers/policy.py` exists with an `APIRouter` exporting the five endpoints
- [ ] `server.py` mounts the router and no longer defines `oauth/token`, `check`, `audit`, `jwks`, or `metrics` inline
- [ ] All routes still respond at the same paths (no prefix change)
- [ ] `make test-integration` passes — in particular `test_phase1_governance.py`, `test_token_usage.py`, `test_phase6_correlation_id.py`
- [ ] OpenAPI schema at `/openapi.json` still lists all five paths
- [ ] Audit writes still commit to Dolt (`harness_tool_calls_total` metric increments)

## Blocked by

- [01 — Extract governance core infrastructure into a `core/` package](01-extract-governance-core-infra.md)
