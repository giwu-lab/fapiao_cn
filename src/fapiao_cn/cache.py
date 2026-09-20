"""LRU cache for parsed ``ExtractResponse``s.

Caching is keyed on ``sha256(contents)[:32] : filename`` so the same
bytes always hit the same entry regardless of which request uploaded
them first.

The cache is intentionally bounded (default 128 entries) — see
``Settings.cache_size``.  Setting ``FC_CACHE_SIZE=0`` disables it.

Thread-safe so it can be shared across uvicorn workers via a global.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Optional


class ParseResultCache:
    """Bounded LRU cache for ``parse_pdf`` results.

    ``capacity=0`` behaves as a disabled cache (all reads miss, all
    writes are no-ops).
    """

    def __init__(self, capacity: int = 128) -> None:
        self._capacity = max(0, capacity)
        self._lock = threading.Lock()
        self._data: "OrderedDict[str, object]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(contents: bytes, filename: str) -> str:
        h = hashlib.sha256(contents).hexdigest()[:32]
        return f"{h}:{filename}"

    def get(self, contents: bytes, filename: str) -> Optional[object]:
        if self._capacity == 0:
            return None
        key = self._key(contents, filename)
        with self._lock:
            if key in self._data:
                self.hits += 1
                self._data.move_to_end(key)
                return self._data[key]
            self.misses += 1
            return None

    def put(self, contents: bytes, filename: str, value: object) -> None:
        if self._capacity == 0:
            return
        key = self._key(contents, filename)
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._data),
                "capacity": self._capacity,
                "hits": self.hits,
                "misses": self.misses,
                "hit_ratio": round(self.hits / total, 4) if total else 0.0,
                "enabled": self._capacity > 0,
            }


# ── Module-level singleton ──────────────────────────────────────────
_cache: Optional[ParseResultCache] = None
_cache_lock = threading.Lock()


def get_cache() -> ParseResultCache:
    """Return the process-wide cache, creating it on first use.

    Honours ``settings.cache_size`` at creation time; subsequent calls
    return the same instance.  Use ``reset_cache_for_tests`` to swap it.
    """

    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                from fapiao_cn.config import settings
                _cache = ParseResultCache(settings.cache_size)
    return _cache


def reset_cache_for_tests(capacity: int | None = None) -> ParseResultCache:
    """Replace the singleton with a fresh instance (test-only)."""

    global _cache
    with _cache_lock:
        _cache = ParseResultCache(capacity or 0)
    return _cache