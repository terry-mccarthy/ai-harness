"""Unit tests for retrieve_logs — pure function with a fake store.

No Docker stack or Ollama required.
"""
import pytest

pytestmark = pytest.mark.asyncio


class _FakeStore:
    def __init__(self, results=None):
        self.calls: list[dict] = []
        self._results = results or []

    async def search(self, namespace: str, query: str, top_k: int = 5):
        self.calls.append({"namespace": namespace, "query": query, "top_k": top_k})
        return self._results


_ENTRY = {
    "key": "log:cost-spike:0",
    "value": {
        "timestamp": "2024-01-15T13:55:00Z",
        "level": "ERROR",
        "service": "architect-agent",
        "message": "LLM call timeout after 120s, retrying",
        "trace_id": "abc123",
    },
    "score": 0.91234,
}


# ---------------------------------------------------------------------------
# Behavior 1 — happy path: correct shape returned
# ---------------------------------------------------------------------------

async def test_retrieve_logs_returns_correct_shape():
    from harness_memory.log_retriever import retrieve_logs

    store = _FakeStore(results=[_ENTRY])
    result = await retrieve_logs(store, "timeout errors")

    assert result["query"] == "timeout errors"
    assert len(result["logs"]) == 1
    entry = result["logs"][0]
    assert entry["id"] == "log:cost-spike:0"
    assert entry["timestamp"] == "2024-01-15T13:55:00Z"
    assert entry["level"] == "ERROR"
    assert entry["service"] == "architect-agent"
    assert entry["message"] == "LLM call timeout after 120s, retrying"


# ---------------------------------------------------------------------------
# Behavior 2 — empty store returns empty list
# ---------------------------------------------------------------------------

async def test_retrieve_logs_empty_store():
    from harness_memory.log_retriever import retrieve_logs

    store = _FakeStore(results=[])
    result = await retrieve_logs(store, "nothing here")

    assert result["logs"] == []
    assert result["query"] == "nothing here"


# ---------------------------------------------------------------------------
# Behavior 3 — score rounded to 3 decimal places
# ---------------------------------------------------------------------------

async def test_retrieve_logs_score_rounded():
    from harness_memory.log_retriever import retrieve_logs

    store = _FakeStore(results=[_ENTRY])
    result = await retrieve_logs(store, "q")

    assert result["logs"][0]["score"] == 0.912


# ---------------------------------------------------------------------------
# Behavior 4 — top_k forwarded to store.search
# ---------------------------------------------------------------------------

async def test_retrieve_logs_top_k_forwarded():
    from harness_memory.log_retriever import retrieve_logs

    store = _FakeStore()
    await retrieve_logs(store, "q", top_k=10)

    assert store.calls[0]["top_k"] == 10


# ---------------------------------------------------------------------------
# Behavior 5 — searches the "logs" namespace
# ---------------------------------------------------------------------------

async def test_retrieve_logs_uses_logs_namespace():
    from harness_memory.log_retriever import retrieve_logs

    store = _FakeStore()
    await retrieve_logs(store, "q")

    assert store.calls[0]["namespace"] == "logs"


# ---------------------------------------------------------------------------
# Behavior 6 — order of results preserved
# ---------------------------------------------------------------------------

async def test_retrieve_logs_order_preserved():
    from harness_memory.log_retriever import retrieve_logs

    entries = [
        {**_ENTRY, "key": f"log:svc:{i}", "score": 0.9 - i * 0.1}
        for i in range(3)
    ]
    store = _FakeStore(results=entries)
    result = await retrieve_logs(store, "q")

    ids = [e["id"] for e in result["logs"]]
    assert ids == ["log:svc:0", "log:svc:1", "log:svc:2"]
