# AI Harness

A governed code-review agent. Submit a git diff, get back structured security and quality findings with a pass/fail verdict. Every tool call routes through a central MCP proxy so access is auditable.

## What it does

Point it at a diff (or let it fetch one from a repo). It runs a linter, analyses both, and returns structured JSON:

```json
{
  "verdict": "fail",
  "findings": [
    {
      "severity": "CRITICAL",
      "file": "auth.py",
      "line": 14,
      "message": "Password is being printed to stdout — credential leak risk.",
      "suggestion": "Remove the print statement."
    }
  ],
  "summary": "The diff introduces a critical security vulnerability: passwords are logged in plaintext."
}
```

The reviewer checks for security vulnerabilities (credential leaks, injection flaws, path traversal), code quality issues (error handling gaps, dead code, resource leaks), and architectural concerns (hardcoded values, tight coupling, shared mutable state). Findings are classified as `CRITICAL`, `WARNING`, or `INFO`; verdict is `fail` if any `CRITICAL` finding exists.

The agent is also exposed as an MCP tool (`review_diff`) — Claude Code or any MCP client can call it directly.

## Stack

- **MCPJungle** — MCP proxy that routes all tool calls, keeps an audit log, and exposes itself as an MCP server
- **OPA** — policy engine (wired, enforcement requires Enterprise tier)
- **PostgreSQL** — MCPJungle state store
- **Ollama** (`qwen2.5-coder:7b`) — local LLM, no API key needed
- **git-diff-stub** — runs real `git diff` on a baked-in sample repo
- **linter-stub** — pattern-matching linter (swappable for a real one)
- **review-server** — FastMCP service wrapping the full agent; callable from Claude Code

## Quick start

**Prerequisites:** Docker, Ollama running with `qwen2.5-coder` pulled.

```bash
cd ai-harness

# 1. Configure
cp .env.example .env
# edit .env — set OLLAMA_MODEL, CODE_REVIEWER_SECRET, etc.

# 2. Build and start the stack
docker compose build git-diff-stub linter-stub review-server
docker compose up -d
sleep 20  # wait for init containers to register MCP servers

# 3. Install Python deps
python3 -m venv .venv
.venv/bin/pip install -e packages/harness-gateway -e packages/harness-agents -e packages/harness-tests

# 4. Run all tests
source .env
.venv/bin/pytest packages/harness-tests/ -v -m integration
```

## Configuration

All options are in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5-coder` | Model used by the reviewer agent |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `MCPJUNGLE_URL` | `http://localhost:8080` | MCPJungle proxy URL |
| `CODE_REVIEWER_SECRET` | — | Client secret (interface compat, unused in free tier) |
| `LOG_LEVEL` | `INFO` | Log verbosity — set to `DEBUG` to see full prompts, raw LLM responses, and MCPJungle request/response payloads |

To enable debug logging without restarting the whole stack:

```bash
LOG_LEVEL=DEBUG docker compose up -d git-diff-stub linter-stub review-server
```

## Tests (9 total)

| Test | What it proves |
|---|---|
| `test_reviewer_produces_structured_output` | Diff in → valid JSON out, catches obvious bugs |
| `test_tool_calls_go_through_gateway` | Tool calls are visible in the gateway audit log |
| `test_reviewer_denied_cross_role_tool` | Unlisted tools are blocked before the network call |
| `test_review_diff_tool_is_reachable` | `review_diff` MCP tool is registered and callable |
| `test_review_diff_returns_valid_schema` | MCP tool output satisfies the output schema |
| `test_review_diff_catches_credential_leak` | End-to-end: MCP call → agent → model → CRITICAL finding |
| `test_git_diff_returns_real_diff_format` | `git_diff` runs real git and returns proper diff output |
| `test_git_diff_contains_commit_changes` | Diff output contains the actual changed lines |
| `test_git_diff_respects_ref` | Tool accepts base/head refs |

## Connect Claude Code

MCPJungle exposes itself as an MCP server. Add to Claude Code settings:

```json
{
  "mcpServers": {
    "ai-harness": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

Claude Code will see all registered tools including `review_server__review_diff`.

## Project layout

```
ai-harness/
├── packages/
│   ├── harness-gateway/   # GatewayClient — HTTP calls to MCPJungle
│   ├── harness-agents/    # CodeReviewerAgent, AgentState, output schema
│   └── harness-tests/     # Integration tests
├── services/
│   └── review_server/     # review_diff MCP tool (wraps the agent)
├── stub_servers/          # git_diff (real) and run_linter (stub) MCP servers
├── policies/              # OPA policy
├── docker-compose.yml
└── .env.example
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full request flow and design decisions, and [CLAUDE.md](CLAUDE.md) for operational notes and gotchas.
