# AI Harness — Thin Slice Build Plan

> **Goal**: A governed Code Reviewer agent you can run today.  
> Submit a git diff → agent calls governed tools → structured findings + verdict.  
> Everything else deferred.

---

## What You're Building

```
you → reviewer agent → MCPJungle (OAuth + OPA) → stub MCP tools → structured output
                             ↓
                       PostgreSQL (checkpoint)
```

**In scope:**
- 3-service Docker Compose (PostgreSQL, MCPJungle, OPA)
- One OAuth 2.1 client: `code-reviewer`
- One OPA rule: code-reviewer may call `git_diff` and `run_linter`
- Two stub MCP tool servers (tiny FastAPI apps — no real Git needed yet)
- Code Reviewer agent, called directly (no supervisor graph)
- PostgreSQL checkpointer (persistence from day one)
- JSON Schema validated output
- Three tests that prove it all works together

**Explicitly out of scope:**
- Supervisor / routing / formula store
- Memory store, Redis, ConsolidationWorker
- Dolt audit log (stdout for now)
- Architect and SRE agents
- Human-in-the-loop gate
- ContextForge

---

## File Structure

Create only these files. Nothing else.

```
ai-harness/
├── docker-compose.yml
├── Makefile
├── pyproject.toml
├── .env.example
├── policies/
│   └── harness.rego
├── mcpjungle/
│   └── config.yml
├── stub_servers/
│   ├── git_diff_server.py      ← returns a fake diff
│   └── linter_server.py        ← returns fake lint warnings
└── packages/
    ├── harness-gateway/
    │   ├── pyproject.toml
    │   └── harness_gateway/
    │       ├── __init__.py
    │       └── client.py        ← OAuth token fetch + tool call
    ├── harness-agents/
    │   ├── pyproject.toml
    │   └── harness_agents/
    │       ├── __init__.py
    │       ├── types.py         ← AgentState TypedDict + output schema
    │       └── reviewer.py      ← Code Reviewer agent
    └── harness-tests/
        ├── pyproject.toml
        ├── conftest.py          ← fixtures
        └── test_thin_slice.py  ← THE three tests
```

---

## Step 1 — Write the Three Tests First

**File: `packages/harness-tests/test_thin_slice.py`**

Write these before any other code. Run them. Watch them fail.

```python
import pytest
import jsonschema
from uuid import uuid4
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.types import AgentState, REVIEWER_OUTPUT_SCHEMA
from harness_gateway.client import GatewayClient

# ── fixtures (defined in conftest.py) ────────────────────────────────────────
# gateway_client  →  authenticated GatewayClient for code-reviewer role
# reviewer_agent  →  CodeReviewerAgent wired to gateway_client

SAMPLE_DIFF = """
diff --git a/auth.py b/auth.py
index 1a2b3c4..5d6e7f8 100644
--- a/auth.py
+++ b/auth.py
@@ -12,6 +12,8 @@ def login(username, password):
     user = db.find(username)
+    print(f"Login attempt: {username} {password}")   # obvious secret leak
     if user and user.check_password(password):
         return generate_token(user)
"""

@pytest.mark.integration
async def test_reviewer_produces_structured_output(reviewer_agent):
    """Core contract: diff in → validated structured output."""
    state = AgentState(
        task="Review this diff for security and quality issues.",
        diff=SAMPLE_DIFF,
        thread_id=str(uuid4()),
        agent_output=None,
        requires_human_approval=False,
        error=None,
    )
    result = await reviewer_agent.run(state)

    output = result["agent_output"]
    assert output is not None

    # Must validate against the output contract schema
    jsonschema.validate(output, REVIEWER_OUTPUT_SCHEMA)

    # The diff has an obvious secret leak — verdict must be fail
    assert output["verdict"] == "fail"
    assert len(output["findings"]) > 0


@pytest.mark.integration
async def test_tool_calls_go_through_gateway(reviewer_agent, gateway_client):
    """Governance contract: tool calls are visible in gateway audit log."""
    state = AgentState(
        task="Quick review.",
        diff=SAMPLE_DIFF,
        thread_id=str(uuid4()),
        agent_output=None,
        requires_human_approval=False,
        error=None,
    )
    await reviewer_agent.run(state)

    # Gateway client exposes a last_calls log for testing
    tool_names = [c["tool"] for c in gateway_client.last_calls]
    assert "git_diff" in tool_names or "run_linter" in tool_names


@pytest.mark.integration
async def test_reviewer_denied_cross_role_tool(gateway_client):
    """Policy contract: code-reviewer token cannot call shell_exec."""
    with pytest.raises(Exception, match="403|Forbidden|ToolAccessDenied"):
        await gateway_client.call_tool("shell_exec", {"command": "ls"})
```

