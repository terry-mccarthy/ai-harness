# AI Harness — Architecture

## Overview

A governed code-review agent. A git diff goes in; structured findings and a pass/fail verdict come out. Every tool call is authenticated, OPA-policy-checked, and committed to a tamper-evident Dolt audit log before it reaches a tool server.

Claude Code (or any MCP client) can call `review_diff` directly via MCPJungle. The agent's internal tool calls route through the governance service, so the full review is auditable end-to-end.

## Request flow

```
Claude Code (MCP client)
  │  :8080/mcp  (streamable-HTTP)
  ▼
MCPJungle  :8080
  │
  │  review_server__review_diff
  ▼
review-server  :9003  (FastMCP)
  │  CodeReviewerAgent
  │  GatewayClient  ← fetches JWT from governance
  │
  │  POST /api/v0/tools/invoke  (Bearer <JWT>)
  ▼
governance  :8090  (FastAPI)
  ├── validate JWT  (HS256, 15-min TTL)
  ├── POST /v1/data/harness/allow  →  OPA  :8181
  ├── INSERT audit_log + CALL DOLT_COMMIT  →  Dolt  :3306
  │
  │  POST /api/v0/tools/invoke  (forwarded)
  ▼
MCPJungle  :8080
  ├── git_diff_stub__git_diff    →  git-diff-stub  :9001
  └── linter_stub__run_linter    →  linter-stub    :9002
```

Every tool call the agent makes produces:
1. An OPA policy decision (`allow` or `deny`)
2. A row in `audit_log` in Dolt
3. A Dolt git commit — queryable with `dolt log` and `dolt diff`

## Services

| Service | Image | Port | Role |
|---|---|---|---|
| `postgres` | postgres:16 | 5432 | MCPJungle state store |
| `opa` | openpolicyagent/opa:latest | 8181 | Policy engine — evaluates `policies/harness.rego` |
| `mcpjungle` | mcpjungle/mcpjungle:latest | 8080 | MCP proxy / tool registry / MCP server for Claude Code |
| `dolt` | local build | 3306 | Git-versioned audit log database |
| `governance` | local build | 8090 | OAuth token issuance, OPA enforcement, Dolt audit |
| `git-diff-stub` | local build | 9001 | Real `git diff` MCP server (baked sample repo) |
| `linter-stub` | local build | 9002 | Pattern-matching `run_linter` MCP server |
| `architect-stub` | local build | 9004 | Stub MCP server for architect-role tools |
| `sre-stub` | local build | 9005 | Stub MCP server for SRE-role tools |
| `review-server` | local build | 9003 | `review_diff` MCP tool — runs full code-reviewer agent |
| `register-*` | mcpjungle image | — | One-shot init containers that register MCP servers |

## Python packages (monorepo)

```
packages/
  harness-gateway/   — GatewayClient: JWT auth + HTTP calls to governance
  harness-agents/    — CodeReviewerAgent + AgentState TypedDict + output schema
  harness-tests/     — pytest integration tests (26 tests across 4 files)

services/
  governance/        — OAuth 2.1 token issuance + OPA enforcement + Dolt audit
  dolt/              — Dolt init script and Dockerfile
  review_server/     — FastMCP server wrapping CodeReviewerAgent
```

Dependencies: `harness-tests` → `harness-agents` → `harness-gateway`.

## Governance service

`services/governance/server.py` — three responsibilities per request:

1. **Auth**: validates `Authorization: Bearer <JWT>`, rejects with 401 on missing/expired/invalid tokens
2. **Policy**: calls `POST /v1/data/harness/allow` on OPA with `{agent_role, tool_name}`; returns 403 if denied
3. **Audit**: inserts a row into Dolt `audit_log`, then calls `DOLT_COMMIT` — every tool call is a git commit

Token issuance: `POST /oauth/token` with client credentials (grant type `client_credentials`). Three clients: `architect`, `code-reviewer`, `sre`. Tokens are HS256 JWTs signed with `JWT_SECRET`, 15-min TTL.

## OPA policy

