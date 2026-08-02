"""Unit tests for retrieve_skill and DoltFormulaStore.list_matches.

Pure-function / monkeypatched-store tests — no Docker or Dolt required.
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

_FORMULA_2 = Formula(
    id="sre:db-latency-generic:1",
    name="db-latency-generic",
    agent_role="sre",
    version=1,
    status="active",
    description="Generic database latency triage",
    input_schema={"type": "object", "properties": {"incident": {"type": "string"}}},
    steps=[{"tool": "observability_query", "params": {"query": "db latency"}}],
    output_contract={"type": "object"},
    promoted_by="human_operator",
)


class _FakeStore:
    """Fake store matching the DoltFormulaStore.list_matches contract."""

    def __init__(self, matches=None):
        self.calls: list[dict] = []
        self._matches = matches or []

    def list_matches(self, agent_role: str, task: str) -> list[tuple[Formula, float]]:
        self.calls.append({"agent_role": agent_role, "task": task})
        return self._matches


# ---------------------------------------------------------------------------
# retrieve_skill — Behavior 1: a qualifying match returns id + score
# ---------------------------------------------------------------------------

def test_retrieve_skill_matched_returns_id_and_score():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(matches=[(_FORMULA, 0.42)])
    result = retrieve_skill(store, "sre", "DB latency spike — connection pool")

    assert result["matched"] is True
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["id"] == "sre:db-connection-pool-exhaustion:1"
    assert match["name"] == "db-connection-pool-exhaustion"
    assert match["description"] == _FORMULA.description
    assert match["score"] == 0.42
    assert isinstance(match["steps"], list)
    assert len(match["steps"]) == 2


# ---------------------------------------------------------------------------
# retrieve_skill — Behavior 2: multiple qualifying matches are ranked
# ---------------------------------------------------------------------------

def test_retrieve_skill_ranks_multiple_matches_by_score_desc():
    from harness_memory.skill_retriever import retrieve_skill

    # Store already returns matches sorted best-first (per list_matches contract).
    store = _FakeStore(matches=[(_FORMULA, 0.42), (_FORMULA_2, 0.11)])
    result = retrieve_skill(store, "sre", "DB latency spike — connection pool")

    assert result["matched"] is True
    ids_in_order = [m["id"] for m in result["matches"]]
    assert ids_in_order == ["sre:db-connection-pool-exhaustion:1", "sre:db-latency-generic:1"]
    scores = [m["score"] for m in result["matches"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# retrieve_skill — Behavior 3: no qualifying match returns a well-formed empty result
# ---------------------------------------------------------------------------

def test_retrieve_skill_no_match_returns_well_formed_empty_result():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(matches=[])
    result = retrieve_skill(store, "sre", "unrelated query")

    assert result["matched"] is False
    assert result["matches"] == []
    assert result["query"] == "unrelated query"


# ---------------------------------------------------------------------------
# retrieve_skill — Behavior 4: agent_role and task forwarded to store.list_matches
# ---------------------------------------------------------------------------

def test_retrieve_skill_forwards_args():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(matches=[(_FORMULA, 0.42)])
    retrieve_skill(store, "sre", "DB latency spike")

    assert store.calls[0]["agent_role"] == "sre"
    assert store.calls[0]["task"] == "DB latency spike"


# ---------------------------------------------------------------------------
# retrieve_skill — Behavior 5: query field in result matches input task
# ---------------------------------------------------------------------------

def test_retrieve_skill_query_echoed():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(matches=[])
    result = retrieve_skill(store, "sre", "memory leak in worker")

    assert result["query"] == "memory leak in worker"


# ---------------------------------------------------------------------------
# retrieve_skill — Behavior 6: output_contract and input_schema included per match
# ---------------------------------------------------------------------------

def test_retrieve_skill_includes_output_contract_and_input_schema():
    from harness_memory.skill_retriever import retrieve_skill

    store = _FakeStore(matches=[(_FORMULA, 0.42)])
    result = retrieve_skill(store, "sre", "q")

    match = result["matches"][0]
    assert "output_contract" in match
    assert match["input_schema"] == _FORMULA.input_schema


# ---------------------------------------------------------------------------
# DoltFormulaStore.list_matches / lookup — pure scoring logic, DB monkeypatched out
# ---------------------------------------------------------------------------

def _make_store():
    from harness_memory.formula_store import DoltFormulaStore

    # Constructor only stores connection kwargs — no network I/O happens here,
    # so this is safe to instantiate without a live Dolt server.
    return DoltFormulaStore(host="unused", port=3306, user="root", password="root", database="harness")


def test_list_matches_ranks_candidates_by_score_desc():
    store = _make_store()
    store.list_active = lambda agent_role: [_FORMULA_2, _FORMULA]

    matches = store.list_matches("sre", "connection pool exhaustion causing DB latency spikes")

    assert [f.id for f, _ in matches] == [
        "sre:db-connection-pool-exhaustion:1",
        "sre:db-latency-generic:1",
    ]
    scores = [s for _, s in matches]
    assert scores == sorted(scores, reverse=True)


def test_list_matches_excludes_scores_at_or_below_threshold():
    store = _make_store()
    store.list_active = lambda agent_role: [_FORMULA]

    # Query shares no keywords with the formula's name/description.
    matches = store.list_matches("sre", "birthday party planning catering")

    assert matches == []


def test_list_matches_empty_when_no_active_candidates():
    store = _make_store()
    store.list_active = lambda agent_role: []

    matches = store.list_matches("sre", "anything")

    assert matches == []


def test_lookup_returns_top_scoring_formula():
    store = _make_store()
    store.list_active = lambda agent_role: [_FORMULA_2, _FORMULA]

    result = store.lookup("sre", "connection pool exhaustion causing DB latency spikes")

    assert result is not None
    assert result.id == "sre:db-connection-pool-exhaustion:1"


def test_lookup_returns_none_when_no_match_above_threshold():
    store = _make_store()
    store.list_active = lambda agent_role: [_FORMULA]

    result = store.lookup("sre", "birthday party planning catering")

    assert result is None


def test_lookup_returns_none_when_no_active_candidates():
    store = _make_store()
    store.list_active = lambda agent_role: []

    result = store.lookup("sre", "anything")

    assert result is None
