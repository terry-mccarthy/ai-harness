# PRD: Semantic Response Cache for SRE Agent

Status: ready-for-agent

## Problem Statement

Every task submitted to the `DynamicSREAgent` triggers a full ReAct loop regardless of whether a nearly identical task has been successfully resolved before. A runbook lookup for "what is the escalation path for database incidents?" runs the same multi-turn LLM conversation as the first time it was asked, consuming the same tokens against the same (possibly expensive) model. In a production deployment where many engineers query the agent with semantically equivalent tasks, this is pure waste: the harness already stores incident outcomes in `PostgresMemoryStore` and injects them as context, but it never uses them to skip the loop entirely.

The cost multiplier is highest at the top of the model tier ladder. A high-tier incident that was resolved yesterday and stored in memory will still trigger a full Sonnet (or equivalent) conversation today, even if the stored answer is authoritative.

## Solution

Add a semantic cache layer that sits in front of the ReAct loop. Before the agent begins its investigation, it embeds the incoming task and searches a dedicated `"cache"` namespace in `PostgresMemoryStore`. If the top result exceeds a high-confidence similarity threshold, the stored `agent_output` is returned directly as the response — no LLM calls, no tool invocations. On a cache miss, the agent runs normally; on successful completion, the result is written to the cache namespace for future hits.

The cache is built on top of infrastructure that already exists: `PostgresMemoryStore` (pgvector similarity search), the `nomic-embed-text` embedding model, and Redis for fast exact-key lookups. No new services are required.

## User Stories

1. As an SRE, I want repeated runbook lookups to return instantly, so that I am not charged for the same LLM call multiple times.
2. As an SRE, I want repeated incident queries to return a cached answer, so that I get a consistent response across team members asking the same question.
3. As an operator, I want cache hits to be identifiable in the agent output, so that I know when I am reading a cached response vs. a live investigation.
4. As an operator, I want the cache threshold to be configurable, so that I can tune confidence vs. freshness for my deployment.
5. As an operator, I want cached entries to expire after a configurable TTL, so that stale incident responses do not persist indefinitely.
6. As an operator, I want the cache to be skippable per-request, so that I can force a fresh investigation when needed.
7. As a developer, I want the cache to use the same `PostgresMemoryStore` interface as the existing memory and runbook layers, so that I do not introduce a new storage dependency.
8. As a developer, I want the cache to degrade gracefully when `PostgresMemoryStore` is unavailable, so that a store outage does not block the agent.
9. As a developer, I want the cache write to happen only on successful agent completion, so that failed or errored investigations are never cached.
10. As a developer, I want the cache to use a dedicated namespace (`"cache"`), so that it does not collide with the `"runbooks"` and `"logs"` namespaces used by the retrieval layer.
11. As a developer, I want cache hits to skip the `_report_llm_usage` call, so that Prometheus token metrics remain accurate.
12. As a developer, I want unit tests for the cache logic that do not require a running `PostgresMemoryStore`, so that the test suite remains fast.
13. As a developer, I want integration tests that verify a second identical task gets a cache hit, so that the end-to-end flow is proven.
14. As an operator, I want the demo script to report cache hits in the token ledger as zero tokens, so that the savings comparison is accurate.
15. As an operator, I want the comparison report to show a "cache hits" count alongside token savings, so that the impact of caching is visible.

## Implementation Decisions

- **Cache namespace**: `"cache"` in `PostgresMemoryStore`. Separate from `"runbooks"` and `"logs"` to avoid cross-namespace pollution. The existing `UNIQUE (namespace, key)` constraint and TTL/expiry columns are reused without schema changes.

- **Cache key**: `f"cache:{hash(task)}"` — a deterministic key derived from the raw task string, used for exact-match Redis lookups on repeated identical tasks. The pgvector index handles fuzzy/semantic lookups for near-identical tasks.

- **Similarity threshold**: 0.92 (versus 0.80 for memory consolidation clustering). The higher bar is intentional — a cache hit must be authoritative, not just topically related. Configurable via `cache_threshold` on the agent constructor (default 0.92).

