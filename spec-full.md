
| AI HARNESS Incremental Build Specification with TDD *MCP Governance  ·  Persistent Memory  ·  Custom Agents  ·  Orchestration* |
| :---: |

Version 1.0  ·  May 2026

Stack: Python · LangGraph · MCPJungle / ContextForge · PostgreSQL / Redis · OpenTelemetry

# **Contents**

# **Executive Summary**

This specification describes the incremental, test-driven construction of an AI harness — a control and compute infrastructure that wraps LLM-powered agents with governance, memory, specialisation, and orchestration. The harness is not a product; it is the platform on which products are built.

The four pillars addressed are:

* MCP Governance — a gateway that enforces identity-scoped access to tools, with an immutable audit trail and OPA-driven policy.

* Persistent Memory — a two-layer memory architecture separating execution state (checkpointer) from knowledge (memory store), namespaced per agent skill.

* Custom Agents — three production-ready specialists: Architect, Code Reviewer, and SRE / Incident Responder, each with scoped tools, a distinct system prompt, and its own memory namespace.

* Agent Orchestration — a LangGraph supervisor graph that routes tasks to specialists, synthesises outputs, and supports human-in-the-loop pause/resume for high-risk actions.

The build is structured into six phases (0–5). Each phase is gated by a passing test suite before implementation begins — red first, then green, then refactor. No phase begins until the previous phase's acceptance criteria are met.

| TDD Contract Every phase defines its tests before a single line of implementation is written. A phase is 'done' when all tests pass and the Definition of Done checklist is complete — not before. |
| :---- |

# **Architecture Overview**

## **System Topology**

The harness separates into two planes that communicate only through defined interfaces:

| Component | Responsibility |
| :---- | :---- |
| **Control Plane** | Auth, routing, policy enforcement, audit, orchestration, cost controls. Runs the MCP Gateway and the LangGraph Supervisor. |
| **Compute Plane** | Sandboxed model execution, tool calls, file/shell access. Runs individual agent nodes inside the LangGraph state machine. |
| **MCP Gateway** | Sits between all agents and all downstream MCP servers. Every tool call passes through it. Enforces OAuth 2.1 scopes, OPA policy, and emits OTel spans. |
| **Memory Layer** | Three layers: (1) PostgreSQL checkpointer for execution state, (2) PostgreSQL \+ Redis memory store for agent knowledge, (3) Dolt formula store for versioned workflow templates. |
| **Formula Store** | Dolt table of versioned, declarative workflow templates. The supervisor looks up a matching formula before routing a task; if found, it pours (instantiates) the formula rather than running ad-hoc. Every formula mutation is a git commit. |
| **Agents** | Architect, Code Reviewer, SRE — each a LangGraph node with a scoped system prompt, tool access list, memory namespace, and a library of formulas it can execute. |
| **Orchestrator** | LangGraph Supervisor node. Classifies tasks, looks up a matching formula, pours it if found, dispatches to the correct specialist, and synthesises a final response. |

## **Protocol Standards**

The harness is built on two AAIF-governed protocols (Linux Foundation Agentic AI Foundation, December 2025):

| Protocol | Role in Harness |
| :---- | :---- |
| **MCP (agent-to-tool)** | All tool connections go through MCP servers. Governed by the gateway. OAuth 2.1 \+ PKCE mandatory per 2025-11-25 spec. RFC 8707 resource indicators scope tokens per server. |
| **A2A (agent-to-agent)** | Reserved for future cross-framework federation. Not used in Phase 1–3; wired in Phase 4 if inter-team agent calls are needed. |

## **Self-Learning Loop**

The harness is not static. Every task run is an opportunity to improve future runs. The self-learning loop operates at three speeds:

| Loop Speed | Behaviour |
| :---- | :---- |
| **Immediate (per-task)** | Each agent writes an episodic memory item immediately after task completion — a structured record of what the task was, what tools were called, what was found, and the agent's confidence in the outcome. If the task exposed a novel pattern not covered by any existing formula, the agent's output reaches the propose\_formula node and a draft formula is created in Dolt for human review. |
| **Nightly (consolidation)** | The ConsolidationWorker runs on a schedule. It groups episodic memories by semantic similarity (pgvector cosine distance), summarises each cluster into a semantic memory using a lightweight LLM call, marks the source episodes as 'consolidated', and prunes low-confidence items that have expired. Semantic memories are the durable, compressed knowledge base agents draw on in future tasks. |
| **Formula quality loop** | Every formula pour records an outcome (success/failure, duration\_ms, agent\_confidence\_score). The ConsolidationWorker aggregates these: formulas with ≥80% success over 10+ pours graduate from 'active' to 'proven'; formulas below 30% are flagged 'review'. A daily Dolt commit captures the scoring pass — the whole quality history is queryable with dolt log. |

| Memory Type | Description |
| :---- | :---- |
| **Episodic memory** | Raw task outcomes. Written immediately post-task. Scoped to agent namespace. Expires after N days (configurable). Input to consolidation. |
| **Semantic memory** | Distilled facts, patterns, and heuristics. Written by ConsolidationWorker. Longer TTL. What agents actually read on task start. |
| **Procedural memory** | Formulas in Dolt. Versioned, human-approved workflow templates. Updated by quality loop. The harness's learned repertoire of how to do recurring work. |

## **C4 Architecture Diagrams**

The four diagrams below use the C4 model (Context, Container, Component). External systems are shown in dark grey; internal containers in blue; databases in darker blue; workers in green.

### **C1 — System Context**

| *\[Person\]* Engineer / Developer Submits tasks. Views results. Approves high-risk SRE actions via the human gate. | → *submits tasks* | *\[System\]* AI Harness Governs tool access, orchestrates specialist agents, persists memory, runs the self-learning and formula quality loops. | → *calls tools via MCP* | *\[External System\]* MCP Servers Git, linter, observability API, runbook store, shell sandbox. All governed by the MCP gateway. |
| :---- | ----- | :---- | :---: | :---- |
|  |  |  | → *LLM inference* | *\[External System\]* **LLM Provider** *Claude / GPT-4o* Inference only. No harness data stored by provider. Model-agnostic — different nodes may use different models. |
|  |  |  | → *OTel spans* | *\[External System\]* **Observability Stack** *Grafana / Phoenix / OTLP* Receives OpenTelemetry spans from all harness components. Dashboards for cost, latency, error rate, active threads. |

*C1 — System Context: The AI Harness and its external relationships*

### **C2 — Container Diagram**

The harness boundary contains six containers. PostgreSQL and Redis are in the compute plane; Dolt, the MCP Gateway, LangGraph Orchestrator, and Consolidation Worker form the control infrastructure.

