"""按 ``key_id`` 维度的进程内 token bucket。

每 key 独立桶：``capacity = burst``，``refill_rate = rpm / 60``（token/秒）。
线程安全（FastAPI 同步线程池 + uvicorn worker 都可能并发）。
N=1 uvicorn worker 时内存 < 1 KB/key；多 worker 部署时各算各的（本期范围）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from fapiao_cn.auth.keys import KeyRecord


@dataclass
class _Bucket:
    tokens: float
    last_refill: float
    rpm: int
    burst: int


class RateLimiter:
    """进程内 token bucket 集合。"""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def consume(self, key_id: str, rec: KeyRecord, *, cost: float = 1.0) -> tuple[bool, float]:
        """尝试消耗 ``cost`` 个 token。

        返回 ``(ok, retry_after_seconds)``；当 ``ok=False`` 时 ``retry_after`` 是
        到下一次可用所需等待的秒数（向上取整到 0.1s）。
        """

        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key_id)
            if bucket is None:
                bucket = _Bucket(
                    tokens=float(rec.burst),
                    last_refill=now,
                    rpm=rec.rpm,
                    burst=rec.burst,
                )
                self._buckets[key_id] = bucket

            # 回填
            elapsed = now - bucket.last_refill
            refill_rate = bucket.rpm / 60.0
            bucket.tokens = min(
                float(bucket.burst),
                bucket.tokens + elapsed * refill_rate,
            )
            bucket.last_refill = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0

            # 不足：算出需要等多久
            deficit = cost - bucket.tokens
            retry = deficit / refill_rate if refill_rate > 0 else 60.0
            return False, round(retry + 0.05, 1)

    def stats(self) -> dict:
        with self._lock:
            return {
                "tracked_keys": len(self._buckets),
                "buckets": {
                    kid: {
                        "rpm": b.rpm,
                        "burst": b.burst,
                        "tokens": round(b.tokens, 2),
                    }
                    for kid, b in self._buckets.items()
                },
            }


_LIMITER = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    """进程内单例。"""

    return _LIMITER


def reset_rate_limiter_for_tests() -> None:
    """测试钩子：清空所有桶。"""

    global _LIMITER
    _LIMITER = RateLimiter()
