"""Unit tests for services/governance/core/dolt.py — issue #03.

Verifies that write_audit / write_episode / write_gate_failure are async,
share a single lazily-created aiomysql connection pool, and reuse it across
calls instead of opening a new pymysql connection per write.
"""
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Governance service modules use bare `core.xxx` imports (no package prefix
# beyond `core`/`routers`), matching the WORKDIR=/app layout in its Dockerfile.
# core.config requires these env vars at import time.
os.environ.setdefault("JWT_PRIVATE_KEY_FILE", str(
    Path(__file__).resolve().parents[2] / "test-fixtures" / "jwt-test-key.pem"
))
os.environ.setdefault("ENV", "test")
os.environ.setdefault("CODE_REVIEWER_SECRET", "test-code-reviewer-secret")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/governance"))

import core.dolt as dolt  # noqa: E402

# `server.py` pulls in the full router chain, including core/metrics.py,
# which registers Prometheus counters under the same names as
# services/review_server/metrics.py (both use the process-global default
# CollectorRegistry). The two services never share a process in production,
# but in a shared pytest session — if a review_server test has already
# imported its metrics module — importing governance's `server` here raises
# ValueError: Duplicated timeseries. That collision is pre-existing and out
# of scope for issue #03, so the lifespan tests below are skipped rather than
# failing the whole file when it happens.
try:
    import server  # noqa: E402
    _SERVER_IMPORT_ERROR = None
except ValueError as e:
    server = None
    _SERVER_IMPORT_ERROR = e


class _FakeCursor:
    def __init__(self, raise_on_execute=False):
        self.executed = []
        self.raise_on_execute = raise_on_execute

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=None):
        if self.raise_on_execute:
            raise RuntimeError("boom")
        self.executed.append((query, params))


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _FakePoolAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, cursor=None):
        self.cursor = cursor or _FakeCursor()
        self.conn = _FakeConn(self.cursor)
        self.acquire_calls = 0

    def acquire(self):
        self.acquire_calls += 1
        return _FakePoolAcquireCtx(self.conn)


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch):
    """Ensure each test starts with no cached pool."""
    monkeypatch.setattr(dolt, "_pool", None, raising=False)
    yield
    monkeypatch.setattr(dolt, "_pool", None, raising=False)


def test_write_functions_are_async():
    assert inspect.iscoroutinefunction(dolt.write_audit)
    assert inspect.iscoroutinefunction(dolt.write_episode)
    assert inspect.iscoroutinefunction(dolt.write_gate_failure)


@pytest.mark.asyncio
async def test_pool_created_once_and_reused_across_writes(monkeypatch):
    fake_pool = _FakePool()
    create_pool = AsyncMock(return_value=fake_pool)
    monkeypatch.setattr(dolt.aiomysql, "create_pool", create_pool)

    await dolt.write_audit("agent-1", "tool", "srv", "req", "resp", "allow", "rule", 5, "corr-1")
    await dolt.write_audit("agent-1", "tool", "srv", "req", "resp", "allow", "rule", 5, "corr-2")
    await dolt.write_episode("agent-1", "tool", "srv", "req", "corr-1", "svc")

    assert create_pool.call_count == 1, "pool must be created exactly once and reused"
    assert fake_pool.acquire_calls == 3, "each write should acquire (and release) a pooled connection"


@pytest.mark.asyncio
async def test_write_audit_swallows_exceptions(monkeypatch):
    fake_pool = _FakePool(cursor=_FakeCursor(raise_on_execute=True))
    monkeypatch.setattr(dolt.aiomysql, "create_pool", AsyncMock(return_value=fake_pool))

    # Should not raise — existing swallow-and-log behavior preserved.
    await dolt.write_audit("agent-1", "tool", "srv", "req", "resp", "allow", "rule", 5, "corr-1")


@pytest.mark.asyncio
async def test_write_audit_issues_insert_then_dolt_commit(monkeypatch):
    fake_pool = _FakePool()
    monkeypatch.setattr(dolt.aiomysql, "create_pool", AsyncMock(return_value=fake_pool))

    await dolt.write_audit("agent-1", "tool", "srv", "req", "resp", "allow", "rule", 5, "corr-1")

    queries = [q for q, _ in fake_pool.cursor.executed]
    assert any("INSERT INTO audit_log" in q for q in queries)
    assert any("DOLT_COMMIT" in q for q in queries)


@pytest.mark.skipif(
    server is None,
    reason=f"server import collided with review_server metrics registry: {_SERVER_IMPORT_ERROR}",
)
@pytest.mark.asyncio
async def test_lifespan_warms_pool_at_startup_and_closes_on_shutdown(monkeypatch):
    """Governance's FastAPI lifespan should create the pool once at startup
    (not wait for the first request) and tear it down on shutdown."""
    init_calls = []
    close_calls = []

    async def fake_init_pool():
        init_calls.append(1)
        return object()

    async def fake_close_pool():
        close_calls.append(1)

    monkeypatch.setattr(server, "init_pool", fake_init_pool)
    monkeypatch.setattr(server, "close_pool", fake_close_pool)

    async with server.lifespan(server.app):
        assert len(init_calls) == 1

    assert len(close_calls) == 1


@pytest.mark.skipif(
    server is None,
    reason=f"server import collided with review_server metrics registry: {_SERVER_IMPORT_ERROR}",
)
@pytest.mark.asyncio
async def test_lifespan_startup_failure_does_not_crash_app(monkeypatch):
    """A transient Dolt outage at startup must not prevent governance from
    coming up — /check and other Dolt-independent endpoints still need to
    serve traffic. The pool is retried lazily on first write."""
    async def failing_init_pool():
        raise ConnectionError("dolt unreachable")

    async def fake_close_pool():
        pass

    monkeypatch.setattr(server, "init_pool", failing_init_pool)
    monkeypatch.setattr(server, "close_pool", fake_close_pool)

    async with server.lifespan(server.app):
        pass  # must not raise
