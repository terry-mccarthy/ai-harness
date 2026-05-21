# AI Harness — Architecture

## Overview

A governed code-review agent. A git diff goes in; structured findings and a pass/fail verdict come out. Every tool call is routed through a central proxy (MCPJungle) so tool access can be audited and controlled.

Claude Code (or any MCP client) can call `review_diff` directly — the proxy handles routing to the agent, which routes its own tool calls back through the same proxy.

```
Claude Code (MCP client)
  │  /mcp  (streamable-HTTP)
  ▼
MCPJungle  :8080  ──── postgres :5432  (state)
  │         │     └─── opa :8181       (policy — wired, not enforced in free tier)
  │         │
  │    review_server__review_diff
  ▼
review-server  :9003  (FastMCP)
  │  CodeReviewerAgent
  │  calls tools via GatewayClient
  ▼
MCPJungle  :8080  (same instance)
  ├── git_diff_stub__git_diff    → git-diff-stub  :9001
  └── linter_stub__run_linter    → linter-stub    :9002
```

## Services (docker-compose)

| Service | Image | Port | Role |
|---|---|---|---|
| `postgres` | postgres:16 | 5432 | MCPJungle state store |
| `opa` | openpolicyagent/opa:latest | 8181 | Policy engine (wired, not yet enforced) |
| `mcpjungle` | mcpjungle/mcpjungle:latest | 8080 | MCP proxy / tool registry / MCP server |
| `git-diff-stub` | local build | 9001 | Real `git diff` MCP server (baked sample repo) |
| `linter-stub` | local build | 9002 | Fake `run_linter` MCP server |
| `review-server` | local build | 9003 | `review_diff` MCP tool — runs full agent |
| `register-git-diff` | mcpjungle image | — | One-shot: registers git_diff_stub |
| `register-linter` | mcpjungle image | — | One-shot: registers linter_stub |
| `register-review-server` | mcpjungle image | — | One-shot: registers review_server |

## Python packages (monorepo)

```
packages/
  harness-gateway/   — GatewayClient: HTTP calls to MCPJungle
  harness-agents/    — CodeReviewerAgent + AgentState TypedDict + output schema
  harness-tests/     — pytest integration tests (9 tests across 3 files)

services/
  review_server/     — FastMCP server wrapping CodeReviewerAgent
```

Dependencies: `harness-tests` → `harness-agents` → `harness-gateway`.

`review_server` installs `harness-gateway` and `harness-agents` at image build time (copied from `packages/` into the Docker context).

## Request flows

### Direct agent call (Python)

1. Caller builds `AgentState` (task + diff + thread_id), calls `CodeReviewerAgent.run()`.
2. Agent calls `GatewayClient.call_tool("git_diff", {"diff_text": ...})` and `call_tool("run_linter", ...)`.
3. `GatewayClient` maps short name → `serverName__toolName`, POSTs flat JSON to `/api/v0/tools/invoke`.
4. MCPJungle routes to the MCP server via streamable-HTTP, returns `{"content": [{"type": "text", "text": "<json>"}]}`.
5. `GatewayClient` unwraps `content[0].text` and JSON-parses it.
6. Agent builds a prompt from both results, calls Ollama (`qwen2.5-coder:7b`).
7. Model returns raw JSON. Agent validates against `REVIEWER_OUTPUT_SCHEMA`, retries up to 3× on failure.
8. Returns populated `AgentState` with `agent_output`.

### Via MCP (Claude Code or any MCP client)

1. Client connects to `http://localhost:8080/mcp` (MCPJungle's own MCP endpoint).
2. Client calls `review_server__review_diff` with `diff_text` (and optional `task`).
3. MCPJungle routes to `review-server:9003/mcp`.
4. `review-server` instantiates `CodeReviewerAgent` and runs the direct agent flow above.
5. Returns structured findings JSON back through MCPJungle to the client.

## Tool access control

Enforced in `GatewayClient.TOOL_NAME_MAP`:

```python
TOOL_NAME_MAP = {
    "git_diff":    "git_diff_stub__git_diff",
    "run_linter":  "linter_stub__run_linter",
    "review_diff": "review_server__review_diff",
}
```

Any tool not in the map raises `ToolAccessDenied` before the network call. This is the current substitute for OAuth + OPA enforcement (Enterprise-tier MCPJungle feature).

OPA is running with `policies/harness.rego` loaded, but MCPJungle free tier does not call it.

## git_diff tool

The `git-diff-stub` container runs real `git` commands. The Dockerfile bakes in a sample repo at `/app/sample-repo` with two commits — the second adds a password-logging `print` statement. This lets the full review pipeline run against a real diff without needing an external repo.

The tool accepts:
- `diff_text` (string) — if provided, echoes it back (passthrough mode, used by `CodeReviewerAgent`)
- `repo_path` (string, default `/app/sample-repo`) + `base`/`head` refs — runs real `git diff`

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

`verdict` is `"fail"` if any finding is `CRITICAL`. Validated by jsonschema on every LLM response.

## Test coverage (9 tests)

| File | Tests | What they cover |
|---|---|---|
| `test_thin_slice.py` | 3 | Core agent contract, gateway audit log, tool access denial |
| `test_review_mcp.py` | 3 | `review_diff` MCP tool reachable, schema valid, catches credential leak |
| `test_real_git_diff.py` | 3 | Real git diff format, contains commit changes, respects ref params |

## What is deferred (from spec)

- **OAuth 2.1 gateway auth** — requires MCPJungle Enterprise
- **PostgreSQL checkpointer** — LangGraph state persistence
- **Supervisor / routing graph** — multi-agent orchestration
- **Dolt audit log** — structured tool call history
- **Memory store + ConsolidationWorker**
- **Architect and SRE agents**
- **Human-in-the-loop gate**

## Known gaps / future work

- MCP registration is ephemeral — init containers re-register on every `docker compose up`.
- OPA policy is loaded but not enforced (free tier). Real enforcement needs Enterprise or a custom gateway.
- `git_diff` passthrough mode means `CodeReviewerAgent` never exercises the real `git diff` path — the agent sends `diff_text` directly, which echoes back unchanged.
- `qwen2.5-coder:7b` occasionally produces `findings: []` with `verdict: fail` on ambiguous diffs. The retry loop handles schema errors but not semantic gaps.