|   *AI Harness — System Boundary* *\[Container\]* MCP Gateway *MCPJungle → ContextForge* OAuth 2.1 identity, OPA policy enforcement, tool-group scoping, audit log emission. → *tool calls* *\[Container\]* LangGraph Orchestrator *Python / LangGraph 1.0* Supervisor graph: classify → formula\_lookup → route → specialist → synthesise. Manages state and human-in-the-loop. → *triggers nightly* *\[Container\]* Consolidation Worker *Python / APScheduler* Nightly: groups episodic memories, distils semantic memories, scores formula quality, prunes expired items. ↓ *reads/writes* ↓ *reads/writes* ↓ *reads/writes* *\[Database\]* PostgreSQL *LangGraph PostgresSaver \+ pgvector* Execution state checkpoints. Agent memory store (episodic \+ semantic). pgvector for semantic search. ↕ *hot cache* *\[Database\]* Redis *Redis 7* Hot read cache for active agent context. Sub-millisecond reads on in-flight conversations. *\[Database\]* Dolt *git-versioned MySQL* Audit log (every tool call \= a commit). Formula store (versioned workflow templates).  |
| ----- |

*C2 — Container Diagram: Deployable units within the AI Harness boundary*

### **C3 — Orchestrator Components**

The LangGraph Orchestrator is a StateGraph whose nodes are shown left-to-right in execution order. Dashed paths are conditional.

| *\[Component\]* classify *LLM prompt* Determines task\_type: design, review, or incident. | → *type* | *\[Component\]* formula\_lookup *Dolt query* Matches task to a stored formula. Sets formula\_id or null. | → *formula / null* | *\[Component\]* route *conditional edge* Dispatches to architect, code\_reviewer, or sre node. | → *dispatch* | *\[Component\]* specialist node *architect / reviewer / sre* Executes formula steps (if formula\_id set) or reasons ad-hoc. Calls tools via MCP gateway. | → *output* | *\[Component\]* synthesise *LLM prompt* Formats structured output. Records formula outcome to Dolt. May route to propose\_formula. |
| :---- | :---: | :---- | :---: | :---- | :---: | :---- | :---: | :---- |

| *\[Component\]* human\_gate *LangGraph interrupt* Pauses graph when requires\_human\_approval=true. Resumes on valid signed JWT from harness API. | → *approved* | *\[Component\]* propose\_formula *Dolt insert (draft)* Creates draft formula from ad-hoc run steps. Pending human review before activation. | → *error* | *\[Component\]* error\_handler *OTel \+ state* Catches tool failures, gateway 403s, LLM errors. Emits error span and returns structured error state. |  |
| :---- | :---: | :---- | :---: | :---- | :---- |

*C3 — Orchestrator Components: LangGraph node flow (top row \= happy path; bottom row \= branches)*

### **C3 — Memory & Self-Learning Components**

Three layers plus the Consolidation Worker form a closed learning loop. Arrows show the flow of information across the learning cycle.

| *\[Component (Layer 3)\]* Formula Store *Dolt — git-versioned* Active \+ draft formulas. Every write \= a commit. Outcome records per pour. Quality scoring by Consolidation Worker. | ↕ *quality scoresformula updates* | *\[Component\]* Consolidation Worker *Python / APScheduler* Nightly: clusters episodic memories → semantic memories. Scores formula outcomes. Prunes expired items. Each pass \= a Dolt commit. |  |
| ----- | ----- | ----- | :---- |
| ↓ *propose / lookup* |  | ↓ *reads episodicwrites semantic* |  |
| *\[Component (Layer 2)\]* **Memory Store** *PostgreSQL \+ pgvector* Episodic memories (raw, short TTL). Semantic memories (consolidated, long TTL). Namespaced per agent. Semantic search via pgvector. | ↕ *hot reads* | *\[Component\]* **Redis Cache** *Redis 7* Hot read cache for active agent namespace. Invalidated on memory store write. Reduces PostgreSQL load on in-flight tasks. |  |
| ↓ *saves / resumesexecution state* |  |  |  |
| *\[Component (Layer 1)\]* **Checkpointer** *LangGraph PostgresSaver* Full graph state saved at every super-step. Scoped to thread\_id. Enables fault-tolerant resumption and human gate pause/resume. |  |  |  |

*C3 — Memory & Self-Learning: Three-layer memory model with consolidation loop*

## **Tech Stack Decisions**

| Component | Role | Rationale |
| :---- | :---- | :---- |
| **LangGraph 1.0** | Orchestration | Graph-based state machine. First-class checkpointing, human-in-the-loop, and conditional routing. Production-proven at LinkedIn/Uber. |
| **MCPJungle** | MCP Gateway (Phase 1–2) | Lightweight self-hosted, Docker Compose, RBAC, tool groups, OTel. Fast to wire up and validate the governance model. |
| **ContextForge (IBM)** | MCP Gateway (Phase 3+) | Migration target once multi-region federation, plugin ecosystem, and advanced audit are needed. |
| **PostgreSQL** | Checkpointer \+ memory store | Durable, strongly consistent. LangGraph PostgresSaver for execution state. Also backs the agent knowledge memory store with pgvector for semantic search. |
| **Dolt** | Audit log \+ formula store | Git-versioned MySQL-compatible database. Every audit record and formula write is a commit — full causal history queryable with git diff, git log, and SQL. Forensics and compliance story without extra tooling. |
| **Redis** | Hot memory reads | Sub-millisecond reads for active agent context. Hot path for in-flight conversations. |
| **OPA / Rego** | Policy engine | Gateway policy evaluated per tool call. Declarative, auditable, version-controlled alongside code. |
| **OpenTelemetry** | Observability | 2026 standard. Every tool call, agent step, and policy decision emits a span. Exporter: OTLP to Grafana/Phoenix. |
| **pytest \+ LangSmith** | Test & trace | pytest for unit/integration. LangSmith for graph execution traces in CI. |

# **TDD Philosophy & Test Strategy**

## **Red → Green → Refactor**

Each phase begins with a failing test suite. Implementation is written only to make tests pass. Refactoring happens only when tests are green. No exceptions.

| Test Type | Scope & Rules |
| :---- | :---- |
| **Unit tests** | Test a single function, node, or class in isolation. All dependencies mocked. Run in \<1s each. Must cover the happy path, all error branches, and boundary conditions. |
| **Integration tests** | Test two or more real components wired together (e.g., agent node \+ memory store, gateway \+ OPA policy). Run against real services in Docker Compose. |
| **Contract tests** | Test MCP server interface compliance — that a given server honours the tool schema it advertises. Run on every MCP server added to the registry. |
| **End-to-end tests** | Drive the full graph from task input to synthesised output. Slow; run in CI on merge to main only. Use recorded LLM responses (cassettes) to avoid flakiness. |

