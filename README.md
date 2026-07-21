# AI Harness

![AI Harness](docs/ai-harness.jpeg)

A governed, memory-augmented agent harness with production hardening. Every tool call routes through a governance layer: OAuth 2.1 auth, OPA policy enforcement, and a tamper-evident Dolt audit log. Recurring successful remediations are promoted into versioned, HITL-gated skills via a procedural skill-learning pipeline. Supports MCPJungle and ContextForge as MCP gateway backends with a feature-flag rollback.

## Agents

| Agent | What it does | Doc |
|---|---|---|
| **Code Reviewer** | Lint + LLM analysis of diffs; returns structured findings with severity and suggestions | [docs/code-reviewer.md](docs/code-reviewer.md) |
| **Adversarial Code Critic** | Attacks the Code Reviewer's first-pass findings; a confirmed/escalated CRITICAL requires a concrete `exploit_scenario`, not a bare severity label. Opt-in, run separately via `adversarial_review` | [docs/code-reviewer.md](docs/code-reviewer.md) |
| **Architect** | Four-phase codebase analysis for layering violations, coupling, and abstraction leaks | [docs/architect.md](docs/architect.md) |
| **Adversarial Architecture Critic** | Attacks the Architect's first-pass synthesis findings; a confirmed/escalated HIGH+ finding requires a concrete `regression_scenario`, not a bare severity label. Opt-in, run separately via `adversarial_architecture_review` | [docs/architect.md](docs/architect.md) |
| **SRE** | Incident investigation with skill-guided remediation, semantic cache, and human-gated shell exec | [docs/sre.md](docs/sre.md) |

Skills learned from agent runs are surfaced as Claude Code slash commands. See [docs/skills.md](docs/skills.md).

## Stack

- **Governance** — FastAPI service (`:8090`) that issues RS256 JWTs, enforces OPA policy, and writes tamper-evident audit rows to Dolt; exposes `GET /jwks` for public key distribution
- **MCPJungle** — MCP proxy that routes tool calls and exposes itself as an MCP server at `:8080/mcp`
- **OPA** — policy engine; `policies/harness.rego` maps agent roles to allowed tools; enforced on every request
- **Dolt** — git-versioned MySQL-compatible database; audit rows and skill versions are auto-committed so both logs are append-only and diffable
- **PostgreSQL** (`pgvector/pgvector:pg16`) — MCPJungle state, LangGraph checkpoints, and vector memory store; pgvector extension enables semantic search
- **Redis 7** — hot-read cache for the memory store; frequently accessed items served in-process without hitting PostgreSQL
- **LLM providers** — pluggable via `LLM_PROVIDER`: `ollama` (default), `gemini`, or `openrouter`. Switchable at runtime via `PUT /config` on the review-server
- **diff-proxy** — `git diff` on the baked sample repo, or fetches a PR diff from the GitHub API
- **linter-stub** — semgrep linter; rules in `stub_servers/semgrep-rules.yml`
- **github-mcp** — MCP server wrapping GitHub API for architect-role tools
- **sre-stub** — MCP server for SRE-role tools; pgvector runbook/log search when `PG_DSN` is set; TF-IDF skill search from Dolt
- **skills-registry-server** — FastMCP service (`:9006`) exposing all registry operations as 14 MCP tools
- **review-server** — FastMCP service wrapping the code-reviewer agent; callable from Claude Code and CI pipelines via `POST /review`
- **ContextForge** (`:4444`) — production MCP gateway; alternative to MCPJungle via `GATEWAY_BACKEND=contextforge`
- **Prometheus + Grafana** — optional monitoring; `make monitoring-up`; pre-built cost-per-role dashboard at `localhost:3000`

## Quick start

