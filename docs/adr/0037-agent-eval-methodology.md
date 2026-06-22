# ADR-0037: Agent eval methodology — fixture-based quality benchmarking for LLM agents

**Status:** accepted
**Date:** 2026-06-22

Delivered as of 2026-06-22 with the `CodeReviewerAgent` (7 fixtures, 8 tests) and `ArchitectAgent` (3 fixtures, 4 tests) eval suites, plus `.github/workflows/architect-eval.yml` for CI.

## Context

LLM-based agents (`CodeReviewerAgent`, `ArchitectAgent`, `SREAgent`) emit structured output that is critical to downstream workflows (gate decisions, issue filing, runbook execution). Unlike deterministic code, LLM behavior is **sensitive to prompt rewrites, model changes, and version drifts**. A prompt enhancement can unexpectedly degrade on some patterns; a model upgrade can shift reasoning quality in non-obvious ways. Without a regression net, these degradations only surface in production.

The existing test suite covers *happy path* (agent runs, returns structured output) and *integration* (agent reaches the real Docker stack and MCP servers). Neither dimension catches **quality regressions** — a review that parses but misses a critical finding, or an architecture report that's schema-valid but detects nothing.

## Decision

Build a **lightweight, provider-agnostic eval suite** for each agent type.

### Eval design

**Fixtures:** each case is a labeled example representing a class of problem the agent should solve. A fixture is **not a live codebase**, but a small set of **canned tool responses** (JSON) — what `codebase_search`, `git_diff`, etc. return for that case. A `_MockGateway` intercepts tool calls and returns the fixture data, bypassing the live Docker stack and GitHub API.

**Fixture format:**
- `eval-fixtures/<agent_type>/<case>/` — per-case data (query results, diffs, hotspots)
- `eval-fixtures/<agent_type>/labels/<case>.json` — ground truth: expected verdict/findings + `must_flag` patterns to detect

For reviewers, the fixture is a `.diff` file. For the architect (multi-phase), fixtures are structured as phase-specific JSON files (`recon.json`, `files.json`, `hotspots.json`, `interfaces.json`, `adrs.json`) — each routed by `_MockGateway` to the phase that requests it.

**Scoring dimensions** (multi-faceted, not a single number):

1. **Schema validity:** output parses and conforms to the declared `*_OUTPUT_SCHEMA`. This is the gate — a schema violation means the agent is broken, not just wrong.

2. **Detection accuracy** (for smell-detection agents like the reviewer and architect):
   - A `clean` baseline fixture must not raise a false CRITICAL/HIGH.
   - A `planted-smell` fixture must raise at least one HIGH/CRITICAL finding.

3. **Recall:** of the `must_flag` patterns in the label, how many surface in the agent's HIGH/CRITICAL findings? Measured per fixture; averaged across the suite.

**Pass bars** (coarse, intentionally forgiving for small live-LLM sets):
- Schema validity: 100% (non-negotiable)
- Detection accuracy: ≥ 66% (≥2 of 3 cases detected correctly)
- Avg recall: ≥ 50% (at least half of the must-flag patterns are caught)

Early-stage suites (CodeReviewerAgent: 8 fixtures; ArchitectAgent: 3) are tuned conservatively to avoid brittleness; expand as the suite grows.

### CI integration

`.github/workflows/architect-eval.yml` is a template. The `eval` job:
- Triggers on PRs that touch the agent code, its prompt, the schema, or the fixtures
- Uses `_build_llm` (in the eval harness) to select the LLM provider from `LLM_PROVIDER` env var
- Defaults to local Ollama; CI runs with `LLM_PROVIDER=openrouter` on a fast hosted model (`gemini-2.5-flash`), requiring an `OPENROUTER_API_KEY` repo secret
- Skips gracefully (with a warning) if the secret is unset

The eval runs **without** a Docker stack — only the agent code, the fixture mocks, and an LLM. Rounds to ~1 minute on OpenRouter, ~3 minutes on Ollama 7b.

### Provider flexibility

`_build_llm` in the eval harness selects the LLM from environment:
```python
if LLM_PROVIDER == "openrouter":
    return OpenRouterProvider(api_key=..., model=...)
else:
    return OllamaProvider(model=...)
```

This allows **the same eval to run locally and in CI on different models** without code changes. Useful for cost/speed tradeoffs: a developer can quickly test on `ollama:7b`, while CI is more thorough on `gemini-2.5-flash` or Claude.

### Lessons learned from the first run (architect)

**Brittleness from strict enums:** the initial `ARCHITECT_OUTPUT_SCHEMA` had a strict `enum` on the `category` field (modularity, coupling, etc.). When the model tagged a finding `"maintainability"` (not in the enum), the entire synthesis was rejected at runtime. A single off-vocabulary tag **voided an otherwise-valid review**. Solution: relax `category` to a free string; keep only `severity` as an enum (the downstream filtering layer depends on it).

**Token truncation on hosted models:** OpenRouter's default `max_tokens=1024` truncates large synthesis output (the architect emits findings + recommendations + debt + nfr risks) into unparseable JSON. Only the multi-finding fixtures fail; the small `clean_layered` passes. Symptom: half the suite errors while the other half is fine. Solution: raise `max_tokens` to 4096 for synthesis-heavy agents. The aggregate test now also asserts `not errored` (fails loudly on any fixture error) instead of silently skipping, so truncation-style regressions can't pass vacuously.

**Integration validation:** the aggregate test was skipping errored fixtures (`if result.get("error"): continue`). That meant a broken eval could pass with zero fixtures if they all errored. Now it fails on any fixture error, making the test a true gate.

## Architectural review

### Strengths

- **No external dependencies:** fixtures are pure JSON (no GitHub rate limits, no Docker, no live LLM for every edit cycle).
- **Reproducible:** the same fixture produces the same score across model versions (within LLM variance) and is entirely local by default.
- **Provider-agnostic:** swappable LLM (`--env LLM_PROVIDER`) for speed/cost tradeoffs without test code changes.
- **Low maintenance:** a new fixture is just two files (`<case>/` + `labels/<case>.json`); the parametrized test auto-discovers them.

### Limitations and risks

- **Small samples:** 3 fixtures per agent type is a floor, not comprehensive coverage. Early evals are coarse gates (≥66% detection), not precision instruments.
- **Canned data brittleness:** fixtures are static JSON. If the actual tool API shape changes (e.g., `codebase_search` adds a new field), the fixtures become stale and tests still pass vacuously on the mock.
- **LLM variance:** two runs of the same eval on the same model can differ (especially on edge cases near the score thresholds). For determinism, use a fixed seed or a reasoning model; this trade-off is not yet made.
- **Schema-valid ≠ correct:** a synthesis can pass the schema but emit wrong findings (e.g., all LOWs when CRITICALs were present). Schema validity is a gate, not a measure of correctness.

## Next steps (post-acceptance)

1. **Expand coverage:** add 2–3 more architect fixtures (security, scalability, ISP) and 2–3 more reviewer fixtures (business-logic, cryptography).
2. **Runtime schema validation:** currently only the eval enforces the schema. Wire `jsonschema.validate` into the agent `run()` method itself so production catches violations.
3. **Deterministic LLM behavior:** consider a fixed seed or reasoning model for reproducible evals (tradeoff: slower, less diverse).
4. **Automated fixture generation:** explore LLM-assisted fixture synthesis (e.g., generate a "business logic bug" from a seed pattern) to scale beyond manual authorship.
