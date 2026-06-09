"""Phase 2 — Persistent Memory Layer.

All 27 integration tests. Run against the live Docker stack.
"""
import os
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Connection constants (read from env so they match the stack)
# ---------------------------------------------------------------------------
PG_DSN = os.environ.get("PG_DSN", "postgresql://harness:harness@localhost:5432/harness")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
DOLT_CONN = dict(
    host=os.environ.get("DOLT_HOST", "localhost"),
    port=int(os.environ.get("DOLT_PORT", "3306")),
    user="root",
    password="root",
    database="harness",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
async def setup_memory_schema():
    """Create memory_items table once per test session."""
    from harness_memory.memory_store import PostgresMemoryStore
    store = PostgresMemoryStore(PG_DSN, REDIS_URL, EMBED_MODEL, OLLAMA_HOST)
    await store.setup()
    await store.close()


@pytest.fixture
async def memory_store():
    from harness_memory.memory_store import PostgresMemoryStore
    store = PostgresMemoryStore(PG_DSN, REDIS_URL, EMBED_MODEL, OLLAMA_HOST)
    await store.setup()
    yield store
    await store._truncate()
    await store.close()


@pytest.fixture
def formula_store():
    from harness_memory.formula_store import DoltFormulaStore
    return DoltFormulaStore(**DOLT_CONN)


@pytest.fixture
async def consolidation_worker(memory_store, formula_store):
    from harness_memory.consolidation import ConsolidationWorker
    return ConsolidationWorker(store=memory_store, formula_store=formula_store)


# ---------------------------------------------------------------------------
# Checkpointer tests (3)
# ---------------------------------------------------------------------------

async def test_checkpointer_saves_state():
    """After a graph step, PostgresSaver checkpoint exists for thread_id."""
    from typing import TypedDict
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import StateGraph, END

    class State(TypedDict):
        visited: list

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    async with AsyncPostgresSaver.from_conn_string(PG_DSN) as saver:
        await saver.setup()

        def step(state: State) -> State:
            return {"visited": state.get("visited", []) + ["step"]}

        builder = StateGraph(State)
        builder.add_node("step", step)
        builder.set_entry_point("step")
        builder.add_edge("step", END)
        graph = builder.compile(checkpointer=saver)

        await graph.ainvoke({"visited": []}, config)

        saved = await saver.aget(config)
        assert saved is not None, "Checkpoint should exist after graph run"
        assert saved["channel_values"].get("visited") == ["step"]


async def test_checkpointer_resumes():
    """Graph resumed from checkpoint continues from last saved step, not start."""
    from typing import TypedDict
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import StateGraph, END

    class State(TypedDict):
        steps: list

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    async with AsyncPostgresSaver.from_conn_string(PG_DSN) as saver:
        await saver.setup()

        def step1(state: State) -> State:
            return {"steps": state.get("steps", []) + ["step1"]}

        def step2(state: State) -> State:
            return {"steps": state.get("steps", []) + ["step2"]}

        builder = StateGraph(State)
        builder.add_node("step1", step1)
        builder.add_node("step2", step2)
        builder.set_entry_point("step1")
        builder.add_edge("step1", "step2")
        builder.add_edge("step2", END)

        # First run: interrupt after step1
        graph_interrupted = builder.compile(checkpointer=saver, interrupt_after=["step1"])
        result1 = await graph_interrupted.ainvoke({"steps": []}, config)
        assert result1["steps"] == ["step1"], "Only step1 should have run"

        # Resume: should run step2 only, not replay step1
        graph_full = builder.compile(checkpointer=saver)
        result2 = await graph_full.ainvoke(None, config)
        assert result2["steps"] == ["step1", "step2"], "step1 should not replay on resume"


async def test_checkpointer_thread_isolation():
    """Checkpoint for thread_A is not visible when loading thread_B."""
    from typing import TypedDict
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import StateGraph, END

    class State(TypedDict):
        marker: str

    thread_a = str(uuid.uuid4())
    thread_b = str(uuid.uuid4())
    config_a = {"configurable": {"thread_id": thread_a}}
    config_b = {"configurable": {"thread_id": thread_b}}

    async with AsyncPostgresSaver.from_conn_string(PG_DSN) as saver:
        await saver.setup()

        def noop(state: State) -> State:
            return state

        builder = StateGraph(State)
        builder.add_node("noop", noop)
        builder.set_entry_point("noop")
        builder.add_edge("noop", END)
        graph = builder.compile(checkpointer=saver)

        await graph.ainvoke({"marker": "thread_a_data"}, config_a)

        # Thread B has never run; its checkpoint should not exist
        saved_b = await saver.aget(config_b)
        assert saved_b is None, "Thread B checkpoint must not bleed from thread A"


# ---------------------------------------------------------------------------
# Memory store tests (10)
# ---------------------------------------------------------------------------

async def test_memory_write_and_read(memory_store):
    """write() stores item; read() returns same item within same session."""
    await memory_store.write("architect", "key-1", {"fact": "test value"})
    result = await memory_store.read("architect", "key-1")
    assert result == {"fact": "test value"}


async def test_memory_namespace_isolation(memory_store):
    """Item written to architect/ not returned by read() against sre/."""
    await memory_store.write("architect", "shared-key", {"owner": "architect"})
    result = await memory_store.read("sre", "shared-key")
    assert result is None


async def test_memory_cross_session_persistence(memory_store):
    """Item written in session 1 is readable in session 2 (new DB connection)."""
    from harness_memory.memory_store import PostgresMemoryStore

    await memory_store.write("architect", "persist-key", {"fact": "cross-session"})

    # Simulate new session: new store with fresh connection
    store2 = PostgresMemoryStore(PG_DSN, REDIS_URL, EMBED_MODEL, OLLAMA_HOST)
    await store2.setup()
    try:
        result = await store2.read("architect", "persist-key")
        assert result == {"fact": "cross-session"}
    finally:
        await store2.close()


async def test_memory_ttl_expiry(memory_store):
    """Item with expires_at in the past is not returned by read()."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await memory_store.write("architect", "expired-key", {"data": "gone"}, _expires_at=past)
    result = await memory_store.read("architect", "expired-key")
    assert result is None


async def test_memory_redis_hot_read(memory_store):
    """Second read of same key is served from Redis (cache hit counter increases)."""
    await memory_store.write("architect", "hot-key", {"data": "cached"})

    # First read: cache miss (loads from PG, populates Redis)
    result1 = await memory_store.read("architect", "hot-key")
    hits_before = memory_store.cache_hits

    # Second read: cache hit (served from Redis)
    result2 = await memory_store.read("architect", "hot-key")
    assert memory_store.cache_hits == hits_before + 1
    assert result1 == result2


async def test_memory_semantic_search(memory_store):
    """search() returns items semantically related to query, ordered by relevance."""
    await memory_store.write("sre", "db-issue", {"text": "Database connection pool exhausted causing slow queries"})
    await memory_store.write("sre", "cache-tip", {"text": "Redis cache warming improves API response latency"})
    await memory_store.write("sre", "deploy-note", {"text": "Deploy rollback completed after failed canary release"})

    results = await memory_store.search("sre", "database performance problems", top_k=3)
    assert len(results) > 0
    # db-issue should rank first — most semantically related to database performance
    assert results[0]["key"] == "db-issue"


async def test_memory_overwrite(memory_store):
    """write() with same namespace+key overwrites the previous value."""
    await memory_store.write("architect", "ow-key", {"version": 1})
    await memory_store.write("architect", "ow-key", {"version": 2})
    result = await memory_store.read("architect", "ow-key")
    assert result == {"version": 2}


async def test_memory_delete(memory_store):
    """delete() removes item; subsequent read() returns None."""
    await memory_store.write("architect", "del-key", {"to": "delete"})
    await memory_store.delete("architect", "del-key")
    result = await memory_store.read("architect", "del-key")
    assert result is None


async def test_memory_interface_compliance():
    """PostgresMemoryStore satisfies MemoryStore Protocol (structural check)."""
    from harness_memory.protocols import MemoryStore
    from harness_memory.memory_store import PostgresMemoryStore
    import inspect

    proto_methods = {
        name for name, _ in inspect.getmembers(MemoryStore, predicate=inspect.isfunction)
    }
    impl_methods = {
        name for name, _ in inspect.getmembers(PostgresMemoryStore, predicate=inspect.isfunction)
    }
    missing = proto_methods - impl_methods
    assert not missing, f"PostgresMemoryStore missing protocol methods: {missing}"


async def test_sre_runbook_namespace(memory_store):
    """SRE can write/read from sre/ without touching architect/ or code_reviewer/."""
    await memory_store.write("sre", "runbook:db-failover", {"steps": ["check replica", "promote"]})

    sre_result = await memory_store.read("sre", "runbook:db-failover")
    arch_result = await memory_store.read("architect", "runbook:db-failover")
    cr_result = await memory_store.read("code_reviewer", "runbook:db-failover")

    assert sre_result == {"steps": ["check replica", "promote"]}
    assert arch_result is None
    assert cr_result is None


# ---------------------------------------------------------------------------
# Episodic / consolidation tests (5)
# ---------------------------------------------------------------------------

async def test_episodic_memory_write(memory_store):
    """Agent post-task write with memory_type='episodic' stores item with consolidated=False."""
    await memory_store.write("sre", "ep-key", {"obs": "cpu spiked"}, memory_type="episodic")
    row = await memory_store._get_raw("sre", "ep-key")
    assert row["memory_type"] == "episodic"
    assert row["consolidated"] is False


async def test_semantic_memory_written_by_consolidation(memory_store, consolidation_worker):
    """After consolidation run_pass(), semantic items exist and source episodes have consolidated=True."""
    await memory_store.write("sre", "ep1", {"text": "high cpu on web-01"}, memory_type="episodic")
    await memory_store.write("sre", "ep2", {"text": "high cpu on web-02"}, memory_type="episodic")

    result = await consolidation_worker.run_pass("sre")
    assert result.semantic_items_created >= 1

    semantic_items = await memory_store._list_by_type("sre", "semantic")
    assert len(semantic_items) >= 1

    ep1 = await memory_store._get_raw("sre", "ep1")
    assert ep1["consolidated"] is True


async def test_consolidation_clusters_similar_episodes(memory_store, consolidation_worker):
    """Two episodic items with high cosine similarity are merged into one semantic item."""
    await memory_store.write(
        "sre", "sim1",
        {"text": "Database connection pool exhausted causing slow queries"},
        memory_type="episodic",
    )
    await memory_store.write(
        "sre", "sim2",
        {"text": "DB connections running out, queries are timing out"},
        memory_type="episodic",
    )

    result = await consolidation_worker.run_pass("sre")

    semantic_items = await memory_store._list_by_type("sre", "semantic")
    # Two similar items should produce exactly one semantic item
    assert len(semantic_items) == 1
    assert result.semantic_items_created == 1


async def test_consolidation_preserves_distinct_episodes(memory_store, consolidation_worker):
    """Two episodic items with low cosine similarity produce two separate semantic items."""
    await memory_store.write(
        "sre", "dist1",
        {"text": "Database connection pool exhausted causing slow queries"},
        memory_type="episodic",
    )
    await memory_store.write(
        "sre", "dist2",
        {"text": "New feature deployment successful canary release passed"},
        memory_type="episodic",
    )

    await consolidation_worker.run_pass("sre")

    semantic_items = await memory_store._list_by_type("sre", "semantic")
    assert len(semantic_items) == 2


async def test_consolidation_prunes_expired_items(memory_store, consolidation_worker):
    """Expired episodic items are deleted by run_pass(); non-expired remain."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await memory_store.write("sre", "expired", {"text": "old obs"}, memory_type="episodic", _expires_at=past)
    await memory_store.write("sre", "alive", {"text": "live obs"}, memory_type="episodic")

    result = await consolidation_worker.run_pass("sre")
    assert result.items_pruned >= 1

    still_alive = await memory_store._get_raw("sre", "alive")
    gone = await memory_store._get_raw("sre", "expired")
    assert still_alive is not None
    assert gone is None


# ---------------------------------------------------------------------------
# Formula store tests (9)
# ---------------------------------------------------------------------------

def _triage_formula(version: int = 1) -> Any:
    from harness_memory.models import Formula
    # Use a distinct agent_role ("test_sre") so seed formulas (role "sre") don't
    # interfere with lookup/deprecate assertions in these tests.
    return Formula(
        id="test:triage-incident",
        name="Triage Incident",
        agent_role="test_sre",
        version=version,
        status="active",
        description="Respond to production incidents including database alerts latency spikes and error investigations",
        input_schema={"type": "object", "properties": {"alert": {"type": "string"}}},
        steps=[{"action": "observability_query"}, {"action": "runbook_read"}],
        output_contract={"type": "object", "properties": {"report": {"type": "string"}}},
        created_by="test",
    )


@pytest.fixture(autouse=True)
def clean_test_formulas(formula_store):
    """Remove test-only formulas before and after each test."""
    formula_store._delete_where_id_like("test:%")
    yield
    formula_store._delete_where_id_like("test:%")


async def test_formula_quality_score_updated(formula_store, consolidation_worker, memory_store):
    """After run_pass(), formula with 8/10 successful pours has quality_score >= 0.8."""
    formula_store.propose(_triage_formula())
    formula_store._record_pours("test:triage-incident", successes=8, failures=2)

    await consolidation_worker.run_pass("sre")

    formula = formula_store.get("test:triage-incident")
    assert formula is not None
    assert formula.quality_score >= 0.8


async def test_formula_graduates_to_proven(formula_store, consolidation_worker, memory_store):
    """Formula with >=10 pours and >=80% success has status='proven' after consolidation."""
    formula_store.propose(_triage_formula())
    formula_store._record_pours("test:triage-incident", successes=9, failures=1)

    await consolidation_worker.run_pass("sre")

    formula = formula_store.get("test:triage-incident")
    assert formula.status == "proven"


async def test_formula_flagged_for_review(formula_store, consolidation_worker, memory_store):
    """Formula with >=10 pours and <30% success has status='review' after consolidation."""
    formula_store.propose(_triage_formula())
    formula_store._record_pours("test:triage-incident", successes=2, failures=8)

    await consolidation_worker.run_pass("sre")

    formula = formula_store.get("test:triage-incident")
    assert formula.status == "review"


async def test_formula_write_creates_dolt_commit(formula_store):
    """propose() inserts a formula row and dolt log shows a new commit with formula id."""
    commit_hash = formula_store.propose(_triage_formula())

    assert commit_hash, "propose() must return a commit hash"
    log = formula_store.recent_commits(n=5)
    commit_messages = [row["message"] for row in log]
    assert any("test:triage-incident" in msg for msg in commit_messages)


async def test_formula_lookup_by_task(formula_store):
    """lookup('test_sre', 'DB latency alert fired') returns the triage-incident formula."""
    formula_store.propose(_triage_formula())

    result = formula_store.lookup("test_sre", "DB latency alert fired")
    assert result is not None
    assert result.id == "test:triage-incident"


async def test_formula_lookup_no_match(formula_store):
    """lookup() returns None for a task with no matching formula — does not error."""
    result = formula_store.lookup("test_sre", "birthday party planning catering")
    assert result is None


async def test_formula_version_history(formula_store):
    """After two propose() calls with same id, dolt log shows two commits; both versions queryable."""
    formula_store.propose(_triage_formula(version=1))
    formula_store.propose(_triage_formula(version=2))

    log = formula_store.recent_commits(n=10)
    messages = [row["message"] for row in log]
    formula_commits = [m for m in messages if "test:triage-incident" in m]
    assert len(formula_commits) >= 2, "Expected at least 2 commits for this formula"

    v1 = formula_store.get("test:triage-incident", version=1)
    v2 = formula_store.get("test:triage-incident", version=2)
    assert v1 is not None
    assert v2 is not None
    assert v2.version > v1.version


async def test_formula_deprecate(formula_store):
    """Deprecated formula is not returned by list_active() or lookup()."""
    formula_store.propose(_triage_formula())
    formula_store.deprecate("test:triage-incident")

    active = formula_store.list_active("test_sre")
    ids = [f.id for f in active]
    assert "test:triage-incident" not in ids

    result = formula_store.lookup("test_sre", "DB latency alert fired")
    assert result is None


async def test_formula_interface_compliance():
    """DoltFormulaStore satisfies FormulaStore Protocol (structural check)."""
    from harness_memory.protocols import FormulaStore
    from harness_memory.formula_store import DoltFormulaStore
    import inspect

    proto_methods = {
        name for name, _ in inspect.getmembers(FormulaStore, predicate=inspect.isfunction)
    }
    impl_methods = {
        name for name, _ in inspect.getmembers(DoltFormulaStore, predicate=inspect.isfunction)
    }
    missing = proto_methods - impl_methods
    assert not missing, f"DoltFormulaStore missing protocol methods: {missing}"
