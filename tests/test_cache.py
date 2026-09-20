"""Tests for the LRU parse-result cache."""

from __future__ import annotations

import os

import pytest

from fapiao_cn.cache import (
    ParseResultCache,
    get_cache,
    reset_cache_for_tests,
)
from fapiao_cn.formats import parse_pdf


class TestParseResultCache:
    def test_capacity_zero_disables_cache(self):
        cache = ParseResultCache(capacity=0)
        assert cache.get(b"x", "f") is None
        cache.put(b"x", "f", "value")
        # Put is a no-op when capacity is 0; subsequent get still misses.
        assert cache.get(b"x", "f") is None
        stats = cache.stats()
        assert stats["enabled"] is False
        assert stats["size"] == 0

    def test_miss_then_hit(self):
        cache = ParseResultCache(capacity=4)
        assert cache.get(b"alpha", "f") is None
        cache.put(b"alpha", "f", {"v": 1})
        assert cache.get(b"alpha", "f") == {"v": 1}
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_ratio"] == 0.5

    def test_capacity_evicts_oldest(self):
        cache = ParseResultCache(capacity=2)
        cache.put(b"a", "f", 1)
        cache.put(b"b", "f", 2)
        cache.put(b"c", "f", 3)
        # "a" should have been evicted (LRU)
        assert cache.get(b"a", "f") is None
        assert cache.get(b"b", "f") == 2
        assert cache.get(b"c", "f") == 3
        assert cache.stats()["size"] == 2

    def test_lru_recency_updates_on_get(self):
        cache = ParseResultCache(capacity=2)
        cache.put(b"a", "f", 1)
        cache.put(b"b", "f", 2)
        # Touch "a" so it becomes most-recently-used.
        assert cache.get(b"a", "f") == 1
        cache.put(b"c", "f", 3)  # should evict "b", not "a"
        assert cache.get(b"a", "f") == 1
        assert cache.get(b"b", "f") is None
        assert cache.get(b"c", "f") == 3

    def test_clear_resets_state(self):
        cache = ParseResultCache(capacity=4)
        cache.put(b"x", "f", 1)
        cache.get(b"x", "f")
        cache.clear()
        assert cache.stats()["hits"] == 0
        assert cache.stats()["misses"] == 0
        assert cache.get(b"x", "f") is None

    def test_thread_safety_smoke(self):
        """Concurrent put/get on the same cache should not crash."""
        import threading

        cache = ParseResultCache(capacity=64)

        def worker(i: int) -> None:
            for j in range(50):
                cache.put(f"key-{i}-{j}".encode(), "f", j)
                cache.get(f"key-{i}-{j}".encode(), "f")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No assertion needed — the test passes if no exception was raised.


class TestCacheIntegration:
    def test_parse_pdf_uses_cache_on_repeat(self, tmp_path):
        """The LRU cache must shortcut the second ``parse_pdf`` call."""
        reset_cache_for_tests(capacity=16)
        try:
            sample = os.path.join(
                os.path.dirname(__file__), "..", "samples", "购_材料_01.pdf"
            )
            with open(sample, "rb") as f:
                contents = f.read()

            r1 = parse_pdf(contents, filename="购_材料_01.pdf")
            r2 = parse_pdf(contents, filename="购_材料_01.pdf")
            assert r1.invoice_number == r2.invoice_number == "25502000000129241041"

            stats = get_cache().stats()
            assert stats["hits"] >= 1
            assert stats["misses"] >= 1
        finally:
            reset_cache_for_tests(capacity=0)

    def test_parse_pdf_does_not_cache_errors(self):
        """An error response is not stable — must not pollute the cache."""
        reset_cache_for_tests(capacity=16)
        try:
            r = parse_pdf(b"<<not valid pdf>>", filename="bad.pdf")
            assert r.message.startswith("error:")
            # stats should reflect a miss; no entry was stored.
            stats = get_cache().stats()
            assert stats["size"] == 0
        finally:
            reset_cache_for_tests(capacity=0)


@pytest.fixture(autouse=True)
def _reset_cache_between_tests():
    """Each test in this module gets a fresh cache singleton."""
    reset_cache_for_tests(capacity=16)
    yield
    reset_cache_for_tests(capacity=0)