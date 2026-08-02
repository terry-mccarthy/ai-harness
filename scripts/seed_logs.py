"""Seed log entries from docs/logs/ into the pgvector memory store."""
import asyncio
import os
from pathlib import Path

from harness_memory.memory_store import PostgresMemoryStore
from harness_memory.embedding_provider import build_embedding_provider_from_env
from harness_memory.log_seed import seed_logs


async def main() -> None:
    store = PostgresMemoryStore(
        os.environ["PG_DSN"],
        os.environ.get("REDIS_URL", "redis://localhost:6379"),
        build_embedding_provider_from_env(),
    )
    await store.setup()
    log_dir = Path(__file__).resolve().parents[1] / "docs" / "logs"
    n = await seed_logs(log_dir, store)
    print(f"seeded {n} log entries")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
