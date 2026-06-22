"""Unit tests for retrieve_skill — pure function with a fake formula store.

No Docker stack or Dolt required.
"""
from harness_memory.models import Formula

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FORMULA = Formula(
    id="sre:db-connection-pool-exhaustion:1",
    name="db-connection-pool-exhaustion",
    agent_role="sre",
    version=1,
    status="active",
    description="Diagnose and recover from connection pool exhaustion causing DB latency spikes",
    input_schema={"type": "object", "properties": {"incident": {"type": "string"}}},
    steps=[
        {"tool": "observability_query", "params": {"query": "connection pool metrics"}},
        {"tool": "log_search", "params": {"query": "connection pool exhausted"}},
    ],
    output_contract={"type": "object"},
    promoted_by="human_operator",
)


class _FakeStore:
    def __init__(self, formula=None):
        self.calls: list[dict] = []
        self._formula = formula

    def lookup(self, agent_role: str, task: str) -> Formula | None:
        self.calls.append({"agent_role": agent_role, "task": task})
        return self._formula


# ---------------------------------------------------------------------------
# Behavior 1 — matched formula returns correct shape
# ---------------------------------------------------------------------------

def test_retrieve_skill_matched_returns_correct_shape():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(formula=_FORMULA)
    result = retrieve_skill(store, "sre", "DB latency spike — connection pool")

    assert result["matched"] is True
    skill = result["skill"]
    assert skill["id"] == "sre:db-connection-pool-exhaustion:1"
    assert skill["name"] == "db-connection-pool-exhaustion"
    assert skill["description"] == _FORMULA.description
    assert isinstance(skill["steps"], list)
    assert len(skill["steps"]) == 2


# ---------------------------------------------------------------------------
# Behavior 2 — no match returns skill=None and matched=False
# ---------------------------------------------------------------------------

def test_retrieve_skill_no_match():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(formula=None)
    result = retrieve_skill(store, "sre", "unrelated query")

    assert result["matched"] is False
    assert result["skill"] is None


# ---------------------------------------------------------------------------
# Behavior 3 — agent_role and task forwarded to store.lookup
# ---------------------------------------------------------------------------

def test_retrieve_skill_forwards_args():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(formula=_FORMULA)
    retrieve_skill(store, "sre", "DB latency spike")

    assert store.calls[0]["agent_role"] == "sre"
    assert store.calls[0]["task"] == "DB latency spike"


# ---------------------------------------------------------------------------
# Behavior 4 — query field in result matches input task
# ---------------------------------------------------------------------------

def test_retrieve_skill_query_echoed():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(formula=None)
    result = retrieve_skill(store, "sre", "memory leak in worker")

    assert result["query"] == "memory leak in worker"


# ---------------------------------------------------------------------------
# Behavior 5 — output_contract included in result
# ---------------------------------------------------------------------------

def test_retrieve_skill_includes_output_contract():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(formula=_FORMULA)
    result = retrieve_skill(store, "sre", "q")

    assert "output_contract" in result["skill"]
    assert result["skill"]["input_schema"] == _FORMULA.input_schema
