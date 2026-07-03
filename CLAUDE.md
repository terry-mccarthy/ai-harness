# AI Harness — Claude Code Guide

## Ways of working

See `.claude/rules/ways-of-working.md`.

---

## Running the stack

```bash
# First time or after any changes to stub_servers/ or services/
docker compose build diff-proxy linter-stub github-mcp sre-stub review-server governance dolt
docker compose down && docker compose up -d
# wait ~30s for Dolt to init and MCP init containers to register servers
docker compose ps   # all should show (healthy) or Exited (0)
```

```bash
# Run all tests
make test-integration
```

## Python environment

Python 3.14 is in use. The project uses **uv** for dependency management with a workspace layout.

```bash
# First time or after adding/changing dependencies
uv sync --all-packages   # creates .venv and installs all workspace packages

# Regenerate uv.lock after editing any pyproject.toml
uv lock
```

Workspace members in `packages/` reference each other via `[tool.uv.sources]` with `{ workspace = true }` — do not use PyPI paths for these.

All Docker services (`services/governance`, `services/review_server`, `stub_servers`) are also workspace members. Their `requirements.txt` files are generated from `uv.lock` and committed — Dockerfiles install from them:

```bash
# After editing any service's pyproject.toml or after uv lock
make requirements
```

Never hand-edit a service `requirements.txt` — it is always regenerated from the lockfile.

## Request flow (Phase 6+)

```
Agent / GatewayClient
    → POST /check          (governance :8090 — OPA policy decision)
    → POST /api/v0/tools/invoke   (MCPJungle :8080 or CF :4444 — direct)
        → MCP server (github-mcp :9010 / review-server :9003 / sre-stub :9005 / etc.)
    → POST /audit          (governance :8090 — async Dolt write, fire-and-forget)
```

Governance is no longer in the forwarding path. GatewayClient calls the gateway (MCPJungle or ContextForge) directly, with governance providing policy checks and audit as a sidecar.

Claude Code connects to MCPJungle at `:8080/mcp` directly.

---

## Reference docs

Detailed gotchas and internals live in `docs/dev/`. Read the relevant file before working on that component.

| Topic | File |
|---|---|
| GatewayClient, MCPJungle, ContextForge, MCP timeout, git_diff modes | `docs/dev/gateway.md` |
| Governance service, OPA policy, human approval tokens | `docs/dev/governance.md` |
| Dolt init, gotchas, version quirks | `docs/dev/dolt.md` |
| LLM providers (Ollama, OpenRouter, Gemini), `build_llm_from_env`, runtime config | `docs/dev/llm-providers.md` |
| Memory layer, SRE signals, DynamicSREAgent, cache, orchestration, architectural gate | `docs/dev/memory-agents.md` |
| FastMCP config, adding tools/servers, review_server HTTP endpoint, linter, prompts, evals | `docs/dev/mcp-servers.md` |
| Monitoring stack, Prometheus, Grafana, Claude Code OTEL pipeline | `docs/dev/monitoring.md` |
