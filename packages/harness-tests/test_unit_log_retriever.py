"""Unit tests for retrieve_logs — pure function with a fake store.

No Docker stack or Ollama required.
"""
import pytest

pytestmark = pytest.mark.asyncio


class _FakeStore:
    """In-memory stand-in for PostgresMemoryStore.search().

    Mirrors PostgresMemoryStore.search()'s real behavior: min_score filters
    server-side *before* the top_k slice, so LIMIT stays meaningful (K
    relevant rows, not K rows later thinned down).
    """

    def __init__(self, results=None):
        self.calls: list[dict] = []
        self._results = results or []

    async def search(
        self, namespace: str, query: str, top_k: int = 5, min_score: float | None = None
    ):
        self.calls.append(
            {"namespace": namespace, "query": query, "top_k": top_k, "min_score": min_score}
        )
        results = self._results
        if min_score is not None:
            results = [r for r in results if r["score"] >= min_score]
        return results[:top_k]


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
# Behavior 4 — top_k caps the number of logs returned to the caller
# ---------------------------------------------------------------------------

async def test_retrieve_logs_top_k_caps_returned_logs():
    from harness_memory.log_retriever import retrieve_logs

    entries = [
        {**_ENTRY, "key": f"log:svc:{i}", "score": 0.9 - i * 0.01}
        for i in range(8)
    ]
    store = _FakeStore(results=entries)
    result = await retrieve_logs(store, "q", top_k=3, min_score=0.0)

    assert len(result["logs"]) == 3


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


# ---------------------------------------------------------------------------
# Behavior 7 — a relevance threshold is applied by default and forwarded to
# the store, so filtering happens server-side (mirrors runbook_retriever)
# ---------------------------------------------------------------------------

async def test_min_score_defaults_to_relevance_threshold_and_is_forwarded():
    from harness_memory.log_retriever import LOG_MIN_SCORE, retrieve_logs

    store = _FakeStore(results=[])
    await retrieve_logs(store, "timeout errors")

    assert store.calls[0]["min_score"] == LOG_MIN_SCORE


# ---------------------------------------------------------------------------
# Behavior 8 — the threshold is tunable via a min_score override
# ---------------------------------------------------------------------------

async def test_min_score_is_overridable():
    from harness_memory.log_retriever import retrieve_logs

    store = _FakeStore(results=[])
    await retrieve_logs(store, "timeout errors", min_score=0.3)

    assert store.calls[0]["min_score"] == 0.3


# ---------------------------------------------------------------------------
# Behavior 9 — returned_count / total_count let the caller detect truncation:
# more rows clear the threshold than the top_k cap displays
# ---------------------------------------------------------------------------

async def test_returned_count_and_total_count_detect_truncation():
    from harness_memory.log_retriever import retrieve_logs

    # 8 rows all clear a min_score of 0.5; top_k caps display at 3.
    entries = [
        {**_ENTRY, "key": f"log:svc:{i}", "score": 0.9 - i * 0.01}
        for i in range(8)
    ]
    store = _FakeStore(results=entries)
    result = await retrieve_logs(store, "q", top_k=3, min_score=0.5)

    assert result["returned_count"] == 3
    assert result["total_count"] == 8
    assert len(result["logs"]) == 3


# ---------------------------------------------------------------------------
# Behavior 10 — when nothing is truncated, returned_count == total_count
# ---------------------------------------------------------------------------

async def test_returned_count_equals_total_count_when_not_truncated():
    from harness_memory.log_retriever import retrieve_logs

    store = _FakeStore(results=[_ENTRY])
    result = await retrieve_logs(store, "timeout errors")

    assert result["returned_count"] == 1
    assert result["total_count"] == 1


# ---------------------------------------------------------------------------
# Behavior 11 — a no-match query returns a well-formed empty result (empty
# logs, zero counts), not an error
# ---------------------------------------------------------------------------

async def test_no_match_returns_well_formed_empty_result():
    from harness_memory.log_retriever import retrieve_logs

    store = _FakeStore(results=[])
    result = await retrieve_logs(store, "completely unrelated query")

    assert result["logs"] == []
    assert result["returned_count"] == 0
    assert result["total_count"] == 0
    assert result["query"] == "completely unrelated query"