## **Test Infrastructure**

Set up the test infrastructure in Phase 0 and do not modify it mid-build. Adding new fixtures or helpers is acceptable; changing the runner or assertion library is not.

* pytest with pytest-asyncio for all async LangGraph nodes.

* Docker Compose test stack: PostgreSQL, Redis, MCPJungle, OPA sidecar.

* Testcontainers-python to spin up and tear down the stack per integration test suite.

* LangSmith CI integration: graph traces uploaded on every test run, tagged with branch and commit.

* Coverage gate: 80% line coverage minimum on all harness modules. Agents exempt from the coverage gate (prompt logic is tested via E2E, not line coverage).

| What We Don't Test LLM responses are not deterministic and cannot be unit-tested. What we test is the harness behaviour around the model: routing decisions, memory reads/writes, policy enforcement, audit emission, and error handling. Agent 'quality' is evaluated separately via evals, not in the TDD suite. |
| :---- |

| PHASE 0 | Foundation & Test Infrastructure Week 1 · No model calls · No tool calls |
| :---: | :---- |

## **Objective**

Establish the project skeleton, CI pipeline, test harness, and Docker Compose dev stack. No agent logic. No LLM calls. The output of Phase 0 is a repo where every subsequent phase can drop in a test file and run it immediately.

## **Deliverables**

* Mono-repo structure with packages: harness-gateway, harness-memory, harness-agents, harness-orchestrator, harness-tests.

* Docker Compose stack: PostgreSQL 16, Redis 7, MCPJungle latest, OPA latest, Dolt latest.

* pytest configuration with asyncio mode, coverage reporting, and LangSmith export.

* GitHub Actions CI: lint (ruff), type-check (mypy), unit tests, integration tests (Docker), coverage gate.

* Makefile targets: make test-unit, make test-integration, make test-e2e, make stack-up, make stack-down.

## **Test Suite (write first)**

| Test Name | Type | Asserts |
| :---- | :---- | :---- |
| `test_postgres_connection` | **Integration** | PostgreSQL is reachable and returns version string. |
| `test_redis_connection` | **Integration** | Redis PING returns PONG. |
| `test_mcpjungle_health` | **Integration** | MCPJungle /health endpoint returns 200\. |
| `test_opa_policy_load` | **Integration** | OPA /v1/data returns 200 after loading base policy bundle. |
| `test_coverage_gate` | **Unit** | Coverage report file exists and overall % \>= 80\. |
| `test_ci_env_vars_present` | **Unit** | Required env vars (LANGSMITH\_API\_KEY, PG\_DSN, REDIS\_URL, DOLT\_DSN) are set in CI env. |

## **Definition of Done**

1. All 6 tests above pass in CI.

2. make stack-up brings all four services to healthy within 60 seconds.

3. A developer can clone the repo, run make stack-up && make test-integration, and see green output without manual steps.

4. README documents the local dev setup in under 10 steps.

| PHASE 1 | MCP Gateway & Governance Weeks 2–3 · No model calls · Tool calls governed |
| :---: | :---- |

## **Objective**

Deploy MCPJungle as the MCP gateway. Implement agent identity (OAuth 2.1 client credentials), tool group scoping, OPA-driven policy, and audit log emission. By end of phase, every tool call is authenticated, authorised, and auditable — even though no real agents are calling yet.

## **Key Design Decisions**

| Decision | Detail |
| :---- | :---- |
| **Identity model** | Each agent role (architect, code-reviewer, sre) is an OAuth 2.1 client with its own client\_id. Client credentials grant type (RFC 6749 §4.4). No shared service account. |
| **Tool groups** | MCPJungle tool groups map 1:1 to agent roles. architect group: codebase-search, adr-store, diagram-gen. code-reviewer group: git-diff, linter, coverage. sre group: observability-api, runbook-store, shell-sandbox. |
| **Policy engine** | OPA sidecar. Policy bundle loaded from /policies repo directory. Gateway calls OPA /v1/data/harness/allow before forwarding any tool call. Sub-millisecond target. |
| **Audit log** | Dolt table: agent\_id, tool\_name, server\_id, request\_hash, response\_hash, policy\_decision, policy\_rule, timestamp, latency\_ms. Every INSERT is a Dolt commit — full git history of all tool calls. No deletes permitted. Roll back a policy mistake with git revert; walk the causal chain with git log. |
| **Token TTL** | Access tokens expire in 15 minutes. No refresh tokens — agents re-authenticate. Short TTL limits blast radius of a compromised token. |

## **OPA Policy Skeleton**

The base policy lives at policies/harness.rego. Write this before the gateway tests.

`package harness`

`default allow = false`

`allow {`

    `input.agent_role == "architect"`

    `input.tool_name in {"codebase_search","adr_read","adr_write","diagram_gen"}`

`}`

`allow {`

    `input.agent_role == "code_reviewer"`

    `input.tool_name in {"git_diff","run_linter","coverage_report","repo_conventions_read"}`

`}`

`allow {`

    `input.agent_role == "sre"`

    `input.tool_name in {"observability_query","runbook_read","log_search","shell_exec"}`

`}`

## **Test Suite (write first)**

| Test Name | Type | Asserts |
| :---- | :---- | :---- |
| `test_architect_client_auth` | **Integration** | OAuth client\_credentials grant returns access token for architect client. |
| `test_reviewer_client_auth` | **Integration** | OAuth client\_credentials grant returns access token for code\_reviewer client. |
| `test_sre_client_auth` | **Integration** | OAuth client\_credentials grant returns access token for sre client. |
| `test_architect_allowed_tool` | **Integration** | Architect token can call codebase\_search via gateway. Returns 200\. |
| `test_architect_denied_tool` | **Integration** | Architect token calling shell\_exec returns 403 Forbidden. |
| `test_reviewer_allowed_tool` | **Integration** | code\_reviewer token can call git\_diff via gateway. Returns 200\. |
| `test_reviewer_denied_tool` | **Integration** | code\_reviewer token calling adr\_write returns 403 Forbidden. |
| `test_sre_allowed_tool` | **Integration** | sre token can call runbook\_read via gateway. Returns 200\. |
| `test_unknown_token_rejected` | **Integration** | Request with invalid bearer token returns 401 Unauthorized. |
| `test_audit_row_written` | **Integration** | After any tool call (allowed or denied), a row appears in audit\_log with correct agent\_id and tool\_name. |
| `test_audit_policy_rule_recorded` | **Integration** | audit\_log row records which OPA rule permitted or denied the call. |
| `test_audit_dolt_commit_created` | **Integration** | After an audit INSERT, dolt log shows a new commit whose message references the tool call and agent\_id. |
| `test_audit_dolt_history_queryable` | **Integration** | dolt diff between two commits shows exactly the rows written between those two tool calls. |
| `test_audit_no_delete` | **Integration** | Attempt to DELETE from audit\_log table is rejected (Dolt branch write policy enforced at connection level). |
| `test_opa_allow_architect_tool` | **Unit** | OPA evaluates harness/allow=true for {agent\_role:architect, tool\_name:codebase\_search}. |
| `test_opa_deny_cross_role` | **Unit** | OPA evaluates harness/allow=false for {agent\_role:architect, tool\_name:shell\_exec}. |
| `test_token_expiry` | **Integration** | Token issued with 15-min TTL. Gateway rejects the token after TTL is mocked to expire. |

