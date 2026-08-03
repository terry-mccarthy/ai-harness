# LLM Providers

## LLM provider factory — `build_llm_from_env()`

All agents and scripts must construct LLM providers via the canonical factory in `harness_agents/llm.py`. **Do not construct `OllamaProvider`, `GeminiProvider`, or `OpenRouterProvider` directly** in scripts or tests.

```python
from harness_agents.llm import build_llm_from_env

# env-driven (LLM_PROVIDER, OLLAMA_MODEL, etc.)
provider = build_llm_from_env()

# kwarg overrides
provider = build_llm_from_env(model="qwen3.6:27b", max_tokens=2048)

# config dict layer (from DB or any source) — same schema as server_config JSONB
provider = build_llm_from_env(config={"llm_provider": "gemini", "gemini": {"model": "gemini-2.5-flash"}})
```

Resolution order: **kwarg > config dict > env var > default**.

Provider dispatch uses `_PROVIDER_BUILDERS` dict; adding a new provider means adding a `_build_<name>()` function and an entry there.

**`harness-agents` has no asyncpg dependency.** If you need to read the `server_config` table to populate `config=`, do it in the calling script (see `scripts/demo_sre.py:_load_llm_config_from_pg()`). The factory itself stays DB-agnostic.

## Model tuning (Ollama)

Three env vars control the LLM call in `review-server`. Set them in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Model name. 32b gives much better findings; 7b is faster for iteration. |
| `OLLAMA_NUM_CTX` | `8192` | Context window in tokens. Large diffs need more — default Ollama is 2048 which truncates real diffs. |
| `OLLAMA_TEMPERATURE` | `0.1` | Low = deterministic JSON. Don't raise above 0.3 or schema failures increase. |
| `OLLAMA_NUM_PREDICT` | `1024` | Max tokens to generate. Raise to 2048 for diffs with many findings. |

After changing `.env`, restart the container (no rebuild needed):
```bash
docker compose up -d --no-deps review-server
```

**Thinking models (qwen3 and similar):** Models that emit `<think>...</think>` blocks before their answer are handled automatically — the reviewer strips them before JSON parsing. `qwen3.6:27b` uses this path and reasons more carefully but is significantly slower.

**Speed vs quality on Apple Silicon:**
- `qwen2.5-coder:7b` — ~10s, misses subtle bugs
- `qwen2.5-coder:32b` — ~60–90s, catches most issues
- `qwen3.6:27b` — ~2–5 min, best reasoning (thinking mode)

## Ollama from inside Docker

The `review-server` container needs to reach Ollama on the host. Docker Desktop exposes this via `host.docker.internal`:

```yaml
environment:
  OLLAMA_HOST: http://host.docker.internal:11434
```

## OllamaProvider timeout

`OllamaProvider` now enforces a **120-second timeout** on embeddings and LLM calls. If Ollama is memory-starved (e.g., 32b model loaded while running large test suite), requests will timeout after 120s rather than hanging forever.

## OpenRouter provider

Set `LLM_PROVIDER=openrouter` to route all LLM calls through OpenRouter, which proxies dozens of hosted models — useful when local Ollama context limits are too small for large diffs.

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | *(required)* | Get from openrouter.ai/keys |
| `OPENROUTER_MODEL` | `anthropic/claude-3.5-sonnet` | Any slug from openrouter.ai/models |
| `LLM_TEMPERATURE` | `0.1` | Shared with other providers |
| `LLM_MAX_TOKENS` | `1024` | Output token cap — raise for large diffs |

Recommended large-context models via OpenRouter:
- `anthropic/claude-3.5-sonnet` — 200K context, strong reasoning
- `google/gemini-2.5-flash` — 1M context, very fast
- `openai/gpt-4o` — 128K context, reliable JSON output

```bash
# .env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet
LLM_MAX_TOKENS=2048
```

After changing `.env`, restart the container (no rebuild needed):
```bash
docker compose up -d --no-deps review-server
```

**Implementation note:** `OpenRouterProvider` uses the `openai` Python SDK with `base_url="https://openrouter.ai/api/v1"` — OpenRouter is OpenAI API-compatible. The class is in `packages/harness-agents/harness_agents/llm.py`.

**o-series reasoning models:** `temperature` is silently omitted for models matching `openai/o\d` (e.g. `openai/o1`, `openai/o4-mini`) — these models reject the parameter with a 400 error.