**Prerequisites:** Docker, Ollama running with `qwen2.5-coder` pulled, [uv](https://docs.astral.sh/uv/) installed.

```bash
# 1. Configure
cp .env.example .env
# edit .env — set CODE_REVIEWER_SECRET, ARCHITECT_SECRET, SRE_SECRET, REGISTRY_OPERATOR_SECRET
# JWT_PRIVATE_KEY_FILE defaults to test-fixtures/jwt-test-key.pem (dev only; set ENV=test)

# 2. Build and start the stack
docker compose build diff-proxy linter-stub github-mcp sre-stub review-server governance dolt skills-registry-server
docker compose up -d
sleep 30  # wait for Dolt to init and MCP init containers to register servers

# 3. Install Python deps
uv sync --all-packages

# 4. Run all tests
make test-integration
```

## Configuration

All options are in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | Active LLM provider: `ollama`, `gemini`, or `openrouter` |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | LLM for chat/reasoning when provider is `ollama` |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model when provider is `gemini` (requires `GEMINI_API_KEY`) |
| `OPENROUTER_MODEL` | `anthropic/claude-3.5-sonnet` | Model when provider is `openrouter` (requires `OPENROUTER_API_KEY`) |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model for semantic memory search (768 dims) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `GOVERNANCE_URL` | `http://localhost:8090` | Governance service URL |
| `JWT_PRIVATE_KEY_FILE` | `test-fixtures/jwt-test-key.pem` | PEM RSA private key for RS256 JWT signing. Set `ENV=test` when using the committed test key. |
| `CODE_REVIEWER_SECRET` | — | Client secret for the `code-reviewer` OAuth client |
| `ARCHITECT_SECRET` | — | Client secret for the `architect` OAuth client |
| `SRE_SECRET` | — | Client secret for the `sre` OAuth client |
| `REGISTRY_OPERATOR_SECRET` | — | Client secret for the `human-operator` OAuth client (skills registry) |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `PG_DSN` | `postgresql://harness:harness@localhost:5432/harness` | PostgreSQL DSN |
| `DOLT_HOST` | `localhost` | Dolt MySQL endpoint host |
| `DOLT_PORT` | `3306` | Dolt MySQL endpoint port |
| `GATEWAY_BACKEND` | `mcpjungle` | Active MCP backend: `mcpjungle` or `contextforge` |
| `LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG` for raw LLM responses and tool payloads) |

### Runtime LLM config

`GET /config` and `PUT /config` on the review-server (`:9003`) change LLM settings at runtime without rebuilding. Changes persist in PostgreSQL and are shared with the SRE demo (`make demo-sre`).

```json
{
  "llm_provider": "openrouter",
  "openrouter": { "model": "anthropic/claude-sonnet-4-6", "max_tokens": 2048 }
}
```

## Connect Claude Code

Add to Claude Code settings (`.mcp.json` or settings UI):

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

Claude Code will see all registered tools as `mcp__ai-harness__<name>`. For long-running tools (e.g. `bootstrap_architecture`):

```bash
MCP_TOOL_TIMEOUT=300000 claude   # 5-minute timeout
```

### Available MCP tools