## **Definition of Done**

5. All 17 tests above pass in CI.

6. A simulated tool call from each agent role produces an audit log row and a Dolt commit within 200ms.

7. OPA policy file is version-controlled and loaded from the repo, not hardcoded in gateway config.

8. No tool call can bypass the gateway (verified by network policy in Docker Compose: agents can only reach MCPJungle, not MCP servers directly).

9. dolt log on the audit database shows one commit per tool call with a human-readable message.

10. Phase 2 can begin without modifying the gateway or policy engine.

| PHASE 2 | Persistent Memory Layer Weeks 4–5 · No model calls · Memory API proven |
| :---: | :---- |

## **Objective**

Implement the three-layer memory architecture. The checkpointer (PostgreSQL-backed) saves LangGraph execution state per thread. The memory store (PostgreSQL \+ Redis) persists cross-thread, cross-session agent knowledge. The formula store (Dolt) holds versioned, declarative workflow templates that the supervisor pours when a matching task arrives. By end of phase, all three layers are tested in isolation — no agents yet.

## **Three-Layer Model**

| Layer | Responsibility |
| :---- | :---- |
| **Layer 1: Checkpointer** | Saves the full LangGraph state dict at every super-step. Backend: LangGraph PostgresSaver. Enables fault-tolerant resumption, time-travel debugging, and human-in-the-loop pause/resume. Scoped to a thread\_id. Not where knowledge lives. |
| **Layer 2: Memory Store** | Stores facts, summaries, and learned context that survive across threads and sessions. Backend: PostgreSQL (durable) \+ Redis (hot reads). Namespaced per agent: architect/, code\_reviewer/, sre/. Items have a TTL and a confidence score. |
| **Layer 3: Formula Store** | Stores versioned, declarative workflow templates in Dolt. A formula defines a named, repeatable unit of work — its agent\_role, input schema, step sequence, and expected output contract. Every formula change is a git commit. Agents can propose new formulas; humans approve them via a PR-style review before they enter the store. |

## **Memory Store Schema (PostgreSQL)**

The schema distinguishes episodic (raw, short-lived) from semantic (consolidated, long-lived) memory items. The ConsolidationWorker reads episodic items and writes semantic items; agents primarily read semantic items on task start and write episodic items on task end.

`CREATE TABLE memory_items (`

    `id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),`

    `namespace     TEXT NOT NULL,         -- e.g. 'architect', 'sre'`

    `key           TEXT NOT NULL,         -- e.g. 'adr:auth-middleware'`

    `memory_type   TEXT NOT NULL DEFAULT 'episodic',  -- 'episodic' | 'semantic'`

    `value         JSONB NOT NULL,`

    `source_ids    UUID[],                -- episodic items that produced this semantic item`

    `embedding     VECTOR(1536),          -- for semantic search (pgvector)`

    `confidence    FLOAT DEFAULT 1.0,`

    `consolidated  BOOL DEFAULT FALSE,    -- TRUE once absorbed into a semantic item`

    `created_at    TIMESTAMPTZ DEFAULT now(),`

    `expires_at    TIMESTAMPTZ,`

    `UNIQUE (namespace, key)`

`);`

## **Formula Store Schema (Dolt)**

Stored in Dolt so every CREATE, UPDATE, and DEPRECATE is a git commit. Formula history is queryable with dolt log and dolt diff — you can see exactly when a workflow changed and why.

`CREATE TABLE formulas (`

    `id           VARCHAR(64) PRIMARY KEY,  -- slug e.g. 'sre:triage-incident'`

    `name         TEXT NOT NULL,`

    `agent_role   TEXT NOT NULL,            -- 'architect'|'code_reviewer'|'sre'`

    `version      INTEGER NOT NULL DEFAULT 1,`

    `status       TEXT NOT NULL DEFAULT 'active', -- 'draft'|'active'|'deprecated'`

    `description  TEXT,`

    `input_schema JSONB NOT NULL,           -- JSON Schema for expected inputs`

    `steps        JSONB NOT NULL,           -- ordered list of step descriptors`

    `output_contract JSONB NOT NULL,        -- JSON Schema for expected output`

    `created_at   DATETIME NOT NULL,`

    `created_by   TEXT NOT NULL,            -- agent_id or human username`

    `UNIQUE (id, version)`

`);`

Example formula (stored as a row, referenced by id):

`id:           'sre:triage-incident'`

`name:         'Triage Incident'`

`agent_role:   'sre'`

`steps:        [`

  `{ action: 'observability_query', params: { lookback: '1h' } },`

  `{ action: 'log_search',          params: { severity: 'ERROR' } },`

  `{ action: 'runbook_read',        params: { match_by: 'error_signature' } },`

  `{ action: 'llm_synthesise',      params: { output: 'incident_report' } },`

`]`

## **Interfaces**

Define both interfaces before implementing. All agents and the supervisor program against these, not the storage backends.

`class MemoryStore(Protocol):`

    `async def write(self, namespace: str, key: str, value: dict,`

                    `ttl_hours: int | None = None) -> None: ...`

    `async def read(self, namespace: str, key: str) -> dict | None: ...`

    `async def search(self, namespace: str, query: str, top_k: int = 5) -> list[dict]: ...`

    `async def delete(self, namespace: str, key: str) -> None: ...`

`class ConsolidationWorker(Protocol):`

    `async def run_pass(self, namespace: str) -> ConsolidationResult: ...`

    `# Clusters unconsolidated episodic items by semantic similarity,`

    `# summarises each cluster into a semantic item via LLM,`

    `# marks source episodes as consolidated=True,`

    `# prunes expired items, and scores formula outcomes.`

