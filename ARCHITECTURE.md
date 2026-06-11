nothing # AI Harness — Architecture

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
  │  GatewayClient  ← fetches JWT from governance /oauth/token
  │
  │  POST /check  (Bearer <JWT>, tool_name)
  ▼
governance  :8090  (FastAPI)          — policy + audit sidecar [HARD]
  ├── validate JWT  (RS256, 15-min TTL)
  └── POST /v1/data/harness/allow  →  OPA  :8181  → 200 allowed / 403 denied
  │
  │  (on allow) POST /api/v0/tools/invoke  (direct, no auth)
  ▼
MCPJungle  :8080
  ├── git_diff_stub__git_diff    →  git-diff-stub  :9001
  └── linter_stub__run_linter    →  linter-stub    :9002
  │
  │  (async, fire-and-forget) POST /audit  →  governance
  ▼
Dolt  :3306  ← INSERT audit_log + CALL DOLT_COMMIT
```

Every tool call the agent makes produces:

1. An OPA policy decision (`allow` or `deny`)
2. A row in `audit_log` in Dolt (written async after the tool returns)
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
| `governance`     | local build                    | 8090 | OAuth token issuance (`/oauth/token`), OPA policy check (`/check`), async Dolt audit (`/audit`) |
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
  harness-gateway/   — GatewayClient + ContextForgeGatewayClient
  harness-agents/    — CodeReviewerAgent + AgentState TypedDict + output schema
  harness-memory/    — PostgresMemoryStore, DoltFormulaStore, ConsolidationWorker
  harness-supervisor/ — LangGraph supervisor orchestration, graph nodes, approval tokens
  harness-tests/     — pytest integration tests (77 tests across 5 test files + load test)

services/
  governance/        — OAuth 2.1 + OPA policy check + async Dolt audit + /metrics (rate limiting delegated to gateway)
  contextforge_setup/ — init script: registers MCP stubs with ContextForge, creates virtual server
  grafana/           — provisioned cost-per-role dashboard
  prometheus/        — scrape config for governance /metrics
  dolt/              — Dolt init: audit_log + formulas + formula_pours + seed data
  postgres/          — PostgreSQL init: enables pgvector extension
  review_server/     — FastMCP server wrapping CodeReviewerAgent

security/
  owasp-review.md    — OWASP Agentic AI Top 10 review

docs/runbooks/       — 4 operational runbooks
```

