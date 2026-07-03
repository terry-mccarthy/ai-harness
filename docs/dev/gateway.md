# Gateway — MCPJungle, ContextForge, GatewayClient

## GatewayClient — split gateway + governance

`GatewayClient` in `packages/harness-gateway/harness_gateway/client.py`:

- `gateway_url` — direct tool invocation (MCPJungle `:8080` or CF `:4444`)
- `governance_url` — policy check + audit sidecar (governance `:8090`); optional
- When `governance_url` is set: calls `POST /check` before invoking, fires `POST /audit` after (async background task)
- When `governance_url` is None: legacy proxy mode (gateway_url is the governance proxy)
- `gateway_backend="contextforge"` enables CF's JSON-RPC call format + CF JWT auth
- 401/403 responses raise `ToolAccessDenied`

## GatewayClient response unwrapping

MCPJungle returns `{"content": [{"type": "text", "text": "<json string>"}]}`. The client unwraps this automatically — callers get plain parsed dicts back. The key is `"content"`, not `"result"`.

## What MCPJungle actually does (free tier)

MCPJungle v0.4.5 is a **CLI-managed MCP proxy** — not the auth layer. Governance sits in front of it.

- Tool invocation: `POST /api/v0/tools/invoke` with a **flat** JSON body: `{"name": "server__tool", "key": "value", ...}`. Do NOT nest params inside an `"input"` or `"arguments"` key.
- Tool names use double-underscore separator: `serverName__toolName`.
- Exposes itself as an MCP server at `:8080/mcp`.
- Servers are registered via CLI or init containers — registration is ephemeral, lost on MCPJungle restart.
- The image is distroless — no shell, no wget, no curl. Healthchecks must use the `/mcpjungle` binary itself.

## MCPJungle flat API — `name` parameter conflict

MCPJungle's invoke body is flat: `{"name": "<server__tool>", ...params}`. If a tool has a parameter also named `name`, the params dict will **silently overwrite the tool identifier**:

```python
# BAD — {"name": "sre_stub__runbook_read", "name": "incident-response"}
#       Python merges to {"name": "incident-response"} — wrong tool!
invoke_tool(token, "sre_stub__runbook_read", {"name": "incident-response"})

# GOOD — parameter renamed to runbook_name
invoke_tool(token, "sre_stub__runbook_read", {"runbook_name": "incident-response"})
```

**Never name an MCP tool parameter `name`.**

## git_diff tool modes

Three input modes, evaluated in priority order:

1. `diff_text` (non-empty string) — passthrough; echoed back immediately. Used by agents that already have the diff.
2. `pr_number` + `github_repo` — fetches the unified diff from `GET https://api.github.com/repos/{github_repo}/pulls/{pr_number}` with `Accept: application/vnd.github.v3.diff`. Token read from `GITHUB_TOKEN` env var (optional; omit for public repos). Works from inside Docker — no filesystem access needed.
3. `repo_path` + `base`/`head` — runs `git diff` against the Docker-internal baked sample repo at `/app/sample-repo`.

## Connecting Claude Code to the harness

MCPJungle exposes itself as an MCP server at `http://localhost:8080/mcp`. Register it with a static `Authorization` header to bypass OAuth discovery (see gotcha below):

```bash
claude mcp add -s user --transport http "ai-harness" "http://localhost:8080/mcp" \
  -H "Authorization: Bearer no-auth"
```

This gives Claude Code access to all registered tools, including `review_server__review_diff`.

**OAuth discovery gotcha (Claude Code ≥ v2.1.181):** Claude Code now performs OAuth 2.0 discovery (`GET /.well-known/oauth-authorization-server`) before connecting to any HTTP MCP server. MCPJungle returns `404 page not found` as plain text, which the SDK can't parse as JSON — dropping the connection with "SDK auth failed: HTTP 404". The `-H "Authorization: Bearer no-auth"` flag makes the SDK treat the server as pre-authenticated and skip discovery entirely. MCPJungle ignores the header value.

## Claude Code MCP tool timeout

Claude Code's MCP client has a **hard ~60-second timeout** derived from the MCP TypeScript SDK default (`DEFAULT_REQUEST_TIMEOUT_MSEC = 60000`). Long-running tools like `bootstrap_architecture` (5 LLM calls + 5 tool round-trips) routinely exceed this.

**Workaround — launch Claude Code with an extended timeout:**

```bash
MCP_TOOL_TIMEOUT=300000 claude   # 5 minutes
```

`MCP_TOOL_TIMEOUT` is honoured by Claude Code CLI. The per-server `timeout` field in `.mcp.json` was working in ≤ v2.1.107 but is silently ignored for HTTP transport since v2.1.113 (open regression: [anthropics/claude-code#50289](https://github.com/anthropics/claude-code/issues/50289)).

**Why ContextForge doesn't fix this:** ContextForge's `TOOL_TIMEOUT` (default 60s, configurable) controls how long ContextForge waits for the upstream server — but Claude Code's client-side timeout fires first regardless. Both would need to be extended, and only `MCP_TOOL_TIMEOUT` is actually configurable today.

## ContextForge gateway (Phase 5)

ContextForge (`ghcr.io/ibm/mcp-context-forge:latest`) runs on port 4444 as an alternative to MCPJungle.

**Setup flow:**
1. `docker compose up -d contextforge` — waits for health check
2. `docker compose up setup-contextforge` — registers all stubs + creates `harness_all` virtual server
3. Set `GATEWAY_BACKEND=contextforge` in governance to route through ContextForge

**Key gotchas:**

- **Transport must be `STREAMABLEHTTP` (uppercase)** when registering a gateway. `streamablehttp` (lowercase) returns 422. FastMCP stubs use POST-based streamable HTTP; ContextForge sends `GET /mcp` by default (SSE) which returns 400 from FastMCP.
- **SSRF protection blocks Docker internal hostnames** by default. Must set `SSRF_ALLOW_PRIVATE_NETWORKS=true` and `SSRF_ALLOW_LOCALHOST=true` in the ContextForge container env.
- **JWT format for ContextForge API calls** requires: `sub`, `preferred_username`, `iss=mcpgateway`, `aud=mcpgateway-api`, `jti` (UUID), `exp`. Signed with `CF_JWT_SECRET`. `create_jwt_token` in ContextForge is async.
- **Tool name mapping**: `architect_stub__codebase_search` → `architect-stub-codebase-search` (replace `__` and `_` with `-`).
- **Virtual server UUID**: discovered at runtime by `ContextForgeGatewayClient` via `GET /servers` matching by name. `toolCount=0` on gateway registration is normal — tools are discovered asynchronously.