**Error handling:** provider errors (auth failure, rate limit, empty choices from content filter) are caught in the reviewer's retry loop and returned as structured `{"code": "provider_error", "reason": "..."}` agent errors. The retry loop does not retry provider errors.

**`OPENROUTER_API_KEY` validation:** the key is `.strip()`-ed before the empty check, so a whitespace-only value is caught at startup rather than producing a 401 at review time. Unknown provider names raise `ValueError` with the supported list (`ollama`, `gemini`, `openrouter`) — previously they silently fell through to Ollama.

## Runtime LLM config via Postgres (`server_config` table)

The review-server's `PUT /config` endpoint writes LLM settings to the `server_config` Postgres table (JSONB column `config`). Any process that reads from this table at startup will pick up the same provider/model without a restart.

**`demo_sre.py`** does this: it calls `_load_llm_config_from_pg(pg_dsn)` before constructing the agent, so `make demo-sre` automatically uses whichever LLM the review-server is configured to use.

The table schema:
```json
{
  "llm_provider": "gemini",
  "embedding_provider": "ollama",
  "gemini": { "model": "gemini-2.5-flash", "api_key": "..." },
  "ollama": { "model": "qwen2.5-coder:7b" },
  "openrouter": { "model": "anthropic/claude-3.5-sonnet" }
}
```

Only the active provider's sub-dict is used; the others are ignored. `llm_provider` and `embedding_provider` are independent keys — e.g. chat can run on `gemini` while embeddings stay on local `ollama` (the `ollama` sub-dict is shared; embeddings read `model`/`host` from it, chat reads `model`/`num_ctx`/etc. from the same sub-dict).

## Embedding provider factory — `build_embedding_provider_from_env()`

`PostgresMemoryStore` (`packages/harness-memory`) takes an `EmbeddingProvider` instance instead of raw `embed_model`/`ollama_host` strings — the same seam pattern as `build_llm_from_env()` above, but for embeddings. The factory lives in `harness_memory/embedding_provider.py` (not `harness_agents`) since `harness_memory` is the only package that needs embeddings and already owns `httpx`/`asyncpg`.

```python
from harness_memory.embedding_provider import build_embedding_provider_from_env, OllamaEmbeddingProvider

# env-driven (EMBEDDING_PROVIDER, EMBED_MODEL, OLLAMA_HOST)
provider = build_embedding_provider_from_env()

# kwarg overrides
provider = build_embedding_provider_from_env(model="mxbai-embed-large")

# config dict layer — same server_config JSONB schema as build_llm_from_env,
# reading the "embedding_provider" key instead of "llm_provider"
provider = build_embedding_provider_from_env(config={"embedding_provider": "ollama", "ollama": {"model": "nomic-embed-text"}})

store = PostgresMemoryStore(pg_dsn, redis_url, provider)
```

Resolution order: **kwarg > config dict > env var > default** (same as `build_llm_from_env`).

**Only `ollama` is implemented today.** Unknown provider names (e.g. `gemini`, `openrouter`) raise `ValueError` listing only what's actually supported — do not construct `OllamaEmbeddingProvider` directly in production/script call sites; use the factory so `EMBEDDING_PROVIDER`/`server_config` selection works. Tests that want a deterministic, non-env-driven store should construct `OllamaEmbeddingProvider(host, model)` explicitly instead of going through the factory, to avoid environment leakage between test runs (see `packages/harness-tests/test_phase2_memory.py`).

**Gemini and OpenRouter embedding backends are deferred** — OpenRouter added an OpenAI-shaped `POST /api/v1/embeddings` endpoint (proxying models like `openai/text-embedding-3-small`), so both are viable hosted options, but implementations are only built when an actual hosted deployment needs a second backend (see `.scratch/sre-agent-enhancement/issues/08-pluggable-embedding-provider.md`).

## PG config persistence gotcha

The review-server stores runtime config (PUT /config) in Postgres `server_config` table. `.env` has `PG_DSN=localhost:5432` — works for host-side Python but **breaks inside Docker**. In docker-compose.yml, `PG_DSN` is hardcoded to `postgres` hostname (service name) to avoid the `.env` override.

Even with correct PG_DSN, FastMCP's streamable-http transport only runs the lifespan **per MCP request**, not at server startup. Custom routes (GET /config) bypass the lifespan entirely. Config loads lazily on first MCP call (e.g. `initialize`). Until that call, `_CONFIG` shows defaults.

`_init_pg_pool` has 5-attempt retry with backoff because the review-server has no `depends_on: postgres`.