`class FormulaStore(Protocol):`

    `async def lookup(self, agent_role: str, task: str) -> Formula | None: ...`

    `async def get(self, formula_id: str) -> Formula | None: ...`

    `async def propose(self, formula: Formula) -> str: ...   # returns commit hash`

    `async def list_active(self, agent_role: str) -> list[Formula]: ...`

## **Test Suite (write first)**

| Test Name | Type | Asserts |
| :---- | :---- | :---- |
| `test_checkpointer_saves_state` | **Integration** | After a graph step, PostgresSaver checkpoint exists for thread\_id. |
| `test_checkpointer_resumes` | **Integration** | Graph resumed from checkpoint continues from last saved step, not from start. |
| `test_checkpointer_thread_isolation` | **Integration** | Checkpoint for thread\_A is not visible when loading thread\_B. |
| `test_memory_write_and_read` | **Integration** | write() stores item; read() returns same item within same session. |
| `test_memory_namespace_isolation` | **Integration** | Item written to architect/ namespace is not returned by read() against sre/ namespace. |
| `test_memory_cross_session_persistence` | **Integration** | Item written in session 1 is readable in session 2 (new DB connection). |
| `test_memory_ttl_expiry` | **Integration** | Item with expires\_at in the past is not returned by read(). |
| `test_memory_redis_hot_read` | **Integration** | Second read of same key is served from Redis (verified via cache hit counter). |
| `test_memory_semantic_search` | **Integration** | search() returns items semantically related to query, ordered by relevance. |
| `test_memory_overwrite` | **Integration** | write() with same namespace+key overwrites the previous value. |
| `test_memory_delete` | **Integration** | delete() removes item; subsequent read() returns None. |
| `test_memory_interface_compliance` | **Unit** | PostgresMemoryStore satisfies MemoryStore Protocol (mypy structural check). |
| `test_sre_runbook_namespace` | **Integration** | SRE agent can write and read from sre/ without touching architect/ or code\_reviewer/. |
| `test_episodic_memory_write` | **Integration** | Agent post-task write with memory\_type='episodic' stores item with consolidated=False. |
| `test_semantic_memory_written_by_consolidation` | **Integration** | After consolidation run\_pass(), semantic items exist for the namespace and source episodic items have consolidated=True. |
| `test_consolidation_clusters_similar_episodes` | **Integration** | Two episodic items with high cosine similarity are merged into one semantic item by run\_pass(). |
| `test_consolidation_preserves_distinct_episodes` | **Integration** | Two episodic items with low cosine similarity produce two separate semantic items. |
| `test_consolidation_prunes_expired_items` | **Integration** | Expired episodic items (expires\_at \< now) are deleted by run\_pass(); non-expired items remain. |
| `test_formula_quality_score_updated` | **Integration** | After run\_pass(), formula with 8/10 successful pours has quality\_score \>= 0.8 in Dolt. |
| `test_formula_graduates_to_proven` | **Integration** | Formula with \>=10 pours and \>=80% success has status='proven' after consolidation. |
| `test_formula_flagged_for_review` | **Integration** | Formula with \>=10 pours and \<30% success has status='review' after consolidation. |
| `test_formula_write_creates_dolt_commit` | **Integration** | propose() inserts a formula row and dolt log shows a new commit with the formula id in the message. |
| `test_formula_lookup_by_task` | **Integration** | lookup('sre', 'DB latency alert fired') returns the sre:triage-incident formula. |
| `test_formula_lookup_no_match` | **Integration** | lookup() returns None for a task with no matching formula — does not error. |
| `test_formula_version_history` | **Integration** | After two propose() calls with the same id, dolt log shows two commits and both versions are queryable. |
| `test_formula_deprecate` | **Integration** | Deprecated formula is not returned by list\_active() or lookup(). |
| `test_formula_interface_compliance` | **Unit** | DoltFormulaStore satisfies FormulaStore Protocol (mypy structural check). |

## **Definition of Done**

11. All 27 tests above pass in CI.

12. Memory reads from Redis (hot path) complete in \<5ms p99 under load test of 100 concurrent reads.

13. A checkpoint survives a PostgreSQL restart (data persists to disk, not just in-memory).

14. pgvector extension enabled; semantic search returns non-empty results for a test query.

15. Formula store Dolt database has at least three seed formulas committed: sre:triage-incident, code\_reviewer:review-pr, architect:write-adr.

16. Memory store schema versioned with Alembic; formula store schema versioned as Dolt commits.

17. ConsolidationWorker can be triggered manually via make consolidate and runs to completion without errors on an empty namespace.

18. After seeding 5 episodic items into a namespace, a consolidation pass produces at least 1 semantic item and marks source episodes as consolidated=True.

| PHASE 3 | Specialised Agent Nodes Weeks 6–9 · LLM calls begin · Agents tested in isolation |
| :---: | :---- |

## **Objective**

Implement the three specialist agent nodes as LangGraph graph definitions. Each agent is tested in isolation — driven by a fixed input state, with LLM responses cassette-recorded to eliminate flakiness. The supervisor graph (Phase 4\) is not wired yet; each agent is exercised directly.

| Cassette Recording Use pytest-recording (vcrpy wrapper) to record real LLM responses on first run and replay them on subsequent CI runs. Tag tests requiring a live LLM call with @pytest.mark.live so they can be excluded from fast CI and run nightly. |
| :---- |

## **Agent Node Contract**

Every agent node must satisfy the same interface so the supervisor can route to any of them uniformly:

`class AgentNode(Protocol):`

    `name: str                              # 'architect' | 'code_reviewer' | 'sre'`

    `allowed_tools: list[str]               # MCP tool names this agent may call`

    `memory_namespace: str                  # memory store namespace`

    `async def run(self, state: AgentState) -> AgentState:`

        `# Reads task from state.task`

        `# Reads from memory store`

        `# Calls tools via MCP gateway`

        `# Writes learned context back to memory store`

        `# Returns updated state with output`

        `...`

## **Architect Agent**

* **Produce structured Architectural Decision Records (ADRs) and system design proposals.** Responsibility:

* **codebase\_search, adr\_read, adr\_write, diagram\_gen, web\_research** Tools:

* **Past ADRs, system topology, tech radar entries.** Memory reads:

* **New ADR after each completed design task.** Memory writes:

* **Structured dict: {title, status, context, decision, consequences, alternatives\_considered}** Output contract:

## **Code Reviewer Agent**

