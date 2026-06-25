---
title: "Core semantic cache — lookup, write, threshold, TTL"
status: ready-for-agent
type: AFK
---

## Parent

[Semantic Response Cache PRD](../PRD.md) — Slice 1.

## What to build

Add a semantic cache layer to `DynamicSREAgent` that sits in front of the ReAct loop. Before the agent begins its investigation, it embeds the incoming task and searches a dedicated `"cache"` namespace in `PostgresMemoryStore`. A hit above the similarity threshold returns the stored `agent_output` directly — no LLM calls, no tool invocations. On a miss the agent runs normally; on successful completion the result is written back to the cache.

End-to-end behaviour: a task submitted twice (or with a semantically equivalent variant) returns the stored result on the second call with `cache_hit: True` in the `AgentState`. Cache hits skip `_report_llm_usage` so Prometheus token metrics stay accurate. The cache path is entirely gated on `self.memory is not None` — no store means no cache, agent runs normally.

New constructor params (with defaults):
- `cache_threshold: float = 0.92` — minimum cosine similarity for a hit
- `cache_ttl_seconds: int = 86400` — entry lifetime (0 = no expiry)

New private methods on `DynamicSREAgent`:
- `_cache_lookup(task: str) -> AgentState | None` — embeds task, searches `"cache"` namespace, returns stored state on hit or `None` on miss
- `_cache_write(state: AgentState, agent_output: dict) -> None` — writes result to `"cache"` namespace with TTL; called from the existing `_save_memory` path on successful completion only (no error in result)

Cache key format: `f"cache:{hash(task)}"` — used as the `key` arg to `PostgresMemoryStore.write()` so exact-match Redis hits are fast on repeated identical tasks. pgvector handles near-identical variants.

`_cache_lookup` is called at the top of `run()`, before `_load_formula` and `_load_memory`. On a hit, `run()` returns immediately with `{**cached_state, "cache_hit": True}` without calling `_report_llm_usage`.

Follow TDD: write the test file first, confirm red, then implement.

## Acceptance criteria

- [ ] A mock store returning a result with score ≥ 0.92 causes `run()` to return early with `cache_hit: True` and the stored `agent_output`
- [ ] A mock store returning a result with score < 0.92 does not trigger a cache hit — the ReAct loop runs normally
- [ ] A mock store returning an empty result runs the ReAct loop normally
- [ ] A failed agent run (error present in result) does not write to the cache
- [ ] `_report_llm_usage` is not called when a cache hit is returned
- [ ] `cache_threshold` and `cache_ttl_seconds` are configurable on the constructor and respected at runtime
- [ ] Cache path is entirely skipped when `memory_store=None`; agent behaves identically to pre-cache behaviour
- [ ] Unit tests pass without a running `PostgresMemoryStore` (mock store only)
- [ ] Integration test: submit task A, let agent complete, submit task A again — second call returns `cache_hit: True` with matching `agent_output`
- [ ] Integration test: submit task A, submit a semantically equivalent task B — second call returns `cache_hit: True` (requires live embedding + pgvector)
- [ ] Integration test: write a cache entry with `expires_at` in the past — treated as a miss
- [ ] Code health score remains ≥ 9
- [ ] Docs updated (`CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, `PROGRESS.md`) when green

## Blocked by

None — can start immediately.
