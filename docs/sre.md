# SRE Agent

The SRE agent investigates incidents using a ReAct loop guided by runbooks, log search, and proven remediation formulas (skills). It can execute shell commands with human-in-the-loop approval.

## Skill-guided investigation

Before the ReAct loop starts, the agent looks up the task against the Dolt `skills` table using TF-IDF keyword matching (`DoltFormulaStore.lookup`). If a matching proven skill is found, its **name, id, and description** — not its raw steps — are injected into the opening message as a steer: "a proven skill exists for this incident type... prefer calling `run_skill` with this id over improvising." The LLM decides for itself whether to call it; the harness no longer auto-executes a skill's steps server-side.

`run_skill` is a **native dispatch inside `DynamicSREAgent`, not a gateway/MCP tool** — there is no `TOOL_NAME_MAP` entry and no MCP server hosts it. When the LLM emits `call_tool(run_skill, {skill_id, inputs})`, the agent drives `SkillRunner(self.gateway).execute(...)` directly against its own already-authenticated `GatewayClient`, so every step of the skill still gets its own per-step OPA check under the agent's real credentials and each step's `on_failure` policy (ABORT/CONTINUE/ROLLBACK) is honoured. If no skill was preloaded, the agent can discover one mid-investigation via `skill_search` (ranked, scored ACTIVE matches) and call `run_skill` with the best match's id. A successful `run_skill` call carries the matched skill's `runbook_ref` back to the LLM, and the final report can cite the executed skill via `skill_ref` (`SRE_OUTPUT_SCHEMA`).

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
| `runbook_read` | Semantic pgvector search over operational runbooks, top-3 by cosine similarity, filtered to score ≥ 0.80 (`RUNBOOK_MIN_SCORE`); below threshold returns an empty `runbooks` list rather than a weak match. Seed with `make seed-runbooks` |
| `log_search` | Bounded semantic pgvector search over log events, capped at 5 results (`MAX_LOG_RESULTS`) and filtered to score ≥ 0.55 (`LOG_MIN_SCORE`); response includes `returned_count`/`total_count` so the agent can tell "all relevant lines" from "truncated". Seed with `make seed-logs` |
| `shell_exec` | Execute a shell command; requires scoped `human_approval_token` |
| `skill_search` | Read-only TF-IDF discovery — returns ranked, scored ACTIVE skill matches (id + score) for an incident signature; excludes deprecated/revoked/expired |

`run_skill` (execute a skill by id) is deliberately **not** in this table — it's a native dispatch inside `DynamicSREAgent`, not a gateway-routed MCP tool; see "Skill-guided investigation" above.

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
