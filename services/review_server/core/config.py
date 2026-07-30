"""Runtime configuration store and infrastructure for review_server.

Carved out of server.py (issue #06): env-var resolution helpers, the
in-memory `_CONFIG` override store, its postgres-backed persistence (the
`server_config` table), and credential sanitization for the `/config` HTTP
endpoints. The endpoints themselves (GET/PUT `/config`) stay in server.py —
this module only owns the state and the helpers they call.
"""
import asyncio
import json
import logging
import os

import asyncpg
from starlette.requests import Request

_PG_POOL: asyncpg.Pool | None = None


async def _init_pg_pool() -> None:
    global _PG_POOL
    dsn = os.environ.get("PG_DSN", "postgresql://harness:harness@localhost:5432/harness")
    if not dsn:
        return
    for attempt in range(1, 6):
        try:
            _PG_POOL = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
            logging.info("connected to postgres config store")
            return
        except Exception:
            if attempt < 5:
                wait = attempt * 2
                logging.warning(
                    "pg connect attempt %d/5 failed, retrying in %ds...", attempt, wait
                )
                await asyncio.sleep(wait)
            else:
                logging.warning(
                    "config persistence unavailable — pg not reachable after 5 attempts",
                    exc_info=True,
                )


async def _close_pg_pool() -> None:
    global _PG_POOL
    if _PG_POOL:
        await _PG_POOL.close()
        _PG_POOL = None


async def _ensure_config_table() -> None:
    if not _PG_POOL:
        return
    async with _PG_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS server_config (
                id         INTEGER PRIMARY KEY DEFAULT 1,
                config     JSONB NOT NULL DEFAULT '{}',
                updated_at TIMESTAMPTZ DEFAULT now(),
                CONSTRAINT single_row CHECK (id = 1)
            )
        """)
        await conn.execute("""
            INSERT INTO server_config (id, config)
            VALUES (1, '{}')
            ON CONFLICT (id) DO NOTHING
        """)


# ---------------------------------------------------------------------------
# Runtime config store — set via PUT /config, persisted in postgres server_config
# table. Loaded at startup via lifespan hook; saved on every PUT /config.
# Each provider sub-dict holds typed overrides that take precedence over env vars.
# Setting a value to None (or omitting the key) falls through to the env var / default.
# ---------------------------------------------------------------------------
_CONFIG: dict = {
    "llm_provider": None,   # overrides LLM_PROVIDER env var
    "ollama": {},
    "gemini": {},
    "openrouter": {},
}

_SENSITIVE_KEYS = {"api_key", "client_secret", "secret"}


_ENV_CFG = {
    "llm_provider": ("LLM_PROVIDER", "ollama"),
    "ollama": {
        "model": ("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        "host": ("OLLAMA_HOST", "http://localhost:11434"),
        "num_ctx": ("OLLAMA_NUM_CTX", 8192),
        "temperature": ("OLLAMA_TEMPERATURE", 0.1),
        "num_predict": ("OLLAMA_NUM_PREDICT", 1024),
    },
    "gemini": {
        "model": ("GEMINI_MODEL", "gemini-2.5-flash"),
        "api_key": ("GEMINI_API_KEY", None),
    },
    "openrouter": {
        "model": ("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
        "api_key": ("OPENROUTER_API_KEY", None),
    },
}


def _apply_config_overrides(overrides: dict) -> None:
    if "llm_provider" in overrides:
        _CONFIG["llm_provider"] = overrides["llm_provider"]
    for prov in ("ollama", "gemini", "openrouter"):
        if prov in overrides:
            _CONFIG[prov] = overrides[prov]


async def _load_config_from_pg() -> None:
    if not _PG_POOL:
        return
    async with _PG_POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT config FROM server_config WHERE id = 1")
    if row and row["config"]:
        _apply_config_overrides(json.loads(row["config"]))


async def _save_config_to_pg() -> None:
    if not _PG_POOL:
        return
    payload: dict = {}
    if _CONFIG["llm_provider"] is not None:
        payload["llm_provider"] = _CONFIG["llm_provider"]
    for prov in ("ollama", "gemini", "openrouter"):
        if _CONFIG.get(prov):
            payload[prov] = _CONFIG[prov]
    async with _PG_POOL.acquire() as conn:
        await conn.execute(
            "UPDATE server_config SET config = $1::jsonb, updated_at = now() WHERE id = 1",
            json.dumps(payload),
        )


def _get_cfg(provider: str, key: str):
    """Return a config override value, or None if not set."""
    prov = _CONFIG.get(provider, {})
    if not isinstance(prov, dict):
        return None
    return prov.get(key)


def _env_cfg() -> dict:
    result = {}
    result["llm_provider"] = _CONFIG.get("llm_provider") or os.environ.get("LLM_PROVIDER", "ollama")
    for provider in ("ollama", "gemini", "openrouter"):
        sub = {}
        for k, (env_var, default) in _ENV_CFG[provider].items():
            sub[k] = _get_cfg(provider, k) or os.environ.get(env_var, default)
        result[provider] = sub
    return result


def _should_mask(key: str, val) -> bool:
    if key.lower() not in _SENSITIVE_KEYS:
        return False
    if not isinstance(val, str):
        return False
    if not val:
        return False
    return True


def _mask_value(val: str) -> str:
    return val[:4] + "..." if len(val) > 8 else "***"


def _sanitize_cfg(val, key=""):
    """Mask sensitive values (api keys, secrets) for display."""
    if isinstance(val, dict):
        return {k: _sanitize_cfg(v, k) for k, v in val.items()}
    if _should_mask(key, val):
        return _mask_value(val)
    return val


def _check_api_key(request: Request) -> bool:
    """Return True if the request is authorised.

    When REVIEW_API_KEY is unset the endpoint is open (dev/local mode).
    When set, the request must carry 'Authorization: Bearer <key>'.
    """
    required = os.environ.get("REVIEW_API_KEY")
    if not required:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return header[len("Bearer "):] == required


def _pg_pool_connected() -> bool:
    """Whether the postgres config pool is currently connected.

    Used by server.py's lifespan hook, which imports `_init_pg_pool` (not
    `_PG_POOL` itself) — a plain name import of `_PG_POOL` would snapshot
    `None` and never observe the rebind that happens inside `_init_pg_pool`.
    """
    return _PG_POOL is not None
