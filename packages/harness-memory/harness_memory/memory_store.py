"""PostgreSQL-backed memory store with Redis hot-read cache and Ollama embeddings."""
import json
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from typing import Any

import asyncpg
import numpy as np
import redis.asyncio as aioredis

BASE_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace    TEXT NOT NULL,
    key          TEXT NOT NULL,
    memory_type  TEXT NOT NULL DEFAULT 'episodic',
    value        JSONB NOT NULL,
    source_ids   UUID[],
    embedding    vector({dim}),
    confidence   FLOAT DEFAULT 1.0,
    consolidated BOOL DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT now(),
    expires_at   TIMESTAMPTZ,
    UNIQUE (namespace, key)
);
"""


def _table_name_for_dim(dim: int) -> str:
    """Dimension-versioned table name, e.g. memory_items_1536.

    Switching embedding models (or dimension) never drops or mutates a
    previously-created table — it just resolves reads/writes to a different
    one. Old dimension tables are left intact and still queryable directly.
    """
    return f"memory_items_{int(dim)}"


class PostgresMemoryStore:
    _embed_dim_cache: dict[str, int] = {}  # keyed by model name; shared across instances

    def __init__(
        self,
        pg_dsn: str,
        redis_url: str,
        embed_model: str,
        ollama_host: str,
    ) -> None:
        self._pg_dsn = pg_dsn
        self._redis_url = redis_url
        self._embed_model = embed_model
        self._ollama_host = ollama_host.rstrip("/")
        self._pool: asyncpg.Pool | None = None
        self._redis: aioredis.Redis | None = None
        self.cache_hits: int = 0
        self._table: str | None = None

    async def _ensure_connections(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._pg_dsn, min_size=1, max_size=5)
        if self._redis is None:
            self._redis = aioredis.from_url(self._redis_url, decode_responses=False)

    async def _resolve_embedding_dim(self) -> int:
        if self._embed_model not in PostgresMemoryStore._embed_dim_cache:
            sample = await self._embed("setup")
            PostgresMemoryStore._embed_dim_cache[self._embed_model] = len(sample)
        return PostgresMemoryStore._embed_dim_cache[self._embed_model]

    async def _ensure_table(self, dim: int) -> str:
        """Create the dimension-versioned table for `dim` if it doesn't exist yet.

        Never drops or truncates an existing table — a dimension change simply
        resolves to a different table name, so data written under a previous
        embedding dimension is left fully intact and queryable.
        """
        table = _table_name_for_dim(dim)
        async with self._pool.acquire() as conn:
            await conn.execute(BASE_CREATE_TABLE_SQL.format(table=table, dim=dim))
        return table

    async def setup(self) -> None:
        await self._ensure_connections()
        dim = await self._resolve_embedding_dim()
        self._table = await self._ensure_table(dim)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    # ------------------------------------------------------------------
    # Public Protocol methods
    # ------------------------------------------------------------------

    async def write(
        self,
        namespace: str,
        key: str,
        value: dict,
        ttl_hours: float | None = None,
        *,
        memory_type: str = "episodic",
        _expires_at: datetime | None = None,
        _embedding_text: str | None = None,
    ) -> None:
        if _expires_at is not None:
            expires_at = _expires_at
        elif ttl_hours is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        else:
            expires_at = None

        embedding = await self._embed(_embedding_text if _embedding_text is not None else json.dumps(value))

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table}
                    (namespace, key, memory_type, value, embedding, expires_at, consolidated)
                VALUES ($1, $2, $3, $4::jsonb, $5::vector, $6, FALSE)
                ON CONFLICT (namespace, key)
                DO UPDATE SET
                    memory_type  = EXCLUDED.memory_type,
                    value        = EXCLUDED.value,
                    embedding    = EXCLUDED.embedding,
                    expires_at   = EXCLUDED.expires_at,
                    consolidated = FALSE,
                    created_at   = now()
                """,
                namespace, key, memory_type,
                json.dumps(value),
                self._vec_to_pg(embedding),
                expires_at,
            )

        # Invalidate Redis cache for this key on write
        await self._redis.delete(self._redis_key(namespace, key))

    async def read(self, namespace: str, key: str) -> dict | None:
        rk = self._redis_key(namespace, key)
        cached = await self._redis.get(rk)
        if cached is not None:
            self.cache_hits += 1
            return json.loads(cached)

        row = await self._pg_read(namespace, key)
        if row is None:
            return None

        # Populate Redis cache (1 hour default)
        await self._redis.set(rk, json.dumps(row), ex=3600)
        return row

    async def search(
        self, namespace: str, query: str, top_k: int = 5, min_score: float | None = None
    ) -> list[dict]:
        """Top-K nearest-neighbor search, optionally filtered by relevance.

        When `min_score` is set, rows scoring below it are excluded
        server-side (in the SQL WHERE clause) rather than post-filtered in
        Python, so `LIMIT top_k` still returns the K most relevant rows
        instead of K rows that are then filtered down further.
        """
        embedding = await self._embed(query)
        vec_str = self._vec_to_pg(embedding)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT key, value,
                       1 - (embedding <=> $1::vector) AS score
                FROM {self._table}
                WHERE namespace = $2
                  AND (expires_at IS NULL OR expires_at > now())
                  AND embedding IS NOT NULL
                  AND ($4::float8 IS NULL OR (1 - (embedding <=> $1::vector)) >= $4)
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                vec_str, namespace, top_k, min_score,
            )
        return [{"key": r["key"], "value": json.loads(r["value"]), "score": r["score"]} for r in rows]

    async def delete(self, namespace: str, key: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {self._table} WHERE namespace = $1 AND key = $2",
                namespace, key,
            )
        await self._redis.delete(self._redis_key(namespace, key))

    # ------------------------------------------------------------------
    # Internal / test helpers
    # ------------------------------------------------------------------

    async def _truncate(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {self._table}")

    async def _pg_read(self, namespace: str, key: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT value FROM {self._table}
                WHERE namespace = $1 AND key = $2
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                namespace, key,
            )
        return json.loads(row["value"]) if row else None

    async def _get_raw(self, namespace: str, key: str) -> dict | None:
        """Return raw row dict including memory_type and consolidated flag."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT namespace, key, memory_type, value, consolidated, expires_at
                FROM {self._table}
                WHERE namespace = $1 AND key = $2
                """,
                namespace, key,
            )
        if row is None:
            return None
        return {
            "namespace": row["namespace"],
            "key": row["key"],
            "memory_type": row["memory_type"],
            "value": json.loads(row["value"]),
            "consolidated": row["consolidated"],
            "expires_at": row["expires_at"],
        }

    async def _list_by_type(self, namespace: str, memory_type: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT key, value, source_ids
                FROM {self._table}
                WHERE namespace = $1 AND memory_type = $2
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                namespace, memory_type,
            )
        return [
            {"key": r["key"], "value": json.loads(r["value"]), "source_ids": r["source_ids"]}
            for r in rows
        ]

    async def _write_semantic(
        self,
        namespace: str,
        key: str,
        value: dict,
        source_ids: list[str] | None = None,
    ) -> None:
        """Write a semantic memory item produced by the consolidation worker."""
        embedding = await self._embed(json.dumps(value))
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table}
                    (namespace, key, memory_type, value, embedding, source_ids, consolidated)
                VALUES ($1, $2, 'semantic', $3::jsonb, $4::vector, $5, TRUE)
                ON CONFLICT (namespace, key)
                DO UPDATE SET
                    memory_type  = 'semantic',
                    value        = EXCLUDED.value,
                    embedding    = EXCLUDED.embedding,
                    source_ids   = EXCLUDED.source_ids,
                    consolidated = TRUE,
                    created_at   = now()
                """,
                namespace, key,
                json.dumps(value),
                self._vec_to_pg(embedding),
                source_ids,
            )

    async def _mark_consolidated(self, namespace: str, key: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {self._table} SET consolidated = TRUE WHERE namespace = $1 AND key = $2",
                namespace, key,
            )

    async def _fetch_unconsolidated_episodic(self, namespace: str) -> list[dict]:
        """Return unconsolidated episodic items with their embeddings."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id::text, key, value, embedding::text
                FROM {self._table}
                WHERE namespace = $1
                  AND memory_type = 'episodic'
                  AND consolidated = FALSE
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                namespace,
            )
        return [
            {
                "id": r["id"],
                "key": r["key"],
                "value": json.loads(r["value"]),
                "embedding": self._pg_to_vec(r["embedding"]) if r["embedding"] else None,
            }
            for r in rows
        ]

    async def _delete_expired(self, namespace: str) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {self._table} WHERE namespace = $1 AND expires_at IS NOT NULL AND expires_at <= now()",
                namespace,
            )
        # asyncpg returns "DELETE n"
        return int(result.split()[-1])

    # ------------------------------------------------------------------
    # Embedding helpers
    # ------------------------------------------------------------------

    async def _embed(self, text: str) -> np.ndarray:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._ollama_host}/api/embed",
                json={"model": self._embed_model, "input": text},
            )
            resp.raise_for_status()
        data = resp.json()
        return np.array(data["embeddings"][0], dtype=np.float32)

    @staticmethod
    def _vec_to_pg(vec: np.ndarray) -> str:
        return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"

    @staticmethod
    def _pg_to_vec(s: str) -> np.ndarray:
        s = s.strip("[]")
        return np.array([float(x) for x in s.split(",")], dtype=np.float32)

    def _redis_key(self, namespace: str, key: str) -> str:
        # Scoped by table (which encodes the active embedding dimension) so a
        # hot-read cache entry from one dimension's table can never be served
        # back for a different dimension's store after a dimension change.
        return f"memory:{self._table}:{namespace}:{key}"
