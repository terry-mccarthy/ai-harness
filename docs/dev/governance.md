# Governance service (`:8090`)

FastAPI app at `services/governance/server.py`. Three responsibilities:

1. **OAuth 2.1 client credentials** — `POST /oauth/token` (form body). Three clients: `architect`, `code-reviewer`, `sre`. Issues **RS256 JWTs** with 15-min TTL, signed with a private RSA key loaded from `JWT_PRIVATE_KEY_FILE`.
2. **OPA policy check** — `POST /check` validates a token and calls OPA. Returns 200 `{"allowed": true, ...}` or 403.
3. **Dolt audit** — `POST /audit` accepts an audit record and writes to Dolt asynchronously (202 response). `CALL DOLT_COMMIT` per write.
4. **JWKS** — `GET /jwks` returns the RSA public key as a JWK set; downstream verifiers fetch from here.

Rate limiting is delegated to the gateway (ContextForge natively). Governance does not rate-limit.

Key env vars: `JWT_PRIVATE_KEY_FILE` (path to PEM private key), `OPA_URL`, `DOLT_HOST/PORT/USER/PASSWORD`.

**Test key tripwire:** `test-fixtures/jwt-test-key.pem` is committed for local dev. Governance refuses to start with this key unless `ENV=test` is set — fingerprint-checked at startup. Never set `ENV=test` in a production deployment.

## OPA policy syntax

OPA `latest` requires the `if` keyword:

```rego
allow if {          # correct
    ...
}

allow {             # broken — rego_parse_error on modern OPA
    ...
}
```

Current policy (`policies/harness.rego`) maps three roles to tool sets:
- `architect` → `codebase_search`, `adr_read`, `architecture_review`, `execute_architecture_check`
- `code_reviewer` → `git_diff`, `run_linter`, `coverage_report`, `repo_conventions_read`, `review_diff`
- `sre` → `observability_query`, `runbook_read`, `log_search`, `shell_exec`, `skill_search`

## Rate limiting (Phase 6+)

Rate limiting is now delegated to the gateway (ContextForge natively). Governance no longer rate-limits — the old Redis sliding-window counter was removed. `test_governance_no_rate_limit` verifies governance returns no 429s regardless of call volume.

## Human approval token scoping

The `human_approval_token` is a short-lived JWT (10-min TTL) scoped to a specific `thread_id` and `tool_name` (e.g., `shell_exec`). The token is passed as the `X-Human-Approval-Token` header to governance, which validates the signature and scope before allowing the tool call. A token issued for thread A cannot be reused for thread B, and a token for `shell_exec` cannot be used for other tools.
