"""Unit tests for the runbook ingestion seed.

All tests use a fixture runbook directory and a fake memory store —
no Postgres or Ollama required.
"""
import logging
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RUNBOOK_DIR = Path(__file__).parent / "fixtures" / "runbooks"


@pytest.fixture
def fixture_runbooks(tmp_path):
    """Write a small corpus of fixture runbooks under tmp_path."""
    (tmp_path / "cost-spike.md").write_text(
        "# Runbook: Cost Spike\n\n**When to use:** Token budget exceeded by 2x.\n\n## Steps\nCheck the dashboard."
    )
    (tmp_path / "agent-unresponsive.md").write_text(
        "# Runbook: Agent Unresponsive\n\n**When to use:** Thread stuck, no OTel spans for 120s.\n\n## Steps\nRestart the thread."
    )
    return tmp_path


class _FakeStore:
    """In-memory stand-in for PostgresMemoryStore."""
    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    async def write(self, namespace: str, key: str, value: dict, **kwargs) -> None:
        self.items[(namespace, key)] = value


# ---------------------------------------------------------------------------
# Behavior 1 — happy path: two valid runbooks are ingested with correct fields
# ---------------------------------------------------------------------------

async def test_seed_writes_runbooks_with_correct_fields(fixture_runbooks):
    from harness_memory.runbook_seed import seed_runbooks

    store = _FakeStore()
    await seed_runbooks(fixture_runbooks, store)

    assert ("runbooks", "cost-spike") in store.items
    assert ("runbooks", "agent-unresponsive") in store.items

    entry = store.items[("runbooks", "cost-spike")]
    assert entry["id"] == "cost-spike"
    assert "Token budget exceeded" in entry["signature"]
    assert "## Steps" in entry["body"]


# ---------------------------------------------------------------------------
# Behavior 2 — idempotency: re-seeding does not duplicate (write called once per runbook per run)
# ---------------------------------------------------------------------------

async def test_seed_is_idempotent(fixture_runbooks):
    from harness_memory.runbook_seed import seed_runbooks

    write_calls: list[tuple] = []

    class _TrackingStore:
        async def write(self, namespace, key, value, **kwargs):
            write_calls.append((namespace, key))

    store = _TrackingStore()
    await seed_runbooks(fixture_runbooks, store)
    count_after_first = len(write_calls)

    await seed_runbooks(fixture_runbooks, store)
    count_after_second = len(write_calls)

    # Each run writes exactly one entry per runbook; running twice should write 2×N, not 3×N
    assert count_after_first == 2
    assert count_after_second == 4  # store's upsert handles de-dup; seed always writes


# ---------------------------------------------------------------------------
# Behavior 3 — malformed runbook (missing When to use) is skipped with a warning
# ---------------------------------------------------------------------------

async def test_malformed_runbook_skipped_with_warning(fixture_runbooks, caplog):
    from harness_memory.runbook_seed import seed_runbooks

    (fixture_runbooks / "no-signature.md").write_text(
        "# Runbook: Missing Signature\n\nNo when-to-use line here.\n"
    )

    store = _FakeStore()
    with caplog.at_level(logging.WARNING, logger="harness_memory.runbook_seed"):
        await seed_runbooks(fixture_runbooks, store)

    assert ("runbooks", "no-signature") not in store.items
    assert any("no-signature" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Behavior 4 — empty directory seeds zero runbooks without error
# ---------------------------------------------------------------------------

async def test_empty_directory_seeds_nothing(tmp_path):
    from harness_memory.runbook_seed import seed_runbooks

    store = _FakeStore()
    await seed_runbooks(tmp_path, store)

    assert len(store.items) == 0


# ---------------------------------------------------------------------------
# Behavior 5 — non-.md files in the directory are ignored
# ---------------------------------------------------------------------------

async def test_non_md_files_ignored(fixture_runbooks):
    from harness_memory.runbook_seed import seed_runbooks

    (fixture_runbooks / "README.txt").write_text("not a runbook")
    (fixture_runbooks / ".gitkeep").write_text("")

    store = _FakeStore()
    await seed_runbooks(fixture_runbooks, store)

    keys = {k for _, k in store.items}
    assert "README" not in keys
    assert ".gitkeep" not in keys
    assert len(store.items) == 2  # only the two .md fixtures