* **Review code diffs, surface findings by severity, return a pass/fail verdict.** Responsibility:

* **git\_diff, run\_linter, coverage\_report, repo\_conventions\_read** Tools:

* **Repo conventions, past finding patterns, known false positives.** Memory reads:

* **New finding patterns after novel issues. Updated false-positive list.** Memory writes:

* **Structured dict: {verdict, findings: \[{severity, file, line, message, suggestion}\], summary}** Output contract:

* **May iterate on the diff up to 3 times before declaring a verdict (configurable).** Loop behaviour:

## **SRE / Incident Responder Agent**

* **Diagnose alerts, propose remediation, execute approved steps.** Responsibility:

* **observability\_query, log\_search, runbook\_read, shell\_exec (human-approved only)** Tools:

* **Incident history, known failure signatures, remediation playbooks.** Memory reads:

* **Incident summary and root cause after resolution.** Memory writes:

* **Structured dict: {timeline, likely\_cause, severity, recommended\_steps, runbook\_ref, requires\_human\_approval: bool}** Output contract:

* **shell\_exec tool calls are blocked at the gateway unless the current thread has a human\_approval\_token in state. The agent must surface this requirement, not bypass it.** CRITICAL:

## **Test Suite (write first — all three agents)**

| Test Name | Type | Asserts |
| :---- | :---- | :---- |
| `test_architect_produces_adr` | **Unit** | Given a feature request input state, architect node returns a dict matching the ADR output contract. |
| `test_architect_reads_past_adrs` | **Unit** | Architect reads from memory namespace before generating output (verified by mock call count). |
| `test_architect_writes_adr_to_memory` | **Unit** | After run(), memory store contains a new entry under architect/ namespace. |
| `test_architect_tool_calls_go_via_gateway` | **Integration** | Architect's codebase\_search call is visible in gateway audit log. |
| `test_architect_denied_shell_exec` | **Integration** | Architect node raises ToolAccessDenied if it attempts shell\_exec (gateway 403). |
| `test_reviewer_produces_structured_findings` | **Unit** | Given a diff input state, reviewer returns findings list with severity, file, line, message. |
| `test_reviewer_verdict_fail_on_critical` | **Unit** | If any finding has severity=CRITICAL, verdict is 'fail'. |
| `test_reviewer_loop_max_iterations` | **Unit** | Reviewer loop terminates after 3 iterations even if not satisfied. |
| `test_reviewer_reads_conventions` | **Unit** | Reviewer reads repo\_conventions from memory before first LLM call. |
| `test_sre_produces_incident_report` | **Unit** | Given an alert input state, SRE node returns dict matching incident output contract. |
| `test_sre_shell_exec_blocked_without_approval` | **Integration** | SRE node attempting shell\_exec without human\_approval\_token receives 403 from gateway. |
| `test_sre_shell_exec_allowed_with_approval` | **Integration** | SRE node with valid human\_approval\_token in state can call shell\_exec via gateway. |
| `test_sre_writes_incident_to_memory` | **Unit** | After resolving an incident, memory store contains incident summary under sre/ namespace. |
| `test_agent_node_contract_compliance` | **Unit** | All three agent classes satisfy AgentNode Protocol (mypy structural check). |

## **Definition of Done**

19. All 14 tests above pass in CI (cassette-recorded).

20. Each agent's output passes JSON Schema validation against its output contract.

21. No agent can call a tool outside its allowed\_tools list (enforced by gateway, verified by integration test).

22. SRE shell\_exec cannot fire without a human\_approval\_token — this is a hard gateway policy rule, not a soft agent-side check.

23. Memory writes from each agent are visible in a subsequent session (persistence verified).

| PHASE 4 | Agent Orchestration Weeks 10–12 · Full graph · Supervisor wired |
| :---: | :---- |

## **Objective**

Wire the three agent nodes into a LangGraph supervisor graph. The supervisor classifies incoming tasks, routes to the correct specialist, handles human-in-the-loop interrupts for high-risk actions, and synthesises a final response. End-to-end tests drive the full graph.

## **Supervisor Graph Design**

The supervisor is a LangGraph StateGraph with the following nodes:

* classify — determines task type (design, review, incident) from input. Uses a lightweight classifier prompt, not a full agent.

* formula\_lookup — queries the Dolt formula store for a matching formula given the task type and task text. If found, pours it (instantiates a formula\_instance\_id and binds inputs). If not found, proceeds ad-hoc and sets formula\_id=None.

* route — conditional edge that dispatches to architect, code\_reviewer, or sre based on classify output.

* architect / code\_reviewer / sre — the specialist nodes from Phase 3\. When a formula\_id is present in state, the agent executes the formula steps in order rather than reasoning from scratch.

* human\_gate — interrupt node invoked when the active agent sets requires\_human\_approval=true. Graph pauses; resumes only when human\_approval\_token is injected via the API.

* synthesise — final node that wraps the specialist's structured output into a human-readable response. If a formula was used, records the outcome back to the formula store (success/failure, duration) for future formula quality scoring.

* propose\_formula — optional terminal node reached when an ad-hoc run produces a good result. Agent proposes a new formula based on the steps it took; stored in Dolt as a draft pending human review.

* error\_handler — catches tool failures, gateway 403s, and LLM errors. Emits an OTel error span and returns a structured error state.

## **State Schema**

`class HarnessState(TypedDict):`

    `task:                    str`

    `task_type:               str | None   # 'design' | 'review' | 'incident'`

    `formula_id:              str | None   # matched formula, e.g. 'sre:triage-incident'`

    `formula_instance_id:     str | None   # unique ID for this pour of the formula`

    `active_agent:            str | None`

    `agent_output:            dict | None`

    `final_response:          str | None`

    `human_approval_token:    str | None   # injected by human gate`

    `requires_human_approval: bool`

    `error:                   dict | None`

    `thread_id:               str          # for checkpointer`

## **Human-in-the-Loop Flow**

When the SRE agent sets requires\_human\_approval=True, the graph reaches the human\_gate node and suspends. The checkpoint is saved. An external caller (a Slack bot, a web UI, or a CLI) retrieves the pending approval via the harness API, presents the proposed action to a human, and — if approved — calls the resume endpoint with a signed human\_approval\_token. The graph resumes from the checkpoint, the SRE agent now holds a valid token, and the gateway permits the shell\_exec call.

| Security Invariant The human\_approval\_token is a short-lived signed JWT (HS256, 10-minute TTL) issued by the harness API after a human explicitly approves. The token encodes the specific tool call and thread\_id it authorises. The gateway validates the token's signature and scope before permitting shell\_exec — not just its presence in state. |
| :---- |

