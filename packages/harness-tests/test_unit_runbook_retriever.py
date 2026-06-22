"""Unit tests for the runbook retriever.

All tests use a fake memory store — no Postgres, Ollama, or MCP required.
"""
import pytest

pytestmark = pytest.mark.asyncio


class _FakeStore:
    def __init__(self, results: list[dict] | None = None):
        self._results = results or []
        self.last_call: dict = {}

    async def search(self, namespace: str, query: str, top_k: int = 5) -> list[dict]:
        self.last_call = {"namespace": namespace, "query": query, "top_k": top_k}
        return self._results[:top_k]


# ---------------------------------------------------------------------------
# Behavior 1 — empty store returns empty list, preserves query
# ---------------------------------------------------------------------------

async def test_empty_store_returns_empty_list():
    from harness_memory.runbook_retriever import retrieve_runbooks

    store = _FakeStore(results=[])
    result = await retrieve_runbooks(store, "pod crashing")

    assert result["runbooks"] == []
    assert result["query"] == "pod crashing"


# ---------------------------------------------------------------------------
# Behavior 2 — result has correct shape: id, signature, body, score
# ---------------------------------------------------------------------------

async def test_result_has_correct_shape():
    from harness_memory.runbook_retriever import retrieve_runbooks

    store = _FakeStore(results=[
        {
            "key": "cost-spike",
            "value": {"id": "cost-spike", "signature": "Token budget exceeded by 2x.", "body": "# Runbook: Cost Spike\n\n## Steps\nCheck dashboard."},
            "score": 0.91,
        }
    ])
    result = await retrieve_runbooks(store, "budget exceeded alert")

    assert len(result["runbooks"]) == 1
    rb = result["runbooks"][0]
    assert rb["id"] == "cost-spike"
    assert rb["signature"] == "Token budget exceeded by 2x."
    assert "## Steps" in rb["body"]
    assert rb["score"] == 0.91


# ---------------------------------------------------------------------------
# Behavior 3 — score is rounded to 3 decimal places
# ---------------------------------------------------------------------------

async def test_score_is_rounded_to_3dp():
    from harness_memory.runbook_retriever import retrieve_runbooks

    store = _FakeStore(results=[
        {"key": "agent-unresponsive", "value": {"signature": "Thread stuck.", "body": "Restart."}, "score": 0.87456789},
    ])
    result = await retrieve_runbooks(store, "thread hung")

    assert result["runbooks"][0]["score"] == 0.875


# ---------------------------------------------------------------------------
# Behavior 4 — top_k is forwarded to the store
# ---------------------------------------------------------------------------

async def test_top_k_forwarded_to_store():
    from harness_memory.runbook_retriever import retrieve_runbooks

    store = _FakeStore(results=[
        {"key": "rb1", "value": {"signature": "s1", "body": "b1"}, "score": 0.9},
        {"key": "rb2", "value": {"signature": "s2", "body": "b2"}, "score": 0.8},
        {"key": "rb3", "value": {"signature": "s3", "body": "b3"}, "score": 0.7},
    ])
    await retrieve_runbooks(store, "some incident", top_k=1)

    assert store.last_call["top_k"] == 1


# ---------------------------------------------------------------------------
# Behavior 5 — store is always queried under the "runbooks" namespace
# ---------------------------------------------------------------------------

async def test_namespace_is_runbooks():
    from harness_memory.runbook_retriever import retrieve_runbooks

    store = _FakeStore()
    await retrieve_runbooks(store, "anything")

    assert store.last_call["namespace"] == "runbooks"


# ---------------------------------------------------------------------------
# Behavior 6 — result order matches store order (highest score first)
# ---------------------------------------------------------------------------

async def test_result_order_matches_store():
    from harness_memory.runbook_retriever import retrieve_runbooks

    store = _FakeStore(results=[
        {"key": "high", "value": {"signature": "h", "body": "h"}, "score": 0.95},
        {"key": "low",  "value": {"signature": "l", "body": "l"}, "score": 0.61},
    ])
    result = await retrieve_runbooks(store, "query")

    assert result["runbooks"][0]["id"] == "high"
    assert result["runbooks"][1]["id"] == "low"