---

## Step 2 — Define Types & Output Schema

**File: `packages/harness-agents/harness_agents/types.py`**

```python
from typing import TypedDict, Any

class AgentState(TypedDict):
    task: str
    diff: str                         # the git diff text
    thread_id: str
    agent_output: dict | None
    requires_human_approval: bool
    error: dict | None


# JSON Schema the reviewer output MUST satisfy
REVIEWER_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "findings", "summary"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "fail"]
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "file", "line", "message", "suggestion"],
                "properties": {
                    "severity":   {"type": "string", "enum": ["INFO", "WARNING", "CRITICAL"]},
                    "file":       {"type": "string"},
                    "line":       {"type": "integer"},
                    "message":    {"type": "string"},
                    "suggestion": {"type": "string"},
                }
            }
        },
        "summary": {"type": "string"},
    },
    "additionalProperties": False,
}
```

---

## Step 3 — Gateway Client

**File: `packages/harness-gateway/harness_gateway/client.py`**

```python
import httpx
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

class ToolAccessDenied(Exception):
    pass

@dataclass
class GatewayClient:
    """Thin wrapper: fetch OAuth token, call tools through MCPJungle."""
    gateway_url: str          # e.g. http://localhost:8080
    client_id: str            # e.g. "code-reviewer"
    client_secret: str
    _token: str | None = field(default=None, repr=False)
    _token_expiry: float = field(default=0.0, repr=False)
    last_calls: list = field(default_factory=list, repr=False)

    async def _get_token(self) -> str:
        """Fetch a fresh token if expired (15-min TTL)."""
        if self._token and time.time() < self._token_expiry - 30:
            return self._token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 900)
        return self._token

    async def call_tool(self, tool_name: str, params: dict) -> dict:
        """Call a tool via the gateway. Raises ToolAccessDenied on 403."""
        token = await self._get_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/tools/{tool_name}",
                json=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )

        # Track for test assertions
        self.last_calls.append({"tool": tool_name, "status": resp.status_code})
        logger.info("tool_call tool=%s status=%d", tool_name, resp.status_code)

        if resp.status_code == 403:
            raise ToolAccessDenied(f"403 Forbidden: {tool_name}")
        if resp.status_code == 401:
            self._token = None  # force re-auth next call
            raise Exception(f"401 Unauthorized: {tool_name}")

        resp.raise_for_status()
        return resp.json()
```

---

## Step 4 — Code Reviewer Agent

**File: `packages/harness-agents/harness_agents/reviewer.py`**

