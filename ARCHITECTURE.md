# AI Harness — Architecture

> This file is the authoritative architecture specification for the ai-harness project.
> It is consumed by both human developers and AI coding agents (Claude Code, Codex, etc.).
>
> Rules marked **[HARD]** are inviolable. If your solution requires violating a [HARD] rule,
> stop and surface the conflict rather than proceeding. Rules marked **[SOFT]** are strong
> preferences that require explicit justification to override.

---

## Overview

A governed, self-learning agent harness. Current capability: a code-review agent that takes
a git diff and returns structured security and quality findings with a pass/fail verdict.
Every tool call is authenticated, OPA-policy-checked, and committed to a tamper-evident Dolt
audit log before it reaches a tool server.

The harness also provides a three-layer memory architecture (Phase 2): a LangGraph checkpointer
for fault-tolerant graph execution, a vector memory store for cross-session agent knowledge,
and a Dolt formula store for versioned workflow templates.

Claude Code (or any MCP client) can call `review_diff` directly via MCPJungle. The agent's
internal tool calls route through the governance service, so the full review is auditable
end-to-end.

---

## Architectural Invariants

These are the non-negotiable constraints that define this system. Every other design decision
flows from them.

**[HARD]** The governance service is a mandatory intercept for all agent-to-agent tool calls.
No agent code may call MCPJungle (`:8080`) directly — all calls MUST route through
governance (`:8090`), which handles auth, policy, and audit before forwarding.

**[HARD]** Every tool call — allowed or denied — MUST produce an audit row in Dolt and trigger
a `DOLT_COMMIT`. The audit log must be complete; partial audit is not acceptable.

**[HARD]** `harness-gateway` must not import from `harness-agents`, `harness-memory`, or `harness-tests`.

**[HARD]** `harness-agents` must not import from `harness-tests`.

**[HARD]** `harness-memory` must not import from `harness-agents` or `harness-tests`.

**[HARD]** `harness-tests` is a test-only package. It must not be imported by any service
or runtime package (`governance`, `review_server`, `harness-gateway`, `harness-agents`, `harness-memory`).

**[HARD]** Any new tool added to a stub server or service MUST have a corresponding OPA policy
entry in `policies/harness.rego` before the change is considered complete. Tools without
policy entries default to deny — this is a feature, not a gap.

**[HARD]** The `audit_log` table is append-only. Application code must never issue DELETE or
UPDATE statements against it. The `harness` DB user enforces this at the database level,
but no application code should attempt it regardless.

**[SOFT]** New agent roles should be added as new OPA rule blocks, not by extending the tool
list of an existing role. Role boundaries should remain narrow and explicit.

**[SOFT]** New MCP servers should be implemented using FastMCP with streamable-HTTP transport,
consistent with all existing stub servers and service implementations.

---

## Package Dependency Direction

```
harness-tests
    ↓ (test imports only)
harness-agents      harness-memory
    ↓                    ↓
harness-gateway     (standalone — no runtime package deps)

✗  harness-gateway → harness-agents   [HARD violation]
✗  harness-gateway → harness-memory   [HARD violation]
✗  harness-gateway → harness-tests    [HARD violation]
✗  harness-agents  → harness-tests    [HARD violation]
✗  harness-memory  → harness-agents   [HARD violation]
✗  harness-memory  → harness-tests    [HARD violation]
✗  any service     → harness-tests    [HARD violation]
```

---

## Request Flow

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
governance  :8090  (FastAPI)          ← MANDATORY INTERCEPT [HARD]
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

---

## Services

