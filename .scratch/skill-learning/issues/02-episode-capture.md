---
title: "Episode capture on governance audit path"
status: ready-for-agent
type: AFK
---

## What to build

When `POST /audit` is called on the governance service, write a row to the `episodes` table in Dolt alongside the existing `audit_log` write. The episode captures the tool call as an immutable, unlabeled record — `outcome` and `outcome_labeled_at` are NULL at this point. Labeling happens separately (issue 03).

Fields populated from the audit payload:
- `agent_principal` — from the JWT `sub` claim
- `alert_signature` — derived from the tool name + correlation ID (a normalized key, e.g. `sre.observability_query:<correlation_id>`)
- `service_class` — from the audit payload if present, else `"unknown"`
- `env_fingerprint` — JSON snapshot of `{tool_name, server_id, timestamp_ms}`
- `actions` — JSON array with one entry: `{tool: <tool_name>, scoped_args: <request_hash>, scope_token_ref: <correlation_id>}`

The episode write is fire-and-forget (same pattern as the existing audit write) — governance returns 202 without waiting on the Dolt commit. Episode capture must not raise a 5xx if Dolt is unavailable; log the error and continue.

## Acceptance criteria

- [ ] Calling `POST /audit` with a valid payload creates a row in `episodes` with `outcome = NULL`
- [ ] `agent_principal` matches the JWT `sub` from the audit request
- [ ] Episode write failure is logged but does not affect the 202 response
- [ ] Existing `audit_log` write is unaffected
- [ ] Integration tests covering the audit endpoint still pass

## Blocked by

- [01 — Dolt schema: episodes, candidates, skills tables](01-dolt-schema-episodes-candidates-skills.md)
