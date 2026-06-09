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
CREATE TABLE IF NOT EXISTS memory_items (
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


class PostgresMemoryStore:
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

    async def setup(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._pg_dsn, min_size=1, max_size=5)
        if self._redis is None:
            self._redis = aioredis.from_url(self._redis_url, decode_responses=False)

        # Detect embedding dimension from the configured model
        sample = await self._embed("setup")
        dim = len(sample)

        create_table_sql = BASE_CREATE_TABLE_SQL.format(dim=dim)
        async with self._pool.acquire() as conn:
            # If table already exists with wrong vector dimension, drop it
            existing_dim = await conn.fetchval(
                """
                SELECT atttypmod FROM pg_attribute pa
                JOIN pg_class pc ON pa.attrelid = pc.oid
                JOIN pg_namespace pn ON pc.relnamespace = pn.oid
                WHERE pc.relname = 'memory_items' AND pa.attname = 'embedding'
                  AND pn.nspname = 'public'
                """
            )
            if existing_dim is not None and existing_dim != dim:
                await conn.execute("DROP TABLE IF EXISTS memory_items")

            await conn.execute(create_table_sql)

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
    ) -> None:
        if _expires_at is not None:
            expires_at = _expires_at
        elif ttl_hours is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        else:
            expires_at = None

        embedding = await self._embed(json.dumps(value))

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory_items
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

    async def search(self, namespace: str, query: str, top_k: int = 5) -> list[dict]:
        embedding = await self._embed(query)
        vec_str = self._vec_to_pg(embedding)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT key, value,
                       1 - (embedding <=> $1::vector) AS score
                FROM memory_items
                WHERE namespace = $2
                  AND (expires_at IS NULL OR expires_at > now())
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                vec_str, namespace, top_k,
            )
        return [{"key": r["key"], "value": json.loads(r["value"]), "score": r["score"]} for r in rows]

    async def delete(self, namespace: str, key: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memory_items WHERE namespace = $1 AND key = $2",
                namespace, key,
            )
        await self._redis.delete(self._redis_key(namespace, key))

    # ------------------------------------------------------------------
    # Internal / test helpers
    # ------------------------------------------------------------------

    async def _truncate(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM memory_items")

    async def _pg_read(self, namespace: str, key: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT value FROM memory_items
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
                """
                SELECT namespace, key, memory_type, value, consolidated, expires_at
                FROM memory_items
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
                """
                SELECT key, value, source_ids
                FROM memory_items
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
                """
                INSERT INTO memory_items
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
                "UPDATE memory_items SET consolidated = TRUE WHERE namespace = $1 AND key = $2",
                namespace, key,
            )

    async def _fetch_unconsolidated_episodic(self, namespace: str) -> list[dict]:
        """Return unconsolidated episodic items with their embeddings."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text, key, value, embedding::text
                FROM memory_items
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
                "DELETE FROM memory_items WHERE namespace = $1 AND expires_at IS NOT NULL AND expires_at <= now()",
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

    @staticmethod
    def _redis_key(namespace: str, key: str) -> str:
        return f"memory:{namespace}:{key}"