```python
import json
import logging
import jsonschema
from anthropic import AsyncAnthropic
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_agents.types import AgentState, REVIEWER_OUTPUT_SCHEMA

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3

SYSTEM_PROMPT = """You are a senior code reviewer. You will be given a git diff.

Your job:
1. Call the git_diff tool to retrieve the canonical diff (use the diff_id from your input).
2. Call the run_linter tool to get static analysis results.
3. Analyse both. Surface findings by severity: CRITICAL, WARNING, or INFO.
4. Return a structured JSON object — nothing else.

Output format (strict JSON, no markdown):
{
  "verdict": "pass" | "fail",
  "findings": [
    {"severity": "CRITICAL"|"WARNING"|"INFO", "file": "...", "line": 0, "message": "...", "suggestion": "..."}
  ],
  "summary": "one paragraph summary"
}

Rules:
- verdict is "fail" if ANY finding is CRITICAL.
- verdict is "pass" only if there are zero CRITICAL findings.
- Do not include markdown fences in your response. Raw JSON only."""


class CodeReviewerAgent:
    name = "code_reviewer"
    allowed_tools = ["git_diff", "run_linter"]
    memory_namespace = "code_reviewer"

    def __init__(self, gateway: GatewayClient, llm_client: AsyncAnthropic):
        self.gateway = gateway
        self.llm = llm_client

    async def run(self, state: AgentState) -> AgentState:
        diff_text = state["diff"]
        task = state["task"]

        # Call tools (governed by gateway)
        try:
            diff_result = await self.gateway.call_tool(
                "git_diff", {"diff_text": diff_text}
            )
            lint_result = await self.gateway.call_tool(
                "run_linter", {"diff_text": diff_text}
            )
        except ToolAccessDenied as e:
            logger.error("tool_access_denied: %s", e)
            return {**state, "error": {"code": "tool_access_denied", "reason": str(e)}}

        # Build context for the LLM
        user_message = f"""Task: {task}

Diff tool result:
{json.dumps(diff_result, indent=2)}

Linter result:
{json.dumps(lint_result, indent=2)}

Return your structured review as raw JSON."""

        # LLM call with retry loop (max 3 iterations for valid JSON)
        raw_output = None
        for attempt in range(MAX_ITERATIONS):
            response = await self.llm.messages.create(
                model="claude-3-5-haiku-20241022",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw = response.content[0].text.strip()

            try:
                parsed = json.loads(raw)
                jsonschema.validate(parsed, REVIEWER_OUTPUT_SCHEMA)
                raw_output = parsed
                break
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                logger.warning("attempt %d: invalid output: %s", attempt + 1, e)
                user_message += f"\n\nYour previous response was invalid: {e}\nTry again. Raw JSON only."

        if raw_output is None:
            return {**state, "error": {"code": "invalid_output", "reason": "max retries exceeded"}}

        return {**state, "agent_output": raw_output}
```

---

## Step 5 — Stub MCP Servers

These are tiny FastAPI apps. They return canned responses so you don't need real Git or a real linter yet. Swap them for real MCP servers later.

**File: `stub_servers/git_diff_server.py`**

```python
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.post("/tools/git_diff")
async def git_diff(request: Request):
    body = await request.json()
    # Echo back the diff text — the agent sent us what it got from state
    return {"diff": body.get("diff_text", ""), "source": "stub"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9001)
```

**File: `stub_servers/linter_server.py`**

```python
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.post("/tools/run_linter")
async def run_linter(request: Request):
    body = await request.json()
    diff = body.get("diff_text", "")
    warnings = []
    # Naive pattern matching — good enough for the thin slice
    if "print(" in diff:
        warnings.append({"rule": "no-print", "message": "print() found — possible secret leak", "severity": "WARNING"})
    if "password" in diff.lower() and "print" in diff.lower():
        warnings.append({"rule": "secret-in-log", "message": "Password may be logged", "severity": "CRITICAL"})
    return {"warnings": warnings, "error_count": sum(1 for w in warnings if w["severity"] == "CRITICAL")}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9002)
```

---

## Step 6 — Infrastructure Files

**File: `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: harness
      POSTGRES_USER: harness
      POSTGRES_PASSWORD: harness
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U harness"]
      interval: 5s
      retries: 5

  opa:
    image: openpolicyagent/opa:latest
    command: run --server --addr :8181 /policies
    volumes:
      - ./policies:/policies:ro
    ports: ["8181:8181"]

  mcpjungle:
    image: mcpjungle/mcpjungle:latest
    ports: ["8080:8080"]
    environment:
      OPA_URL: http://opa:8181
      DATABASE_URL: postgres://harness:harness@postgres:5432/harness
    volumes:
      - ./mcpjungle/config.yml:/config/config.yml:ro
    depends_on:
      postgres:
        condition: service_healthy

  git-diff-stub:
    build:
      context: ./stub_servers
      dockerfile: Dockerfile.stub
    command: python git_diff_server.py
    ports: ["9001:9001"]

  linter-stub:
    build:
      context: ./stub_servers
      dockerfile: Dockerfile.stub
    command: python linter_server.py
    ports: ["9002:9002"]
```

**File: `policies/harness.rego`** (minimal — just the one rule you need)

```rego
package harness

default allow = false

allow {
    input.agent_role == "code_reviewer"
    input.tool_name in {"git_diff", "run_linter"}
}
```

**File: `mcpjungle/config.yml`** (check MCPJungle docs for exact format — this is the shape)

```yaml
clients:
  - id: code-reviewer
    secret: "${CODE_REVIEWER_SECRET}"
    role: code_reviewer

tool_groups:
  - name: code_reviewer
    tools:
      - name: git_diff
        server: http://git-diff-stub:9001
      - name: run_linter
        server: http://linter-stub:9002
```