`policies/harness.rego` maps agent roles to allowed tool names:

| Role | Allowed tools |
|---|---|
| `architect` | `codebase_search`, `adr_read`, `adr_write`, `diagram_gen` |
| `code_reviewer` | `git_diff`, `run_linter`, `coverage_report`, `repo_conventions_read`, `review_diff` |
| `sre` | `observability_query`, `runbook_read`, `log_search`, `shell_exec` |

Default: deny. Cross-role calls (e.g. architect calling `shell_exec`) return 403 without reaching the tool server.

## Dolt audit log

Every tool call — allowed or denied — writes a row to `audit_log`:

| Column | Description |
|---|---|
| `agent_id` | OAuth `sub` claim (client_id) |
| `tool_name` | Full MCPJungle tool name (`server__tool`) |
| `server_id` | Short tool name |
| `request_hash` | SHA-256 of request body (first 16 hex chars) |
| `response_hash` | SHA-256 of response body (first 16 hex chars) |
| `policy_decision` | `allow` or `deny` |
| `policy_rule` | OPA rule that matched |
| `timestamp_ms` | Unix milliseconds |
| `latency_ms` | Round-trip to MCPJungle |

After every INSERT, governance calls `CALL DOLT_COMMIT('-Am', 'audit: <tool> by <agent> [allow/deny]')`. The full call history is queryable as a git log:

```sql
SELECT message FROM dolt_log LIMIT 20;
SELECT * FROM dolt_diff_audit_log;   -- row-level diff per commit
```

The `harness` DB user has INSERT + SELECT only — no DELETE. The audit log is append-only by construction.

## GatewayClient

`packages/harness-gateway/harness_gateway/client.py`:

- `_get_token()` posts to `{gateway_url}/oauth/token`, caches the JWT until 30s before expiry
- Falls back gracefully (returns `None`) if the gateway returns 404 on `/oauth/token`
- `call_tool(name, params)` maps short names → `server__tool` via `TOOL_NAME_MAP`, adds `Authorization: Bearer` header, POSTs to `/api/v0/tools/invoke`
- 401 and 403 responses raise `ToolAccessDenied`
- Response unwrapping: MCPJungle returns `{"content": [{"type": "text", "text": "<json>"}]}`; the client unwraps to a plain dict

## CodeReviewerAgent

`packages/harness-agents/harness_agents/reviewer.py`:

- Calls `git_diff` and `run_linter` via the gateway
- Builds a prompt from both results, calls Ollama (`qwen2.5-coder:7b`)
- Validates the model response against `REVIEWER_OUTPUT_SCHEMA` (jsonschema)
- Retries up to 3× on schema failure, feeding the error back to the model
- Strips markdown fences if the model ignores the raw JSON instruction

## Output schema

```json
{
  "verdict": "pass" | "fail",
  "findings": [
    {
      "severity": "CRITICAL" | "WARNING" | "INFO",
      "file": "string",
      "line": 0,
      "message": "string",
      "suggestion": "string"
    }
  ],
  "summary": "string"
}
```

`verdict` is `"fail"` if any finding is `CRITICAL`.

## git_diff tool

The `git-diff-stub` container bakes in a sample repo at `/app/sample-repo` with two commits — the second adds a password-logging `print` statement. This lets the full review pipeline run against a real diff without an external repo.

The tool accepts:
- `diff_text` (string) — passthrough mode; echoed back unchanged (used by `CodeReviewerAgent`)
- `repo_path` + `base`/`head` refs — runs real `git diff` against the baked repo

## Test coverage (26 tests)

| File | Tests | What they cover |
|---|---|---|
| `test_thin_slice.py` | 3 | Core agent contract, gateway audit log, tool access denial |
| `test_review_mcp.py` | 3 | `review_diff` MCP tool reachable, schema valid, catches credential leak |
| `test_real_git_diff.py` | 3 | Real git diff format, contains commit changes, respects ref params |
| `test_phase1_governance.py` | 17 | Auth, OPA policy enforcement, Dolt audit, token expiry |
