"""Seed runbooks from docs/runbooks/ into the pgvector memory store."""
import asyncio
import os
from pathlib import Path

from harness_memory.memory_store import PostgresMemoryStore
from harness_memory.embedding_provider import build_embedding_provider_from_env
from harness_memory.runbook_seed import seed_runbooks


async def main() -> None:
    store = PostgresMemoryStore(
        os.environ["PG_DSN"],
        os.environ.get("REDIS_URL", "redis://localhost:6379"),
        build_embedding_provider_from_env(),
    )
    await store.setup()
    runbook_dir = Path(__file__).resolve().parents[1] / "docs" / "runbooks"
    n = await seed_runbooks(runbook_dir, store)
    print(f"seeded {n} runbooks")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