| Service          | Image                          | Port | Role                                                         |
|------------------|--------------------------------|------|--------------------------------------------------------------|
| `postgres`       | pgvector/pgvector:pg16         | 5432 | MCPJungle state, LangGraph checkpoints, vector memory store  |
| `redis`          | redis:7-alpine                 | 6379 | Hot-read cache for memory store                              |
| `opa`            | openpolicyagent/opa:latest     | 8181 | Policy engine — evaluates `policies/harness.rego`            |
| `mcpjungle`      | mcpjungle/mcpjungle:latest     | 8080 | MCP proxy / tool registry / MCP server for Claude Code       |
| `dolt`           | local build                    | 3306 | Git-versioned audit log + formula store                      |
| `governance`     | local build                    | 8090 | OAuth token issuance, OPA enforcement, Dolt audit            |
| `git-diff-stub`  | local build                    | 9001 | Real `git diff` MCP server (baked sample repo)               |
| `linter-stub`    | local build                    | 9002 | Pattern-matching `run_linter` MCP server                     |
| `architect-stub` | local build                    | 9004 | Stub MCP server for architect-role tools                     |
| `sre-stub`       | local build                    | 9005 | Stub MCP server for SRE-role tools                           |
| `review-server`  | local build                    | 9003 | `review_diff` MCP tool — runs full code-reviewer agent       |
| `register-*`     | mcpjungle image                | —    | One-shot init containers that register MCP servers           |

---

## Python Packages (Monorepo)

```
packages/
  harness-gateway/   — GatewayClient: JWT auth + HTTP calls to governance
  harness-agents/    — CodeReviewerAgent + AgentState TypedDict + output schema
  harness-memory/    — PostgresMemoryStore, DoltFormulaStore, ConsolidationWorker
  harness-supervisor/ — LangGraph supervisor orchestration, graph nodes, approval tokens
  harness-tests/     — pytest integration tests (69 tests across 4 test files)

services/
  governance/        — OAuth 2.1 token issuance + OPA enforcement + Dolt audit
  dolt/              — Dolt init: audit_log + formulas + formula_pours + seed data
  postgres/          — PostgreSQL init: enables pgvector extension
  review_server/     — FastMCP server wrapping CodeReviewerAgent
```

