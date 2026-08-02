"""Integration tests for retrieve_logs against the real seeded log corpus.

Requires the live Docker stack (PG + Redis + Ollama for embeddings) — seeds
docs/logs/*.jsonl through the same harness_memory.log_seed.seed_logs() path
scripts/seed_logs.py uses, then exercises retrieve_logs() against a real
PostgresMemoryStore. Unlike test_unit_log_retriever.py's _FakeStore tests,
this proves ranking and truncation behave sensibly against real embeddings
and real cosine-similarity scores, not a hand-built stand-in.
"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get("PG_DSN", "postgresql://harness:harness@localhost:5432/harness")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")

LOG_DIR = Path(__file__).resolve().parents[2] / "docs" / "logs"


@pytest.fixture
async def seeded_log_store():
    from harness_memory.memory_store import PostgresMemoryStore
    from harness_memory.log_seed import seed_logs

    store = PostgresMemoryStore(PG_DSN, REDIS_URL, EMBED_MODEL, OLLAMA_HOST)
    await store.setup()
    await seed_logs(LOG_DIR, store)
    yield store
    await store._truncate()
    await store.close()


# ---------------------------------------------------------------------------
# Behavior 1 — a query closely matching one incident's vocabulary ranks that
# incident's lines above the unrelated incident's lines
# ---------------------------------------------------------------------------

async def test_query_ranks_matching_incident_above_unrelated(seeded_log_store):
    from harness_memory.log_retriever import retrieve_logs

    result = await retrieve_logs(
        seeded_log_store, "database connection pool exhausted, queries timing out"
    )

    assert result["logs"], "expected at least one db-latency line to clear the threshold"
    top_services = {entry["service"] for entry in result["logs"][:2]}
    # db-latency.jsonl entries come from postgres/api-server; cost-spike.jsonl
    # entries come from architect-agent/governance/mcpjungle — a db-latency
    # query should not be dominated by cost-spike/token-budget lines.
    assert top_services & {"postgres", "api-server"}


# ---------------------------------------------------------------------------
# Behavior 2 — returned_count/total_count are well-formed against the real
# corpus: returned_count never exceeds total_count, and never exceeds top_k
# ---------------------------------------------------------------------------

async def test_returned_and_total_counts_are_consistent(seeded_log_store):
    from harness_memory.log_retriever import MAX_LOG_RESULTS, retrieve_logs

    result = await retrieve_logs(seeded_log_store, "token budget exceeded, LLM call timeout")

    assert result["returned_count"] == len(result["logs"])
    assert result["returned_count"] <= MAX_LOG_RESULTS
    assert result["returned_count"] <= result["total_count"]


# ---------------------------------------------------------------------------
# Behavior 3 — a query with no semantic relationship to the seeded corpus
# returns a well-formed empty result, not an error
# ---------------------------------------------------------------------------

async def test_unrelated_query_returns_well_formed_empty_result(seeded_log_store):
    from harness_memory.log_retriever import retrieve_logs

    result = await retrieve_logs(
        seeded_log_store, "recipe for chocolate chip cookies, preheat oven to 375F"
    )

    assert result["logs"] == []
    assert result["returned_count"] == 0
    assert result["total_count"] == 0
