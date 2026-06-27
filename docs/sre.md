# SRE Agent

The SRE agent investigates incidents using a ReAct loop guided by runbooks, log search, and proven remediation formulas (skills). It can execute shell commands with human-in-the-loop approval.

## Skill-guided investigation

Before the ReAct loop starts, the agent looks up the task against the Dolt `skills` table using TF-IDF keyword matching. If a matching proven skill is found, its steps are injected into the opening message as a structured investigation plan — the agent follows the steps rather than reasoning from scratch.

Skills are discovered through the episode → candidate → promotion pipeline (see [skills.md](skills.md)). Once promoted, a skill is automatically selected by `POST /skills/select` when its `task_patterns` match the incoming task.

## Semantic response cache

Successful remediation runs are cached in the `"cache"` pgvector namespace. When a new task is submitted:

1. Exact key match (Redis, O(1)) — returns cached result immediately
2. Semantic similarity (pgvector cosine, threshold 0.92) — returns cached result for near-identical tasks

Cache hits skip the entire ReAct loop — no LLM calls, no tool invocations. Pass `force_refresh=True` to bypass.

## Human approval for shell commands

`shell_exec` requires a scoped human approval token. The graph pauses at `human_gate` and emits a prompt. The operator provides a token via `X-Human-Approval-Token`:

```python
token = governance.issue_approval_token(thread_id=thread_id, tool_name="shell_exec", ttl=600)
graph.resume(thread_id=thread_id, human_approval_token=token)
```

Tokens are scoped to a specific `thread_id` and tool name — a token for thread A cannot resume thread B, and a `shell_exec` token cannot approve other tools.

## Tools available to the agent (OPA-enforced)

| Short name | What it does |
|---|---|
| `observability_query` | Observability query (stub — wire to real metrics backend) |
| `runbook_read` | Semantic pgvector search over operational runbooks; seed with `make seed-runbooks` |
| `log_search` | Semantic pgvector search over log events; seed with `make seed-logs` |
| `shell_exec` | Execute a shell command; requires scoped `human_approval_token` |
| `skill_search` | TF-IDF lookup of proven remediation formulas from Dolt |

The `sre` OPA role is blocked from architect and code-reviewer tools.

## Seeding knowledge bases

```bash
make seed-runbooks   # docs/runbooks/*.md → pgvector "runbooks" namespace
make seed-logs       # docs/logs/*.jsonl  → pgvector "logs" namespace
```

Without seeding, `runbook_read` and `log_search` fall back to stub responses.

## Running the SRE demo

```bash
make demo-sre
```

Reads LLM config from the `server_config` PostgreSQL table (same provider as the review-server). Shows a capability banner indicating which stores are connected.

## Management slash commands

Full skill lifecycle management from Claude Code — see [skills.md](skills.md) for the management commands and the `make sync-skills` workflow.
