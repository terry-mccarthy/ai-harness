"""Tests for the per-process LRU index cache with watchfiles invalidation.

The cache returns the same ``Index`` instance across calls for the same key,
evicts the least-recently-used entry past ``max_size``, and (when watchfiles
is enabled) invalidates an entry when any file under the watched path changes.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from architect_server.cache import IndexCache
from architect_server.search import build_index


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def repo_a(tmp_path: Path) -> Path:
    root = tmp_path / "a"
    _write(root, "foo.py", "alpha beta gamma\n")
    return root


def test_cache_hit_returns_same_index(repo_a: Path):
    cache = IndexCache(max_size=10, watch_enabled=False)
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return build_index(repo_a)

    key = str(repo_a.resolve())
    idx1 = cache.get_or_build(key, builder)
    idx2 = cache.get_or_build(key, builder)
    assert idx1 is idx2
    assert calls["n"] == 1
    cache.clear()


def test_cache_evicts_lru_at_max_size(tmp_path: Path):
    cache = IndexCache(max_size=2, watch_enabled=False)
    roots = []
    for i in range(3):
        r = tmp_path / f"r{i}"
        _write(r, "f.py", f"chunk{i}\n")
        roots.append(r)

    for r in roots:
        cache.get_or_build(str(r.resolve()), lambda r=r: build_index(r))

    assert str(roots[0].resolve()) not in cache
    assert str(roots[1].resolve()) in cache
    assert str(roots[2].resolve()) in cache
    cache.clear()


def test_cache_lru_promotes_on_access(tmp_path: Path):
    """Touching an entry should move it to the MRU position so it isn't evicted next."""
    cache = IndexCache(max_size=2, watch_enabled=False)
    roots = []
    for i in range(3):
        r = tmp_path / f"r{i}"
        _write(r, "f.py", f"chunk{i}\n")
        roots.append(r)

    cache.get_or_build(str(roots[0].resolve()), lambda: build_index(roots[0]))
    cache.get_or_build(str(roots[1].resolve()), lambda: build_index(roots[1]))
    # Re-access r0 — it becomes MRU; r1 is now LRU.
    cache.get_or_build(str(roots[0].resolve()), lambda: build_index(roots[0]))
    cache.get_or_build(str(roots[2].resolve()), lambda: build_index(roots[2]))

    assert str(roots[0].resolve()) in cache
    assert str(roots[1].resolve()) not in cache
    assert str(roots[2].resolve()) in cache
    cache.clear()


def test_invalidate_removes_entry(repo_a: Path):
    cache = IndexCache(max_size=10, watch_enabled=False)
    key = str(repo_a.resolve())
    cache.get_or_build(key, lambda: build_index(repo_a))
    assert key in cache
    cache.invalidate(key)
    assert key not in cache
    cache.clear()


def test_clear_removes_all_entries(tmp_path: Path):
    cache = IndexCache(max_size=10, watch_enabled=False)
    for i in range(3):
        r = tmp_path / f"r{i}"
        _write(r, "f.py", f"chunk{i}\n")
        cache.get_or_build(str(r.resolve()), lambda r=r: build_index(r))
    assert len(cache) == 3
    cache.clear()
    assert len(cache) == 0


def test_no_watcher_started_when_watch_path_is_none(repo_a: Path):
    """Git-cloned entries (SHA keys) should not spin up a watchfiles thread."""
    cache = IndexCache(max_size=10, watch_enabled=True, watch_debounce_ms=50)
    cache.get_or_build("deadbeef" * 5, lambda: build_index(repo_a))
    assert cache._stop_events == {}
    cache.clear()


def test_watchfiles_evicts_on_file_change(repo_a: Path):
    """Writing inside the watched directory should evict the cached entry."""
    cache = IndexCache(max_size=10, watch_enabled=True, watch_debounce_ms=50)
    key = str(repo_a.resolve())
    cache.get_or_build(key, lambda: build_index(repo_a), watch_path=repo_a.resolve())
    assert key in cache

    # Let the watch thread start its filesystem listener.
    time.sleep(0.3)

    (repo_a / "foo.py").write_text("alpha beta gamma delta\n")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if key not in cache:
            break
        time.sleep(0.05)

    try:
        assert key not in cache, "watchfiles did not evict cached index within 5s"
    finally:
        cache.clear()