- **TTL**: Cached entries expire after 24 hours by default. Configurable via `cache_ttl_seconds` (0 = no expiry). The `expires_at` column in `memory_items` is already used by `PostgresMemoryStore.search()` to filter expired entries.

- **Integration point**: a `_cache_lookup(task)` method on `DynamicSREAgent`, called at the top of `run()` before `_load_formula` and `_load_memory`. Returns the cached `AgentState` on hit or `None` on miss. A `_cache_write(state, agent_output)` method is called from the existing `_save_memory` path on successful completion.

- **Cache-hit flag**: the returned `AgentState` on a hit includes `"cache_hit": True` at the top level. This lets the demo script log it and record zero tokens in the `TokenLedger`.

- **Force-refresh**: `AgentState` gains an optional `"force_refresh": bool` key (default False). When True, `_cache_lookup` returns `None` unconditionally and skips the write.

- **`memory_store` is optional**: the cache path is gated on `self.memory is not None`, exactly as `_load_memory` and `_save_memory` are today. No store = no cache, agent runs normally.

- **Token ledger**: `_report_llm_usage` is not called on cache hits (token counts are zero). The demo script checks `result.get("cache_hit")` and records `(0, 0)` tokens, logging a distinct label.

- **No new dependency**: `PostgresMemoryStore`, Redis, and `nomic-embed-text` are already wired into the stack. The cache is a new usage pattern of existing infrastructure.

## Testing Decisions

A good test exercises the external contract of the cache: given two semantically equivalent tasks submitted in sequence, the second should return a cache hit with zero token usage and the same `agent_output` shape as the first. Tests must not assert on embedding vectors, similarity scores, or internal method calls.

**Unit tests** (`test_unit_semantic_cache.py`):
- A mock `PostgresMemoryStore` that returns a high-score hit triggers a cache-hit return from `run()`.
- A mock store that returns a low-score hit (below threshold) does not trigger a cache hit.
- A mock store that returns an empty result runs the ReAct loop normally.
- `force_refresh=True` in `AgentState` skips the cache lookup even when the store would return a hit.
- A cache hit returns `"cache_hit": True` in the result.
- A cache hit does not call `_report_llm_usage`.
- A failed agent run (error in result) does not write to the cache.

**Integration tests** (extend `test_phase2_memory.py` or a new `test_semantic_cache.py`):
- Submit task A, let the agent complete, then submit task A again. Second call returns a cache hit.
- Submit task A, then submit a semantically equivalent task B. Second call returns a cache hit (requires live embedding + pgvector).
- Submit task A with `force_refresh=True`. Runs full loop even if cache would hit.
- Expired cache entry (TTL in the past) is treated as a miss.

Prior art: `test_phase2_memory.py` shows the pattern for spinning up `PostgresMemoryStore` in integration tests with a real PG connection.

## Out of Scope

- Cache invalidation by tool output (e.g., invalidate when a runbook is updated).
- Cache warming (pre-populating cache entries from the existing memory namespace).
- Per-agent-role cache namespaces (all SRE agent instances share one `"cache"` namespace).
- Cross-agent caching (the cache is only wired into `DynamicSREAgent`; other agents are unaffected).
- Cache metrics in Prometheus (governance already tracks token usage; cache hits are visible as zero-token calls there).
- Distributed cache invalidation across multiple agent instances.

## Further Notes

The 0.92 threshold is a starting point. Calibrate against real task logs: use the existing embedding dimension benchmarks in `CLAUDE.md` (same-topic pairs score 0.82–0.93) to understand where the distribution lies for your runbook corpus. A threshold too low will return wrong cached answers; too high will produce few hits.

The `"cache"` namespace must be seeded with zero entries on first run — `PostgresMemoryStore` handles this automatically (no DDL migration needed; the namespace is a text column, not a table).

The cache is the highest-value layer in the cost governance stack: a hit saves 100% of tokens for that task, versus the ~45% average saving from model selection alone. The two layers compose — a cache miss routes to the cheapest capable model via `build_role_llm()`.
