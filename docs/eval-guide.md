# Eval suites

Run with `pytest -m eval -v -s` (requires live Ollama or `LLM_PROVIDER=openrouter`).

## Reviewer eval suite

`eval-fixtures/` contains labeled diffs for benchmarking the `CodeReviewerAgent` against known security bugs without a running Docker stack:

```bash
pytest -m eval -v -s   # runs against live Ollama; slow (~2 min for 7b model)
```

**Fixture format:**
- `eval-fixtures/diffs/<name>.diff` — synthetic git diff
- `eval-fixtures/labels/<name>.json` — `{"verdict": "pass|fail", "must_flag": [{"pattern": "...", "min_severity": "CRITICAL"}]}`

**Pass bars:** verdict accuracy ≥ 80%, average recall ≥ 60% across all fixtures.

**Adding fixtures:** write a `.diff` + matching `.json` in `eval-fixtures/`. The parametrized test picks them up automatically. When the model uses different phrasing than your pattern (e.g. "role enforcement" instead of "authorization"), update the label pattern — the fixture labels are as much under test as the model.

Eval tests use a `_MockGateway` that returns the fixture diff for `git_diff` and empty findings for `run_linter`, bypassing the live stack entirely.

## Architect eval suite

`eval-fixtures/architecture/` benchmarks the four-phase `ArchitectAgent` against fixture "repositories" expressed as **canned tool responses** — no live stack, no GitHub. `test_eval_architect.py` (`pytest -m eval`) is the architect counterpart to the reviewer eval.

**Fixture format** — one directory per case plus a matching label:
- `eval-fixtures/architecture/<case>/recon.json` — `codebase_search` result for the reconnaissance phase (query contains "directory structure")
- `.../hotspots.json` — `codebase_hotspots` result (a JSON list)
- `.../files.json` — `codebase_search` result for flow_trace (query contains "entry point")
- `.../interfaces.json` — `codebase_search` result for abstraction_analysis (any other query)
- `.../adrs.json` — `adr_read` result for synthesis
- `eval-fixtures/architecture/labels/<case>.json` — `{"expect_high_severity": bool, "must_flag": [{"pattern": "...", "category": "...", "min_severity": "HIGH"}]}`

The `_MockGateway` routes `codebase_search` to the right file **by keyword in the query** (the agent issues a different query per phase). Returns shapes mirror the real `github-mcp` / `review-server` tools: `codebase_search` → `{"results": [{"path", "matches": [{"fragment"}]}]}`, `codebase_hotspots` → a list, `adr_read` → `{"adrs": [...]}`.

**Pass bars:** schema validity 100%, detection accuracy ≥ 66%, average recall ≥ 50%. Detection = a smell fixture raises a HIGH+ finding; the control (`clean_layered`) must not raise a CRITICAL. Bars are coarse for a small live-LLM set — tune as fixtures grow.

**`must_flag` matching** is against HIGH+ findings only (`title`+`message`+`location`+`category`), filtered by `min_severity`. As with the reviewer eval, when the model phrases a finding differently than your pattern, fix the label — the labels are as much under test as the model.

**Schema dimension:** the eval validates synthesis output against `ARCHITECT_OUTPUT_SCHEMA` (`harness_agents/types.py`) — the architecture-**review-report** shape the prompt emits (`findings`/`recommendations`/`technical_debt_hotspots`/...), not the old ADR shape. The synthesis phase **also validates at runtime**: `_phase_synthesis` passes `_validate_synthesis` to `_llm_retry`, so a schema-invalid synthesis is fed back to the model and retried (up to `MAX_ITERATIONS`); if it never validates, `run()` returns `error.code = "invalid_output"`. Only `severity` is enum-constrained — `category` is a free string (the prompt suggests a vocabulary, but an off-list tag must not void an otherwise-valid review).

**CI:** `.github/workflows/architect-eval.yml` runs this suite on PRs that touch the architect, its prompt, the schema, or the fixtures. Runners have no GPU, so it uses `LLM_PROVIDER=openrouter` with `google/gemini-2.5-flash` (`_build_llm` selects the provider from env; defaults to Ollama locally). Requires an `OPENROUTER_API_KEY` repo secret — the job skips with a warning if it's unset.

**Gotcha — OpenRouter `max_tokens` truncation:** the synthesis report (findings + recommendations + debt + nfr risks) is large. The provider default of 1024 output tokens truncates it into unparseable JSON — symptom is synthesis failing with "could not parse" only on the *multi-finding* fixtures while the small `clean_layered` passes. `_build_llm` sets `max_tokens=4096` (override via `LLM_MAX_TOKENS`). The aggregate test treats an errored fixture as a failure (`assert not errored`) rather than skipping it, so a regression like this can't pass vacuously.
