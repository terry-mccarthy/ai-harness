"""Per-process LRU index cache with watchfiles-based invalidation.

Keys are resolved absolute paths for local repos (slice 4); slice 5 will
also key git URLs by commit SHA. Each cached entry can start a daemon
thread running ``watchfiles.watch`` that evicts the entry on the first
filesystem change inside the watched directory.

``embed_index`` is idempotent (see :mod:`architect_server.search`), so the
same cached ``Index`` can be returned to bm25 and hybrid callers without
re-embedding.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from watchfiles import watch

from architect_server.search import Index

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SIZE = 10
_DEFAULT_DEBOUNCE_MS = 200


class IndexCache:
    """Thread-safe LRU cache of ``Index`` objects keyed by string."""

    def __init__(
        self,
        max_size: int = _DEFAULT_MAX_SIZE,
        watch_enabled: bool = True,
        watch_debounce_ms: int = _DEFAULT_DEBOUNCE_MS,
    ):
        self._cache: OrderedDict[str, Index] = OrderedDict()
        self._max_size = max_size
        self._watch_enabled = watch_enabled
        self._watch_debounce_ms = watch_debounce_ms
        self._lock = threading.Lock()
        self._stop_events: dict[str, threading.Event] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._cache

    def get_or_build(self, key: str, builder: Callable[[], Index]) -> Index:
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        # Build outside the lock so a slow build does not serialise readers.
        index = builder()
        with self._lock:
            existing = self._cache.get(key)
            if existing is not None:
                self._cache.move_to_end(key)
                return existing
            self._cache[key] = index
            self._cache.move_to_end(key)
            self._evict_unlocked()
            if self._watch_enabled:
                self._start_watching_unlocked(key)
        return index

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)
            self._stop_watching_unlocked(key)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            for ev in self._stop_events.values():
                ev.set()
            self._stop_events.clear()

    def _evict_unlocked(self) -> None:
        while len(self._cache) > self._max_size:
            old_key, _ = self._cache.popitem(last=False)
            self._stop_watching_unlocked(old_key)

    def _start_watching_unlocked(self, key: str) -> None:
        if key in self._stop_events:
            return
        path = Path(key)
        if not path.is_dir():
            return
        stop = threading.Event()
        self._stop_events[key] = stop
        debounce = self._watch_debounce_ms

        def _loop() -> None:
            try:
                for _changes in watch(str(path), stop_event=stop, debounce=debounce):
                    self.invalidate(key)
                    return
            except Exception:
                logger.exception("watchfiles loop for %s crashed", key)

        t = threading.Thread(target=_loop, daemon=True, name=f"cache-watch-{path.name}")
        t.start()

    def _stop_watching_unlocked(self, key: str) -> None:
        ev = self._stop_events.pop(key, None)
        if ev is not None:
            ev.set()