Dependencies: `harness-tests` → `harness-agents` → `harness-gateway`; `harness-memory`
is standalone (no dependency on other harness packages). See
[Package Dependency Direction](#package-dependency-direction) above for the enforced direction.

---

## Governance Service

`services/governance/server.py` — three responsibilities per request:

1. **Auth**: validates `Authorization: Bearer <JWT>`, rejects with 401 on missing/expired/invalid tokens
2. **Policy**: calls `POST /v1/data/harness/allow` on OPA with `{agent_role, tool_name}`; returns 403 if denied
3. **Audit**: inserts a row into Dolt `audit_log`, then calls `DOLT_COMMIT` — every tool call is a git commit

Token issuance: `POST /oauth/token` with client credentials (grant type `client_credentials`).
Three clients: `architect`, `code-reviewer`, `sre`. Tokens are HS256 JWTs signed with
`JWT_SECRET`, 15-min TTL.

---

## OPA Policy

`policies/harness.rego` maps agent roles to allowed tool names:

| Role            | Allowed tools                                                                        |
|-----------------|--------------------------------------------------------------------------------------|
| `architect`     | `codebase_search`, `adr_read`, `adr_write`, `diagram_gen`                            |
| `code_reviewer` | `git_diff`, `run_linter`, `coverage_report`, `repo_conventions_read`, `review_diff`  |
| `sre`           | `observability_query`, `runbook_read`, `log_search`, `shell_exec`                    |

Default: deny. Cross-role calls (e.g. architect calling `shell_exec`) return 403 without
reaching the tool server.

---

## Dolt Audit Log

Every tool call — allowed or denied — writes a row to `audit_log`:

| Column            | Description                                    |
|-------------------|------------------------------------------------|
| `agent_id`        | OAuth `sub` claim (client_id)                  |
| `tool_name`       | Full MCPJungle tool name (`server__tool`)       |
| `server_id`       | Short tool name                                |
| `request_hash`    | SHA-256 of request body (first 16 hex chars)   |
| `response_hash`   | SHA-256 of response body (first 16 hex chars)  |
| `policy_decision` | `allow` or `deny`                              |
| `policy_rule`     | OPA rule that matched                          |
| `timestamp_ms`    | Unix milliseconds                              |
| `latency_ms`      | Round-trip to MCPJungle                        |

After every INSERT, governance calls `CALL DOLT_COMMIT('-Am', 'audit: <tool> by <agent> [allow/deny]')`.
The full call history is queryable as a git log:

```sql
SELECT message FROM dolt_log LIMIT 20;
SELECT * FROM dolt_diff_audit_log;   -- row-level diff per commit
```

The `harness` DB user has INSERT + SELECT only — no DELETE. The audit log is append-only
by construction and by policy.

Dolt also hosts the **formula store** (`formulas` + `formula_pours` tables). Every
`propose()` call and every quality update is a Dolt commit, so formula history is
fully diffable with `dolt log` and `dolt diff`.

---

## Memory Layer (Phase 2)

Three layers, each with a distinct scope and backend:

### Layer 1 — Checkpointer

LangGraph `AsyncPostgresSaver` (from `langgraph-checkpoint-postgres`) persists the full
graph state dict at every super-step. Scoped to `thread_id`; enables fault-tolerant
resumption and human-in-the-loop pause/resume. Tables managed by LangGraph itself.

**Gotcha:** always use `AsyncPostgresSaver.from_conn_string(dsn)` — constructing directly
from a raw psycopg connection will fail on `CREATE INDEX CONCURRENTLY inside a transaction`.

### Layer 2 — Memory Store

`PostgresMemoryStore` (`packages/harness-memory/harness_memory/memory_store.py`):

- **Write path**: stores value + Ollama embedding in `memory_items` (PostgreSQL); invalidates
  Redis key.
- **Read path**: checks Redis first (cache hit → increments `cache_hits`); on miss, reads
  PostgreSQL and populates Redis with a 1-hour TTL.
- **Semantic search**: pgvector `<=>` cosine distance on stored embeddings; returns items
  ordered by relevance.
- **Episodic vs semantic**: agents write `memory_type='episodic'`; `ConsolidationWorker`
  clusters unconsolidated episodes and promotes clusters to `memory_type='semantic'`.

Embedding dimension is auto-detected at `setup()` time by calling the configured Ollama
model. If the table already exists with a different dimension, it is dropped and recreated.
Current default: `qwen2.5-coder:32b` → 5120 dims.

```
memory_items schema (PostgreSQL):
  id           UUID PK
  namespace    TEXT          — e.g. 'architect', 'sre'
  key          TEXT          — e.g. 'adr:auth-middleware'
  memory_type  TEXT          — 'episodic' | 'semantic'
  value        JSONB
  source_ids   UUID[]        — episodic items that produced this semantic item
  embedding    vector(N)     — N auto-detected from Ollama model
  confidence   FLOAT
  consolidated BOOL          — TRUE once absorbed into a semantic item
  created_at   TIMESTAMPTZ
  expires_at   TIMESTAMPTZ
  UNIQUE (namespace, key)
```

Schema versioned with Alembic (`packages/harness-memory/alembic/`).

### Layer 3 — Formula Store

`DoltFormulaStore` (`packages/harness-memory/harness_memory/formula_store.py`):

- Every `propose()` call inserts a new version row and calls `DOLT_COMMIT`.
- `lookup(agent_role, task)` uses TF-IDF keyword overlap (no ML model); returns the
  best-matching active formula above a 0.05 score threshold.
- Quality scoring: `ConsolidationWorker.run_pass()` reads `formula_pours`, computes
  success rate, and updates `quality_score` + `status` (active / proven / review).

```
formulas schema (Dolt):
  id              VARCHAR(64) PK
  name            TEXT
  agent_role      TEXT
  version         INTEGER
  status          TEXT          — 'draft' | 'active' | 'proven' | 'review' | 'deprecated'
  description     TEXT
  input_schema    JSON
  steps           JSON
  output_contract JSON
  quality_score   FLOAT
  created_at      DATETIME
  created_by      TEXT
  UNIQUE (id, version)

formula_pours schema (Dolt):
  id          BIGINT AI PK
  formula_id  VARCHAR(64)
  success     BOOLEAN
  poured_at   DATETIME
```

Seed formulas committed on Dolt init: `sre:triage-incident`, `code_reviewer:review-pr`,
`architect:write-adr`.

### ConsolidationWorker

`packages/harness-memory/harness_memory/consolidation.py`:

`run_pass(namespace)`:
1. Delete expired items in the namespace
2. Fetch unconsolidated episodic items (with stored embeddings)
3. Greedy cosine-similarity clustering at threshold **0.95** — code LLMs produce a high
   baseline similarity (~0.86–0.94) for all short texts; 0.95 sits above that floor
4. For each cluster: write a semantic item, mark source episodes `consolidated=True`
5. Update quality scores for all formulas with ≥ 10 pours

Triggered manually: `make consolidate`

---

## GatewayClient

`packages/harness-gateway/harness_gateway/client.py`:

- `_get_token()` posts to `{gateway_url}/oauth/token`, caches the JWT until 30s before expiry
- Falls back gracefully (returns `None`) if the gateway returns 404 on `/oauth/token`
- `call_tool(name, params)` maps short names → `server__tool` via `TOOL_NAME_MAP`, adds
  `Authorization: Bearer` header, POSTs to `/api/v0/tools/invoke`
- 401 and 403 responses raise `ToolAccessDenied`
- Response unwrapping: MCPJungle returns `{"content": [{"type": "text", "text": "<json>"}]}`; the client unwraps to a plain dict

---

## CodeReviewerAgent

`packages/harness-agents/harness_agents/reviewer.py`:

- Calls `git_diff` and `run_linter` via the gateway
- Builds a prompt from both results, calls Ollama (`qwen2.5-coder:7b`)
- Validates the model response against `REVIEWER_OUTPUT_SCHEMA` (jsonschema)
- Retries up to 3× on schema failure, feeding the error back to the model
- Strips markdown fences if the model ignores the raw JSON instruction

---

## Output Schema

```json
{
  "verdict": "pass | fail",
  "findings": [
    {
      "severity": "CRITICAL | WARNING | INFO",
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

---

## git_diff Tool

The `git-diff-stub` container bakes in a sample repo at `/app/sample-repo` with two commits —
the second adds a password-logging `print` statement. This lets the full review pipeline run
against a real diff without an external repo.

The tool accepts:

- `diff_text` (string) — passthrough mode; echoed back unchanged (used by `CodeReviewerAgent`)
- `repo_path` + `base`/`head` refs — runs real `git diff` against the baked repo

---

## Fitness Functions

These checks express the architectural invariants above in executable form. They are
the enforcement layer for the constraints declared in the [Architectural Invariants](#architectural-invariants)
section. Every change to the codebase should pass all of them.

| Check | What it proves | Maps to constraint |
|---|---|---|
| `test_tool_calls_go_through_gateway` | Tool calls visible in gateway audit log | Governance is mandatory intercept [HARD] |
| `test_reviewer_denied_cross_role_tool` | Unlisted tools blocked before network call | OPA default-deny enforced [HARD] |
| `test_audit_row_written` | Tool call writes row to `audit_log` in Dolt | Audit log is complete [HARD] |
| `test_audit_dolt_commit_created` | Audit INSERT triggers a Dolt commit | Every call is a git commit [HARD] |
| `test_audit_no_delete` | `harness` DB user cannot DELETE from `audit_log` | Audit log is append-only [HARD] |
| `test_unknown_token_rejected` | Invalid bearer token returns 401 | Auth enforcement [HARD] |
| `test_architect_denied_tool` | Architect token cannot call `shell_exec` (403) | Role boundary enforcement [HARD] |
| `test_token_expiry` | Expired JWT returns 401 | Token TTL enforced [HARD] |
| `test_opa_deny_cross_role` | OPA returns `false` for architect + shell_exec | Policy engine correct [HARD] |

> **Note for agents:** the fitness functions above are integration tests that prove runtime
> behaviour. They are not a substitute for structural checks on import direction and package
> coupling. If Python import analysis tooling is added to CI in future, it should enforce the
> package dependency direction rules in the [Architectural Invariants](#architectural-invariants)
> section.

---

## What Agents Must NOT Do

These prohibitions apply to any AI coding agent working on this codebase. They are not
suggestions — violating them breaks the system's core guarantees.

- **[HARD]** Do not add calls from agent code directly to MCPJungle (`:8080`). All tool calls
  must go through governance (`:8090`).

- **[HARD]** Do not add a new tool to any stub server or service without a corresponding entry
  in `policies/harness.rego`. Deploying a tool without a policy entry is a governance gap.

- **[HARD]** Do not issue DELETE or UPDATE against `audit_log`, even in tests. Use a separate
  test database or table if isolation is needed.

- **[HARD]** Do not import `harness-tests` from any non-test package or service.

- **[HARD]** Do not import `harness-agents` or `harness-tests` from `harness-gateway`.

- **[HARD]** Do not import `harness-agents` or `harness-tests` from `harness-memory`.

- **[SOFT]** Do not add a new agent role by extending an existing role's tool list.
  Add a new role block in `harness.rego` instead.

- **[SOFT]** Do not name any MCP tool parameter `name` — it collides with MCPJungle's flat
  invoke body and silently corrupts the tool identifier. See CLAUDE.md for details.

- **[SOFT]** Do not bypass integration tests by mocking the governance service. Tests should
  run against the real governance container — that is what proves the invariants hold.

---

## Test Coverage (69 Tests)

| Phase | File                        | Tests | What they cover                                                          |
|-------|-------|----------|
| 0     | `test_thin_slice.py`        | 9     | Reviewer agent contract, gateway audit log, tool access denial, MCP reachability |
| 1     | `test_phase1_governance.py` | 17    | Auth, OPA policy enforcement, Dolt audit, token expiry                   |
| 2     | `test_phase2_memory.py`     | 27    | Checkpointer, memory store (write/read/search/TTL/Redis), consolidation, formula store |
| 3     | `test_phase3_agents.py`     | 4     | Agent protocol compliance, tool calls via gateway, shell_exec gating    |
| 4     | `test_phase4_supervisor.py` | 12    | LangGraph orchestration (classify/route/formula/human_gate/checkpoint), E2E task flows |

---

## Decision Log

| ID   | Decision                                                        | Status   |
|------|-----------------------------------------------------------------|----------|
| 0001 | Governance as mandatory intercept rather than per-tool auth     | Accepted |
| 0002 | Dolt for audit log — git-versioned, diffable, append-only       | Accepted |
| 0003 | OPA for policy — declarative, independently testable            | Accepted |
| 0004 | Monorepo with three packages — gateway / agents / tests         | Accepted |
| 0005 | MCPJungle as MCP proxy — Claude Code connects here, not to governance | Accepted |
| 0006 | Default-deny OPA policy — all cross-role calls blocked at policy layer | Accepted |
| 0007 | pgvector + Ollama embeddings for semantic memory search — no external API needed; dimension auto-detected at startup to survive model changes | Accepted |
| 0008 | Redis as read-through cache on `PostgresMemoryStore.read()` — write path goes to PostgreSQL only; Redis populated on first read miss, invalidated on write | Accepted |
| 0009 | Dolt as formula store — every `propose()` is a git commit; formula history is diffable with `dolt log`/`dolt diff`, consistent with audit log approach | Accepted |
| 0010 | TF-IDF keyword matching for `lookup()` instead of vector similarity — avoids a second embedding index on formulas; reliable for the current test suite and formula descriptions | Accepted |