## **Test Suite (write first)**

| Test Name | Type | Asserts |
| :---- | :---- | :---- |
| `test_classify_design_task` | **Unit** | Input 'Design the auth service' → task\_type='design'. |
| `test_classify_review_task` | **Unit** | Input 'Review this PR diff' → task\_type='review'. |
| `test_classify_incident_task` | **Unit** | Input 'Alert fired: DB latency spike' → task\_type='incident'. |
| `test_formula_lookup_hit` | **Integration** | formula\_lookup node with task\_type='incident' returns formula\_id='sre:triage-incident' and populates formula\_instance\_id. |
| `test_formula_lookup_miss` | **Integration** | formula\_lookup with no matching formula sets formula\_id=None and graph continues to ad-hoc routing. |
| `test_route_to_architect` | **Unit** | task\_type='design' routes graph to architect node. |
| `test_route_to_reviewer` | **Unit** | task\_type='review' routes graph to code\_reviewer node. |
| `test_route_to_sre` | **Unit** | task\_type='incident' routes graph to sre node. |
| `test_agent_executes_formula_steps` | **Integration** | When formula\_id is set, SRE agent calls tools in the order defined by the formula steps, not ad-hoc. |
| `test_agent_executes_ad_hoc_without_formula` | **Integration** | When formula\_id is None, SRE agent reasons freely and does not error. |
| `test_full_design_task_e2e` | **E2E** | Full graph run: design task → formula\_lookup → architect node → synthesise → final\_response populated. |
| `test_full_review_task_e2e` | **E2E** | Full graph run: review task → formula\_lookup → reviewer node → synthesise → verdict in final\_response. |
| `test_full_incident_task_no_shell_e2e` | **E2E** | Incident task not requiring shell\_exec completes without human gate. |
| `test_formula_outcome_recorded` | **Integration** | After synthesise node, Dolt formula store contains an outcome record (success, duration\_ms) for the formula\_instance\_id. |
| `test_propose_formula_on_novel_task` | **Integration** | Ad-hoc run reaching propose\_formula node inserts a draft formula row in Dolt with status='draft'. |
| `test_human_gate_pauses_graph` | **Integration** | SRE output with requires\_human\_approval=True causes graph to pause at human\_gate. |
| `test_human_gate_resumes_with_valid_token` | **Integration** | Valid human\_approval\_token resumes graph; shell\_exec proceeds. |
| `test_human_gate_rejects_expired_token` | **Integration** | Expired human\_approval\_token is rejected; graph moves to error\_handler. |
| `test_human_gate_rejects_wrong_scope` | **Integration** | Token scoped to different thread\_id or tool is rejected. |
| `test_error_handler_on_gateway_403` | **Unit** | Gateway 403 triggers error\_handler node; error dict populated with tool\_name and reason. |
| `test_checkpoint_survives_human_pause` | **Integration** | Graph state is correctly checkpointed before human\_gate; resumes from checkpoint on approval. |
| `test_otel_spans_emitted` | **Integration** | After a full graph run, OTel exporter receives spans for classify, formula\_lookup, route, agent, synthesise nodes. |

## **Definition of Done**

24. All 23 tests above pass in CI.

25. Human approval flow demonstrated end-to-end: SRE alert → formula\_lookup → human gate pause → mock approval → shell\_exec executes → incident summary written to memory.

26. All graph node transitions are visible as OTel spans including formula\_lookup.

27. Parallel requests to the supervisor do not share state (thread isolation verified by concurrent E2E test).

28. Graph can be resumed after a simulated PostgreSQL restart (checkpoint durability).

29. Three seed formulas in Dolt (sre:triage-incident, code\_reviewer:review-pr, architect:write-adr) are matched correctly by formula\_lookup for representative inputs.

30. A novel ad-hoc task results in a draft formula row in the Dolt formula store, visible in dolt log.

| PHASE 5 | Production Hardening Weeks 13–16 · Gateway migration · Cost controls · Runbooks |
| :---: | :---- |

## **Objective**

Harden the harness for production: migrate the MCP gateway from MCPJungle to ContextForge, implement cost controls and rate limiting, add alerting, write operational runbooks, and conduct a security review against the OWASP Agentic AI Top 10\.

## **Work Streams**

### **5a — Gateway Migration: MCPJungle → ContextForge**

ContextForge provides multi-region federation, a richer plugin ecosystem, and a more mature audit story. The migration must be transparent to agents — they see the same OAuth endpoints and tool interfaces.

* Export MCPJungle tool group definitions and re-import into ContextForge server registry.

* Migrate OPA policy bundle to ContextForge's policy plugin format.

* Run the full Phase 1–4 integration test suite against ContextForge to verify parity.

* Cut over traffic with a feature flag; roll back to MCPJungle if any test fails.

### **5b — Cost Controls & Rate Limiting**

| Control | Implementation |
| :---- | :---- |
| **Token budget per thread** | Each thread\_id has a max token budget (configurable per agent role). Graph terminates gracefully and writes a partial result when budget is exceeded. |
| **Tool call rate limiting** | Gateway enforces N tool calls per agent per minute. Configurable in OPA policy. Prevents runaway agent loops. |
| **Cost attribution** | Every LLM call tagged with agent\_role and thread\_id in OTel metadata. Grafana dashboard shows cost per role per day. |
| **Alert on budget breach** | OTel alert fires if any single thread exceeds 2× its expected token budget. Pages on-call. |

### **5c — OWASP Agentic AI Top 10 Review**

Conduct a structured review against all 10 OWASP Agentic AI risks. Document each risk, its mitigations in the harness, and any residual risk accepted. This document lives in /security/owasp-review.md and is reviewed on every major release.

| Risk | Harness Mitigation |
| :---- | :---- |
| **Prompt Injection** | Agent inputs sanitised before passing to LLM. MCP server responses treated as untrusted data. Mitigated. |
| **Insecure Tool Use** | Gateway enforces tool scoping per agent identity. No agent can call outside its allowed\_tools. Mitigated. |
| **Memory Poisoning** | Memory store writes require agent authentication. No external actor can write to memory. Mitigated. |
| **Excessive Agency** | shell\_exec requires human\_approval\_token. Rate limits on all tool calls. Mitigated. |
| **Insecure Output Handling** | All agent outputs are JSON Schema validated before being returned to callers. Partial mitigation. |
| **Data Exfiltration** | Gateway network policy blocks agent outbound connections except to whitelisted MCP servers. Mitigated. |
| **Residual Risks** | LLM hallucination in structured outputs, side-channel timing attacks on policy decisions. Accepted with monitoring. |