**File: `.env.example`**

```bash
PG_DSN=postgresql://harness:harness@localhost:5432/harness
CODE_REVIEWER_SECRET=change-me-in-dev
MCPJUNGLE_URL=http://localhost:8080
ANTHROPIC_API_KEY=sk-ant-...
LANGSMITH_API_KEY=ls-...   # optional for now
```

**File: `packages/harness-tests/conftest.py`**

```python
import pytest
import os
from anthropic import AsyncAnthropic
from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent

@pytest.fixture
def gateway_client():
    return GatewayClient(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        client_id="code-reviewer",
        client_secret=os.environ["CODE_REVIEWER_SECRET"],
    )

@pytest.fixture
def reviewer_agent(gateway_client):
    return CodeReviewerAgent(
        gateway=gateway_client,
        llm_client=AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"]),
    )
```

**File: `Makefile`**

```makefile
stack-up:
	docker compose up -d --wait

stack-down:
	docker compose down -v

test:
	pytest packages/harness-tests/test_thin_slice.py -v -m integration

review:
	@echo "Running a sample review..."
	python -c "
import asyncio, os
from anthropic import AsyncAnthropic
from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.types import AgentState

diff = open('sample.diff').read() if __import__('os').path.exists('sample.diff') else 'diff --git a/x.py'
agent = CodeReviewerAgent(
    gateway=GatewayClient(os.environ['MCPJUNGLE_URL'], 'code-reviewer', os.environ['CODE_REVIEWER_SECRET']),
    llm_client=AsyncAnthropic()
)
result = asyncio.run(agent.run(AgentState(task='Review this', diff=diff, thread_id='manual-run', agent_output=None, requires_human_approval=False, error=None)))
import json; print(json.dumps(result['agent_output'], indent=2))
"
```

**File: `pyproject.toml`** (root — workspace config)

```toml
[tool.uv.workspace]
members = ["packages/*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "integration: requires Docker stack",
    "e2e: full graph, slow",
    "live: requires real LLM (not cassette-recorded)",
]

[tool.ruff]
line-length = 100
```

---

## Step 7 — Make the Tests Pass

```bash
# 1. Start the stack
make stack-up

# 2. Copy .env.example, fill in your keys
cp .env.example .env
# edit .env — set ANTHROPIC_API_KEY, CODE_REVIEWER_SECRET, etc.
source .env

# 3. Run the tests
make test

# 4. Optionally, run a manual review
echo "diff --git a/auth.py..." > sample.diff
make review
```

The three tests tell you:
- **Test 1**: The agent produces valid structured output and catches the obvious bug → core value delivered
- **Test 2**: Tool calls go through the gateway → governance is real, not mocked
- **Test 3**: Cross-role tool access is blocked → policy enforcement works

---

## What "Done" Looks Like

```json
{
  "verdict": "fail",
  "findings": [
    {
      "severity": "CRITICAL",
      "file": "auth.py",
      "line": 14,
      "message": "Password is being printed to stdout — credential leak risk.",
      "suggestion": "Remove the print statement. Use structured logging with redaction if debugging is needed."
    }
  ],
  "summary": "The diff introduces a critical security vulnerability: user passwords are logged in plaintext via print(). This must be fixed before merge."
}
```

All three tests green. Gateway audit shows tool calls. OPA blocked `shell_exec`. You have a working governed agent.

---

## What Comes Next (in order)

Once this is green, each addition is small and testable:

1. **Add Dolt audit log** — replace stdout logging in `gateway/client.py` with a Dolt INSERT. One table, one commit per call. (Phase 1 proper)
2. **Add the checkpointer** — two lines: `PostgresSaver(conn_string)`, pass to LangGraph graph. State now survives restarts.
3. **Add the Architect agent** — same pattern as reviewer, different tools and output schema.
4. **Add the SRE agent** — add the human gate logic once the other two agents work.
5. **Wire the supervisor** — `classify → route → agent → synthesise`. Now you have the full Phase 4 graph.
6. **Add memory store** — each agent reads/writes to its namespace. ConsolidationWorker runs nightly.

Each step is one new file and a handful of new tests. Nothing breaks what came before.

---

*Thin slice plan · AI Harness v1.0 · May 2026*
