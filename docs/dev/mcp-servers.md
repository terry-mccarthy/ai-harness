# MCP Servers

## MCP server config (FastMCP)

All MCP servers use **FastMCP with streamable-HTTP transport** (`mcp[cli]` package, Python 3.12 inside Docker).

Critical config — without this, MCPJungle gets **421 Misdirected Request** when registering:

```python
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "server_name",
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
```

FastMCP defaults to DNS rebinding protection on `127.0.0.1`. Since Docker containers call each other by hostname (e.g. `diff-proxy:9001`), the Host header validation fires and returns 421 unless disabled.

## Adding a new tool

1. Add a `@mcp.tool()` function to an existing server, or create a new server under `services/`.
2. Rebuild: `docker compose build <service>`.
3. Re-register: `docker compose up register-<service>` (or add a new init container to compose).
4. Add the short-name → `server__tool` mapping to `TOOL_NAME_MAP` in `client.py`.
5. If the `CodeReviewerAgent` should call it, add the name to `allowed_tools`.
6. Add the tool to the appropriate role in `policies/harness.rego`; restart OPA or send a `PUT /v1/policies/harness` request.
7. If the OPA policy already lists the tool but it has no server or `TOOL_NAME_MAP` entry (e.g. `coverage_report`, `repo_conventions_read` — which were documented but unreachable), implement it rather than removing the OPA rule. A tool in OPA without a mapping is dead policy: `_resolve_name()` raises `ToolAccessDenied` before OPA is ever reached.

## Adding a new MCP server (service)

New services that depend on `harness-gateway` or `harness-agents` need those packages copied into their Docker context. See `services/review_server/Dockerfile` for the pattern:

```dockerfile
COPY packages/harness-gateway /app/packages/harness-gateway
COPY packages/harness-agents /app/packages/harness-agents
RUN pip install -e /app/packages/harness-gateway -e /app/packages/harness-agents
```

Build context must be `.` (repo root), not the service subdirectory, so the `packages/` COPY works.

## review_server HTTP endpoint

`POST http://localhost:9003/review` — plain JSON endpoint for CI pipelines, pre-commit hooks, and webhooks.

```bash
DIFF=$(git diff origin/main...HEAD)
curl -s http://localhost:9003/review \
  -H "Content-Type: application/json" \
  -d "{\"diff_text\": $(echo "$DIFF" | jq -Rs .)}" | jq .
```

Body: `{"diff_text": "...", "task": "...", "provider": "ollama|gemini|openrouter"}` (`task` and `provider` optional).
Returns same schema as `review_diff` MCP tool. Errors: 401 for bad/missing key, 422 for missing `diff_text`, 400 for bad provider name or missing `OPENROUTER_API_KEY`, 500 for agent failure.

**Auth:** set `REVIEW_API_KEY` in env to require `Authorization: Bearer <key>`. When unset, the endpoint is open (dev/local mode). The empty default in `docker-compose.yml` (`${REVIEW_API_KEY:-}`) means auth is off by default locally — set the var before deploying publicly.

## Linter stub (semgrep)

`stub_servers/linter_server.py` runs semgrep against the added lines extracted from the diff. Rules are in `stub_servers/semgrep-rules.yml` — edit this file to add or tune rules, then `docker compose cp` to test without a full rebuild.

**metavariable-regex gotcha:** semgrep's `metavariable-regex` is anchored (`re.match`), not substring (`re.search`). To match a variable name that *contains* a keyword (e.g. `AWS_SECRET_ACCESS_KEY`), the regex must be `(?i).*secret.*` — not `(?i)secret`. Without the `.*` prefix, compound names silently miss.

## Prompt files

All LLM system prompts live in `prompts/` and are loaded at import time:

| File | Loaded by | Used for |
|---|---|---|
| `prompts/classify.md` | `harness_supervisor/nodes.py` | Task classification (system message) |
| `prompts/synthesise.md` | `harness_supervisor/nodes.py` | Final-response synthesis (system message, LLM call) |
| `prompts/code_reviewer.md` | `harness_agents/reviewer.py` | Code review system prompt |
| `prompts/architect.md` | `harness_agents/architect.py` | Architect agent system prompt |
| `prompts/sre.md` | `harness_agents/sre.py` | SRE agent system prompt |

Override the prompts directory: set `PROMPTS_DIR=/path/to/prompts` before starting any service.

`synthesise_node` makes a real LLM call when `llm_provider` is supplied (graph wiring); falls back to a string-format summary when `llm_provider=None` (test-only path).

## Eval suites (reviewer + architect)

See [`docs/eval-guide.md`](../eval-guide.md) for fixture formats, pass bars, CI setup, and known gotchas.

## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
