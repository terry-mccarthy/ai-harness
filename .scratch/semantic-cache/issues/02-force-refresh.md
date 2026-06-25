---
title: "Force-refresh per-request"
status: ready-for-agent
type: AFK
---

## Parent

[Semantic Response Cache PRD](../PRD.md) — Slice 2.

## What to build

Add a `force_refresh` flag to `AgentState` that lets callers opt out of the cache for a specific request. When `force_refresh=True`, `_cache_lookup` returns `None` unconditionally and `_cache_write` is skipped — the full ReAct loop runs and the result is not stored.

This is a thin slice: one new optional key on `AgentState` (`total=False` already covers the default), two early-return guards in `_cache_lookup` and `_cache_write`, and one unit test.

## Acceptance criteria

- [ ] `force_refresh: bool` added to `AgentState` (optional, defaults to absent/falsy)
- [ ] When `force_refresh=True`, `_cache_lookup` returns `None` even when the store would return a high-score hit
- [ ] When `force_refresh=True`, a successful run does not write to the cache
- [ ] Unit test: mock store returns a high-score hit; `force_refresh=True` causes the ReAct loop to run and `cache_hit` is absent from the result
- [ ] Integration test: submit task A (cached), then submit task A again with `force_refresh=True` — second call runs the full loop and does not return `cache_hit: True`
- [ ] Existing cache behaviour is unaffected when `force_refresh` is absent or `False`
- [ ] Code health score remains ≥ 9

## Blocked by

- [01-core-semantic-cache.md](01-core-semantic-cache.md)