| Short name | MCPJungle name | Role | What it does |
|---|---|---|---|
| `review_diff` | `review_server__review_diff` | code_reviewer | Full code review — lints + analyses diff, returns structured findings |
| `adversarial_review` | `review_server__adversarial_review` | adversarial_code_critic | Attacks a first-pass `review_diff` output; confirmed/escalated CRITICALs require a concrete `exploit_scenario` |
| `git_diff` | `diff_proxy__git_diff` | code_reviewer | Get a diff: passthrough, GitHub PR, or local git refs |
| `run_linter` | `linter_stub__run_linter` | code_reviewer | Semgrep lint on diff additions |
| `coverage_report` | `linter_stub__coverage_report` | code_reviewer | Per-file coverage data (stub) |
| `repo_conventions_read` | `github_mcp__repo_conventions_read` | code_reviewer | Fetch coding standards from a GitHub repo |
| `codebase_search` | `architect_stub__codebase_search` | architect | Search file/symbol patterns via GitHub API |
| `adr_read` | `architect_stub__adr_read` | architect | Read ADRs from `docs/adr/` in a GitHub repo |
| `architecture_review` | `review_server__architecture_review` | architect | Four-phase architectural analysis |
| `bootstrap_architecture` | `review_server__bootstrap_architecture` | architect | Generate `ARCHITECTURE.md`; needs `MCP_TOOL_TIMEOUT=300000` |
| `adversarial_architecture_review` | `review_server__adversarial_architecture_review` | adversarial_architecture_critic | Attacks a first-pass `ArchitectAgent` synthesis output; confirmed/escalated HIGH+ findings require a concrete `regression_scenario` |
| `execute_architecture_check` | `review_server__execute_architecture_check` | architect | Run architecture invariant checks (stub) |
| `code_health_score` | `review_server__code_health_score` | architect | Cyclomatic complexity per file, 0–10 scores |
| `codebase_hotspots` | `review_server__codebase_hotspots` | architect | Rank files by hotspot risk |
| `logical_coupling` | `review_server__logical_coupling` | architect | Files that historically co-change |
| `issue_create` | `github_mcp__issue_create` | architect | File a GitHub issue |
| `runbook_read` | `sre_stub__runbook_read` | sre | Semantic search over runbooks |
| `log_search` | `sre_stub__log_search` | sre | Semantic search over log events |
| `observability_query` | `sre_stub__observability_query` | sre | Observability query (stub) |
| `shell_exec` | `sre_stub__shell_exec` | sre | Execute shell command; requires human approval token |
| `skill_search` | `sre_stub__skill_search` | sre | TF-IDF lookup of proven remediation formulas |

## Tests

602 tests total — see [docs/tests.md](docs/tests.md) for full coverage tables.

```bash
make test-integration   # 267 integration tests
make test-unit          # 314 unit tests
pytest -m eval -v -s    # 19 eval tests (Ollama only)
```

## Project layout

```
├── packages/
│   ├── harness-gateway/    # GatewayClient + ContextForgeGatewayClient
│   ├── harness-agents/     # CodeReviewerAgent, ArchitectAgent, SREAgent, LLM providers
│   ├── harness-memory/     # PostgresMemoryStore, DoltFormulaStore, ConsolidationWorker
│   ├── harness-supervisor/ # LangGraph supervisor graph, HarnessState, OTel spans
│   └── harness-tests/      # Integration + unit + eval tests
├── services/
│   ├── governance/         # OAuth (RS256) + OPA + Dolt audit + /metrics + /jwks (port 8090)
│   ├── skills_registry/    # Skills registry MCP server (port 9006)
│   ├── review_server/      # Code review MCP server + HTTP endpoint (port 9003)
│   ├── dolt/               # Dolt init — audit_log, skills, episodes, candidates, formulas
│   ├── postgres/           # PostgreSQL init — enables pgvector extension
│   ├── grafana/            # Pre-provisioned cost-per-role dashboard
│   └── prometheus/         # Scrape config for governance /metrics
├── stub_servers/           # git_diff, run_linter, architect, sre MCP servers
├── prompts/                # LLM system prompts (classify, synthesise, agents)
├── eval-fixtures/          # Reviewer and architect eval fixtures
├── policies/               # OPA policy (harness.rego)
├── scripts/
│   ├── sync_skills.py      # Sync active skills → .claude/commands/skill-*.md
│   └── skills_cli.py       # CLI for the skill-learning pipeline
├── docs/
│   ├── code-reviewer.md    # Code reviewer agent guide
│   ├── architect.md        # Architect agent guide
│   ├── sre.md              # SRE agent guide
│   ├── skills.md           # Skill lifecycle and registry guide
│   ├── tests.md            # Full test coverage tables
│   ├── eval-guide.md       # Eval fixture format and CI setup
│   ├── runbooks/           # Operational runbooks (seed data)
│   └── adr/                # Architecture decision records
├── security/               # OWASP Agentic AI Top 10 review
├── docker-compose.yml
└── .env.example
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full request flow and design decisions, and [CLAUDE.md](CLAUDE.md) for operational notes and gotchas.
