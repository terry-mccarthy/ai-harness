---
title: "Extract governance core infrastructure into a `core/` package"
status: ready-for-agent
type: AFK
---

## What to build

`services/governance/server.py` is 1,546 lines and concentrates 23 endpoints, five copy-paste OPA helpers, JWT + Dolt + Prometheus glue, and the `CLIENTS` registry into one module. Every change requires reading the whole file, which is slowing iteration noticeably.

Carve the **shared infrastructure helpers** (used by every router) out of `server.py` into a sibling `core/` package under `services/governance/`. The endpoints themselves stay in `server.py` for this slice — only the helpers move. Subsequent slices will extract the routers and import from `core/`.

In scope for this slice:

- `core/config.py` — env vars (`OPA_URL`, `DOLT_*`, `TOKEN_TTL`, `EXPIRY_PASS_INTERVAL`), the `CLIENTS` dict, the test-key tripwire, RSA key loading, `_private_key`/`_public_key`/`_b64url`.
- `core/auth.py` — `_decode_jwt` and any JWT-encode helper used by multiple endpoints. (The `/oauth/token` and `/jwks` endpoints themselves move with the policy router in slice 02 — leave them in `server.py` for now.)
- `core/opa.py` — **collapse the five OPA check helpers** (`_check_opa`, `_check_opa_label`, `_check_opa_propose`, `_check_opa_promote`, `_check_opa_invoke`) into one parameterised function, e.g. `check_opa(rule_path: str, input_dict: dict) -> bool | list`. Caller passes the data path and the input payload; one function handles error/timeout/log uniformly.
- `core/dolt.py` — `get_dolt_conn`, `_write_audit`, `_write_episode`, `_serialise_row`.
- `core/metrics.py` — Prometheus `Counter`/`Histogram` definitions. (The `/metrics` endpoint moves with the policy router in slice 02 — leave it in `server.py` for now.)

`server.py` should import these helpers from `core/` and otherwise stay structurally identical — same endpoints, same behaviour. Goal of this slice: zero behavioural change, all tests pass, but the helper layer is now extracted and de-duplicated.

## Acceptance criteria

- [ ] `services/governance/core/` exists with `config.py`, `auth.py`, `opa.py`, `dolt.py`, `metrics.py`
- [ ] All five `_check_opa_*` call sites in `server.py` now call a single shared function in `core/opa.py`
- [ ] `services/governance/server.py` is smaller (target: < 1,100 lines) and imports from `core/`
- [ ] `make test-integration` passes with zero changes to test files
- [ ] Governance container builds and `docker compose up governance` starts cleanly
- [ ] `/check`, `/audit`, `/oauth/token`, `/jwks`, `/metrics` all respond identically to before (smoke check with `curl` or the existing integration tests)
- [ ] Test-key fingerprint tripwire still fires when `ENV != "test"`

## Blocked by

None — can start immediately.
