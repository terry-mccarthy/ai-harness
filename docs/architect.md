# Architect Agent

The architect agent analyses a codebase for structural problems: layering violations, tight coupling, leaky abstractions, and dependency hygiene. It runs a four-phase ReAct loop and files GitHub issues for CRITICAL/HIGH findings.

## Four-phase analysis

1. **Recon** — discovers entry points, module boundaries, and dependency graph via `codebase_search` and `adr_read`
2. **Flow** — traces request paths to identify where logic lives and how layers communicate
3. **Abstraction** — checks whether domain boundaries are clean (no ORM leaking through ports, no HTTP concerns in domain logic, etc.)
4. **Synthesis** — consolidates findings into a structured report and files issues for anything CRITICAL or HIGH

## Output schema

```json
{
  "verdict": "fail",
  "findings": [
    {
      "severity": "CRITICAL",
      "category": "layering",
      "description": "SQLAlchemy model exposed directly through the domain port",
      "recommendation": "Define a domain entity and map it from the ORM model in the persistence layer"
    }
  ],
  "recommendations": ["..."],
  "summary": "..."
}
```

## How to invoke

**From Claude Code** — via MCP tool:

```
architecture_review  →  mcp__ai-harness__review_server__architecture_review
```

Pass a `repo` (GitHub URL). The agent runs all four phases and returns structured findings.

**Bootstrap** — generate an `ARCHITECTURE.md` from scratch:

```
bootstrap_architecture  →  mcp__ai-harness__review_server__bootstrap_architecture
```

This runs the same four phases plus a fifth doc-render pass. Returns `architecture_md` as a markdown string. Requires an extended MCP timeout:

```bash
MCP_TOOL_TIMEOUT=300000 claude
```

## Architectural gate

Design tasks routed through the supervisor graph pass through an **architectural gate** before synthesis. The gate runs `execute_architecture_check` and classifies the result:

- **PASS** → proceeds to synthesis
- **FAIL / HARD** → graph pauses at `human_gate`; requires human review
- **FAIL / SOFT** → can be overridden by providing a `human_justification` string

Gate failures are recorded in the `architectural_gate_failures` Dolt table with a DOLT_COMMIT.

## Tools available to the agent (OPA-enforced)

| Short name | What it does |
|---|---|
| `codebase_search` | Search file/symbol patterns via GitHub API |
| `adr_read` | Read ADRs from `docs/adr/` in a GitHub repo |
| `architecture_review` | Four-phase analysis (the agent calls itself recursively via this tool) |
| `execute_architecture_check` | Run invariant checks (stub — sandbox not yet wired) |
| `code_health_score` | Radon cyclomatic complexity per file; returns 0–10 scores sorted worst-first |
| `codebase_hotspots` | Rank files by complexity hotspot risk; optional language filter |
| `logical_coupling` | Find files that historically co-change (GitHub commits API) |
| `issue_create` | File a GitHub issue with title, body, and optional labels |
| `repo_conventions_read` | Fetch `CONTRIBUTING.md` and coding standards |

The `architect` OPA role is blocked from SRE and code-reviewer tools.

## Eval suite

The architect is scored against three fixture repos in `eval-fixtures/architecture/`. Run:

```bash
pytest -m eval -v -s packages/harness-tests/test_eval_architect.py
```

Pass bar: schema validity 100%, detection ≥ 66%, recall ≥ 50%. See [eval-guide.md](eval-guide.md) for fixture format.