### **5d — Operational Runbooks**

* Agent unresponsive — how to inspect the checkpoint, kill the thread, and resume or abandon.

* Gateway policy denied all calls — how to roll back an OPA policy bundle.

* Memory store corruption — how to restore from PostgreSQL point-in-time recovery.

* Human gate stuck — how to force-expire a pending approval and route to error\_handler.

* Cost spike — how to identify the runaway thread and terminate it.

* Bad formula deployed — how to dolt revert a formula commit, how to mark a formula deprecated, and how to verify the rollback with dolt diff.

* Consolidation runaway — how to inspect a stuck ConsolidationWorker, cancel the run, and re-seed the job safely without double-consolidating episodes.

* Memory bloat — how to query unconsolidated episodic item count per namespace, force a manual consolidation pass, and tune the TTL schedule.

* Audit gap investigation — how to use dolt log and dolt diff to reconstruct the sequence of tool calls for a given agent\_id and time window.

## **Test Suite (write first)**

| Test Name | Type | Asserts |
| :---- | :---- | :---- |
| `test_contextforge_tool_group_parity` | **Integration** | All Phase 1 tool group tests pass against ContextForge (parametrised re-run). |
| `test_contextforge_audit_log_parity` | **Integration** | Audit rows written by ContextForge match same schema as MCPJungle rows. |
| `test_token_budget_enforced` | **Integration** | Thread exceeding token budget terminates with budget\_exceeded error, not a hang. |
| `test_rate_limit_tool_calls` | **Integration** | Agent making N+1 tool calls in one minute receives 429 from gateway on the last call. |
| `test_cost_otel_tag_present` | **Integration** | OTel spans for LLM calls include agent\_role and thread\_id tags. |
| `test_owasp_prompt_injection_blocked` | **Integration** | Injected instruction in tool response does not alter agent's subsequent tool calls. |
| `test_owasp_memory_write_requires_auth` | **Integration** | Unauthenticated write to memory store returns 401\. |
| `test_gateway_rollback` | **Integration** | Feature flag flipped back to MCPJungle; all Phase 1 tests pass against MCPJungle. |

## **Definition of Done**

31. All 8 tests above pass in CI, plus all prior phase tests pass against ContextForge.

32. OWASP review document present, reviewed, and signed off.

33. All four operational runbooks present in /docs/runbooks/.

34. Grafana dashboard showing cost per agent role is live and rendering real data.

35. Load test: 50 concurrent task submissions, p99 latency \<10s, 0 data isolation failures.

# **Non-Functional Requirements**

| Requirement | Target |
| :---- | :---- |
| **Latency** | Agent response (excluding LLM inference): p99 \<500ms gateway overhead. Total task latency budget set per agent type (design: 120s, review: 60s, incident: 90s). |
| **Throughput** | 50 concurrent threads without state bleed. Horizontal scaling via multiple LangGraph worker processes behind a load balancer. |
| **Availability** | Gateway: 99.9% uptime. Memory store: 99.95% (PostgreSQL with replica). Agents: best-effort (stateless workers, restartable from checkpoint). |
| **Durability** | Checkpoints and memory items survive PostgreSQL restart. Daily point-in-time backup retained for 30 days. |
| **Security** | All traffic TLS 1.3+. Secrets in environment variables or a secrets manager (never in code or config files). Agent tokens rotated every 15 minutes. |
| **Observability** | 100% of tool calls traced in OTel. 100% of policy decisions logged. Grafana dashboards for cost, latency, error rate, and active threads. |
| **Testability** | 80% line coverage minimum. E2E tests run in \<10 minutes using cassette-recorded LLM responses. Integration tests run in \<5 minutes using Testcontainers. |
| **Portability** | Docker Compose for local dev. Kubernetes manifests for production. No cloud-provider-specific dependencies in harness core (AWS/GCP/Azure services are plugins only). |

# **Phase Dependency Map**

Phases are strictly sequential. No phase may begin until the previous phase's Definition of Done is complete and signed off.

| Phase | Depends On | Scope |
| :---- | :---- | :---- |
| **Phase 0** | None | Foundation, CI, Docker Compose stack, test infrastructure. |
| **Phase 1** | Phase 0 complete | MCP Gateway, OAuth identities, OPA policy, Dolt audit log. |
| **Phase 2** | Phase 0 complete | Checkpointer, memory store, Dolt formula store \+ seed formulas. (Can run in parallel with Phase 1.) |
| **Phase 3** | Phases 1 \+ 2 complete | Agent nodes. Requires gateway for tool calls and memory layer for persistence. |
| **Phase 4** | Phase 3 complete | Supervisor graph, routing, human gate, E2E tests. |
| **Phase 5** | Phase 4 complete | Gateway migration, cost controls, security review, runbooks. |

| Note on Phase 2 Parallelism Phase 2 (Memory Layer) depends only on Phase 0 infrastructure and can begin in parallel with Phase 1 (MCP Gateway) if two engineers are available. Phase 3 cannot begin until both Phase 1 and Phase 2 are complete. |
| :---- |

# **Appendix: Tooling Reference**

| Tool / Resource | Reference |
| :---- | :---- |
| **LangGraph 1.0** | https://github.com/langchain-ai/langgraph |
| **MCPJungle** | https://github.com/mcpjungle/MCPJungle |
| **IBM ContextForge** | https://github.com/IBM/mcp-context-forge |
| **OPA (Open Policy Agent)** | https://www.openpolicyagent.org/ |
| **LangSmith** | https://smith.langchain.com/ |
| **pytest-recording (vcrpy)** | https://github.com/kiwicom/pytest-recording |
| **Testcontainers Python** | https://testcontainers-python.readthedocs.io/ |
| **OpenTelemetry Python** | https://opentelemetry.io/docs/languages/python/ |
| **pgvector** | https://github.com/pgvector/pgvector |
| **MCP Authorization Spec** | https://modelcontextprotocol.io/specification/draft/basic/authorization |
| **OWASP Agentic AI Top 10** | https://owasp.org/www-project-top-10-for-large-language-model-applications/ |
| **agentic-community/mcp-gateway-registry** | https://github.com/agentic-community/mcp-gateway-registry |
| **Dolt (git-versioned DB)** | https://github.com/dolthub/dolt |
| **DoltHub docs** | https://docs.dolthub.com/ |
| **Gas City SDK (inspiration)** | https://github.com/gastownhall/gascity |
| **Gas Town (original, Yegge)** | https://steve-yegge.medium.com/welcome-to-gas-town-4f25ee16dd04 |