Dependencies: `harness-tests` → `harness-agents` → `harness-gateway`; `harness-memory`
is standalone (no dependency on other harness packages). See
[Package Dependency Direction](#package-dependency-direction) above for the enforced direction.

---

## Governance Service

`services/governance/server.py` — policy + audit sidecar (not a forwarding proxy). Three endpoints:

1. **`POST /oauth/token`** — client credentials grant. Three clients: `architect`, `code-reviewer`, `sre`. Issues **RS256 JWTs** with `sub`, `role`, 15-min TTL, signed with a private RSA key loaded from `JWT_PRIVATE_KEY_FILE`. Verifier uses the derived public key — downstream services cannot mint tokens.
2. **`GET /jwks`** — returns the RSA public key as a JWK set for downstream verifiers.
3. **`POST /check`** — validates Bearer JWT, calls OPA `POST /v1/data/harness/allow`; returns `{"allowed": true, "role": ..., "agent_id": ..., "rule": ...}` on allow, 403 on deny. Also gates `shell_exec` behind `X-Human-Approval-Token`.
4. **`POST /audit`** — accepts an audit record from GatewayClient and writes to Dolt async (202 response, background task). Emits Prometheus counters/histograms.

Governance no longer forwards tool calls. Rate limiting is delegated to the gateway (ContextForge).

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

- `gateway_url` — direct tool invocation endpoint (MCPJungle `:8080` or CF `:4444`)
- `governance_url` — policy+audit sidecar (governance `:8090`); optional
- `call_tool(name, params)` when `governance_url` is set:
  1. Fetches JWT from `governance_url/oauth/token`
  2. POSTs `governance_url/check` — 403 raises `ToolAccessDenied` immediately
  3. Calls gateway directly (`_invoke_mcpjungle` or `_invoke_cf`)
  4. Fires `governance_url/audit` as an async background task (non-blocking)
- `gateway_backend="contextforge"` enables CF's JSON-RPC format + CF JWT auth
- Legacy mode (no `governance_url`): gateway_url is treated as a proxy (backward-compatible)
- Response unwrapping: `_unwrap(data, status, tool_name)` — parses `{"content": [{"type": "text", "text": "<json>"}]}` to a plain dict; 401/403 raise `ToolAccessDenied`

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

- **[HARD]** Do not bypass the governance policy check. All agent tool calls must go through `GatewayClient` with `governance_url` set — `POST /check` is what enforces OPA policy before any tool executes.

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

## Test Coverage

### Integration suite (74 tests) — `make test-integration`

| Phase | File                        | Tests | What they cover                                                          |
|-------|-----------------------------|-------|--------------------------------------------------------------------------|
| 0     | `test_thin_slice.py`        | 9     | Reviewer agent contract, gateway audit log, tool access denial, MCP reachability |
| 1     | `test_phase1_governance.py` | 17    | Auth (RS256 JWT), OPA policy check (`/check`), Dolt audit (`/audit`), token expiry |
| 2     | `test_phase2_memory.py`     | 27    | Checkpointer, memory store (write/read/search/TTL/Redis), consolidation, formula store |
| 3     | `test_phase3_agents.py`     | 4     | Agent protocol compliance, tool calls via gateway, shell_exec gating    |
| 4     | `test_phase4_supervisor.py` | 12    | LangGraph orchestration (classify/route/formula/human_gate/checkpoint), E2E task flows |
| 5     | `test_phase5_hardening.py`  | 8     | OWASP mitigations, OTel cost tags, token budget, no-rate-limit on governance, CF parity |

### Eval suite (7 tests) — `pytest -m eval -v -s`

| File                       | Tests | What they cover |
|----------------------------|-------|-----------------|
| `test_eval_reviewer.py`    | 7     | CodeReviewerAgent quality: 6 per-fixture tests (verdict + recall) + 1 aggregate score report |

Eval tests use a mock gateway (no Docker stack needed) and hit Ollama directly. They are slow (~2 min for 7b) and are not part of `make test-integration`.

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
| 0011 | LangGraph StateGraph + conditional routing for multi-agent orchestration — native support for pause/resume, checkpoints, and conditional edges | Accepted |
| 0012 | Human approval tokens as short-lived JWTs scoped to (thread_id, tool_name) — enables fine-grained gating of shell_exec without per-tool governance refactors | Accepted |
| 0013 | AsyncPostgresSaver checkpointer with async pool — enables graph pause/resume across human approval interrupts; state survives service restart | Accepted |
| 0014 | ~~FakeEmbedder for unit tests~~ — superseded by ADR 0022; real nomic-embed-text embeddings now used in all Phase 2 tests | Superseded |
| 0015 | MockLLMProvider + SequentialMockLLMProvider for agent testing — deterministic responses replace real model calls; SequentialMockLLMProvider handles multi-turn flows (approve-required then approve-granted) | Accepted |
| 0016 | OTel spans emitted from all supervisor nodes — observability without coupling to logging infrastructure; enables distributed tracing of task classification → formula → agent execution | Accepted |
| 0017 | ContextForge as production MCP gateway (IBM `ghcr.io/ibm/mcp-context-forge`) — richer plugin ecosystem and multi-region federation vs MCPJungle free tier; GATEWAY_BACKEND feature flag enables zero-downtime migration and rollback | Accepted |
| 0018 | ~~Redis sliding-window rate limiter in governance~~ — superseded by ADR 0023; rate limiting delegated to gateway | Superseded |
| 0019 | Token budget via HarnessState fields (`tokens_used`, `token_budget`) — checked in run_agent_node before calling agent; graph exits with budget_exceeded error; no invasive changes to agent internals | Accepted |
| 0020 | Prometheus /metrics on governance + Grafana behind docker-compose monitoring profile — cost attribution per agent_role visible without external observability infra; not started by default to keep `docker compose up` lightweight | Accepted |
| 0021 | LLM-primary task classification with structured JSON output (`{"task_type": ...}`) — keyword-first routing misclassified tasks with misleading surface keywords; keywords demoted to fallback for LLM outage/unparseable output, final default `review` | Accepted |
| 0022 | `nomic-embed-text` (768 dims) as dedicated embedding model, separate from chat `OLLAMA_MODEL` — code LLMs produce 0.86–0.94 baseline similarity for all text, forcing a 0.95 cluster threshold and FakeEmbedder workaround; nomic-embed-text gives 0.82–0.93 for same-topic and 0.35–0.62 for different-topic, enabling a clean 0.80 threshold and real embeddings in all tests | Accepted |
| 0023 | Governance refactored from forwarding proxy to policy+audit sidecar — ContextForge natively handles auth, RBAC, and rate limiting, making governance's forwarding and Redis rate limiter redundant; governance retains OPA policy check (`/check`) and async Dolt audit (`/audit`) because those aren't replaceable by any gateway without custom plugins; GatewayClient calls gateway directly and fires audit as a background task | Accepted |
| 0024 | Governance JWT signing migrated from HS256 shared secret to RS256 asymmetric keypair — with HS256, any service that holds `JWT_SECRET` to verify tokens can also mint them (violates least privilege); RS256 gives governance a private key that never leaves the service and a public key exposed at `GET /jwks` for verifiers; the test key is committed under `test-fixtures/` with a startup fingerprint tripwire (`ENV != "test"` → refuses to start) so it is mechanically un-deployable to production | Accepted |
| 0025 | All LLM system prompts externalized to `prompts/*.md` — `classify.md` and `synthesise.md` were written but orphaned (nodes.py had an inline `_CLASSIFY_PROMPT` that diverged); consolidated so every prompt is a file: editable without code changes, diffable in git, and loadable by the eval suite | Accepted |
| 0026 | Eval suite (`eval-fixtures/` + `pytest -m eval`) for reviewer quality benchmarking — integration tests prove the harness works; eval proves the agent is good; 6 labeled diffs (clean + SQL injection, hardcoded secrets, shell injection, missing auth, path traversal); scored on verdict accuracy (≥80%) and recall of must-flag patterns (≥60%); mock gateway bypasses Docker stack so evals run against Ollama only | Accepted |
