"""API 鉴权子系统。

提供四块能力：
- ``KeyStore``：从 env / JSON 文件加载并校验 API key
- ``RateLimiter``：按 ``key_id`` 维度的进程内 token bucket
- ``audit_log``：受保护请求的审计日志（JSON 行）
- ``auth_required``：FastAPI 依赖工厂，统一 401/403/429 + WWW-Authenticate

调用方主要走 HTTP（n8n / curl / Python SDK / 自研前端），所以鉴权层放在 HTTP
边界（FastAPI Depends）。SDK 内部无需感知。
"""

from __future__ import annotations

from fapiao_cn.auth.audit import audit_log
from fapiao_cn.auth.deps import AuthContext, auth_required
from fapiao_cn.auth.keys import (
    SCOPE_ADMIN,
    SCOPE_EXTRACT,
    SCOPE_VERIFY,
    KeyRecord,
    KeyStore,
    KeyStoreError,
    get_key_store,
    reset_key_store_for_tests,
)
from fapiao_cn.auth.ratelimit import (
    get_rate_limiter,
    reset_rate_limiter_for_tests,
)

__all__ = [
    "AuthContext",
    "KeyRecord",
    "KeyStore",
    "KeyStoreError",
    "SCOPE_ADMIN",
    "SCOPE_EXTRACT",
    "SCOPE_VERIFY",
    "audit_log",
    "auth_required",
    "get_key_store",
    "get_rate_limiter",
    "reset_key_store_for_tests",
    "reset_rate_limiter_for_tests",
]
