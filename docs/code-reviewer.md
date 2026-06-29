# Code Reviewer Agent

The code reviewer takes a diff and returns structured findings: severity-classified issues, a verdict, and a plain-English summary. It combines static analysis (semgrep) with an LLM reasoning pass.

## What it checks

- **Security** — credential leaks, injection flaws (SQL, shell, path traversal), eval usage, `subprocess shell=True`
- **Code quality** — error handling gaps, dead code, resource leaks, hardcoded values
- **Architecture** — tight coupling, shared mutable state, missing abstractions

Findings are classified `CRITICAL`, `WARNING`, or `INFO`. Verdict is `fail` if any `CRITICAL` finding exists.

## Output schema

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

## How to invoke

**From Claude Code** — via MCP tool:

```
review_diff  →  mcp__ai-harness__review_server__review_diff
```

Pass a `diff_text` string, or a `pr_number` + `github_repo` to fetch from GitHub.

**From CI pipelines** — via HTTP endpoint on the review-server:

```bash
DIFF=$(git diff origin/main...HEAD)
curl -s http://localhost:9003/review \
  -H "Content-Type: application/json" \
  -d "{\"diff_text\": $(echo "$DIFF" | jq -Rs .)}" | jq .
```

Body: `{"diff_text": "...", "task": "...", "provider": "ollama|gemini|openrouter"}` (task and provider are optional).

Auth: set `REVIEW_API_KEY` in env to require `Authorization: Bearer <key>`. Unset = open (dev mode).

## Changing the LLM

```bash
curl -s http://localhost:9003/config \
  -X PUT \
  -H "Content-Type: application/json" \
  -d '{"llm_provider": "gemini", "gemini": {"model": "gemini-2.5-flash"}}'
```

Config persists across restarts via the `server_config` PostgreSQL table. No rebuild or restart needed.

## Tools available to the agent (OPA-enforced)

| Short name | What it does |
|---|---|
| `git_diff` | Fetch a diff: passthrough text, GitHub PR, or local git refs |
| `run_linter` | Semgrep lint on diff additions; rules in `stub_servers/semgrep-rules.yml` |
| `coverage_report` | Per-file coverage data (stub) |
| `repo_conventions_read` | Fetch `CONTRIBUTING.md` and coding standards from a GitHub repo |
| `review_diff` | Self-referential — the agent is also exposed as this MCP tool |

The `code_reviewer` OPA role is blocked from all other tools (architect, SRE) at the policy layer.

## Eval suite

The reviewer is scored against labeled diff fixtures in `eval-fixtures/`. Run:

```bash
pytest -m eval -v -s packages/harness-tests/test_eval_reviewer.py
```

Pass bar: verdict accuracy ≥ 80%, recall ≥ 60% across 6 fixtures. See [eval-guide.md](eval-guide.md) for fixture format and adding new cases.
