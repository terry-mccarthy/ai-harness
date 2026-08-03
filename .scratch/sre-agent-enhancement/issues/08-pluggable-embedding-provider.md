---
title: "Pluggable embedding provider (Ollama now, Gemini/OpenRouter later)"
status: done
type: AFK
---

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — "Hosted deployment considerations"
section flagged this as a real gap, not one of the PRD's 4 numbered slices.
**Scope note:** this is cross-cutting to `harness_memory`, not SRE-specific —
`PostgresMemoryStore` backs runbooks/logs/incident-memory (this PRD),
semantic cache, consolidation, and the code-reviewer's memory namespace.
Fixing the seam here benefits all of them, not just the SRE agent. Filed here
because that's where the gap was first written down; happy to live in a more
general tracker location if one exists.

## Why

`PostgresMemoryStore._embed()` (`packages/harness-memory/harness_memory/memory_store.py:319`)
is hardwired to Ollama's `/api/embed` endpoint — no seam to swap it. The
existing chat-provider abstraction (`OllamaProvider`/`GeminiProvider`/
`OpenRouterProvider` in `harness_agents/llm.py`, dispatched via
`build_llm_from_env()`) is chat-only; embeddings were never given the same
treatment. This was flagged as a real gap in the PRD's hosted-deployment
notes but explicitly left out of scope for that PRD.

Correction to the PRD's own text while filing this: it says "note OpenRouter
does not cover embeddings" — **that's now outdated.** OpenRouter added an
OpenAI-shaped `POST /api/v1/embeddings` endpoint (verified 2026-08-03,
`openrouter.ai/docs/api_reference/embeddings`), proxying multiple providers'
embedding models (e.g. `openai/text-embedding-3-small`). Both Gemini and
OpenRouter are viable embedding backends now, not just Gemini.

## What to build

**Now:** introduce the seam and refactor the existing Ollama path behind it.
**Later (not this issue):** add `GeminiEmbeddingProvider` and
`OpenRouterEmbeddingProvider` implementations when an actual hosted
deployment needs them.

1. `EmbeddingProvider` protocol in `harness_memory` (not `harness_agents` —
   `harness_memory` already owns `httpx`/`asyncpg`, and only it needs
   embeddings; avoid a new cross-package dependency):
   ```python
   class EmbeddingProvider(Protocol):
       provider_name: str
       model_name: str
       async def embed(self, text: str) -> np.ndarray: ...
   ```
2. `OllamaEmbeddingProvider(host, model)` wrapping the current `_embed()`
   body verbatim (same `/api/embed` call, same `np.array(..., dtype=np.float32)`
   return shape) — a pure extraction, no behavior change.
3. `build_embedding_provider_from_env(provider=None, config=None, **overrides)`
   mirroring `build_llm_from_env()`'s resolution order (kwarg > config dict >
   env var > default) and `_PROVIDER_BUILDERS`-style dispatch dict, so adding
   Gemini/OpenRouter later is "add a `_build_<name>()` function and a dict
   entry," not a rearchitect. Only `ollama` needs a real builder now; unknown
   provider names should raise `ValueError` listing what's actually
   supported today (don't pretend gemini/openrouter work until they're
   built).
4. Reuse the LLM config pattern documented in `docs/dev/llm-providers.md`
   ("Runtime LLM config via Postgres") — add an `embedding_provider` key
   alongside the existing `llm_provider` key in the same `server_config`
   JSONB blob, so both are configured from one place:
   ```json
   {
     "llm_provider": "gemini",
     "embedding_provider": "ollama",
     "gemini": { "model": "gemini-2.5-flash", "api_key": "..." },
     "ollama": { "model": "nomic-embed-text" },
     "openrouter": { "model": "anthropic/claude-3.5-sonnet" }
   }
   ```
5. `PostgresMemoryStore.__init__` changes from `(pg_dsn, redis_url,
   embed_model, ollama_host)` to accepting an `EmbeddingProvider` instance
   (constructed via the factory by the caller) instead of the raw
   `embed_model`/`ollama_host` pair. `_embed()` becomes a thin call to
   `self._provider.embed(text)`. `_embed_dim_cache` keys off
   `provider.model_name` instead of the raw string, unchanged in spirit.

**Blast radius — every construction site needs updating:**
- `stub_servers/sre_server.py:41`
- `scripts/demo_sre.py:56`
- `scripts/seed_logs.py:11`
- `scripts/seed_runbooks.py:11`
- `packages/harness-tests/test_semantic_cache.py:67`
- `packages/harness-tests/test_phase2_memory.py:40,48,199,279,288`

Each currently does `PostgresMemoryStore(PG_DSN, REDIS_URL, EMBED_MODEL,
OLLAMA_HOST)` — becomes `PostgresMemoryStore(PG_DSN, REDIS_URL,
build_embedding_provider_from_env(...))` or equivalent explicit
`OllamaEmbeddingProvider(...)` construction in tests that want to pin the
provider directly.

## Acceptance criteria

- [ ] `EmbeddingProvider` protocol + `OllamaEmbeddingProvider` exist in `harness_memory`, behavior-identical to the current hardcoded path (same request shape, same return type)
- [ ] `build_embedding_provider_from_env()` exists with the same kwarg > config > env > default resolution order as `build_llm_from_env()`; unsupported provider names raise `ValueError` listing only what's actually implemented
- [ ] `PostgresMemoryStore` takes an `EmbeddingProvider` instance, not `embed_model`/`ollama_host` strings
- [ ] All 4 production call sites and all test call sites updated; existing integration tests (`test_phase2_memory.py`, `test_semantic_cache.py`) pass unchanged against real Ollama through the new seam
- [ ] `server_config` JSONB schema gains `embedding_provider` + per-provider sub-dicts, documented in `docs/dev/llm-providers.md` alongside the existing LLM config section
- [ ] Unit tests cover provider selection/resolution order with a fake `EmbeddingProvider` (no real Ollama call needed for the dispatch logic itself)
- [ ] Docs updated: `docs/dev/llm-providers.md` (or a new `docs/dev/embeddings.md`) documents the seam; PRD's "Hosted deployment considerations" section corrected re: OpenRouter now supporting embeddings

## Explicitly out of scope for this issue

- `GeminiEmbeddingProvider` / `OpenRouterEmbeddingProvider` implementations — build when a hosted deployment actually needs a second backend, not speculatively
- Migrating already-embedded data between providers/dimensions — `PostgresMemoryStore` already handles dimension changes via `_table_name_for_dim` (dimension-versioned tables, old ones left intact); provider changes are just a dimension change from that table's point of view, no new migration path needed

## Blocked by

None — orthogonal to the SRE PRD's slices, can start independently.
