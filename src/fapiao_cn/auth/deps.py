"""FastAPI 依赖 + 401/403/429 + RFC 6750 ``WWW-Authenticate`` 头。

用法：

    @app.post("/extract", dependencies=[Depends(auth_required("extract"))])
    async def extract(request: Request):
        ...

行为表（与 RFC 6750 对齐）：

    | 状态 | 触发                          | WWW-Authenticate                                   |
    | ---- | ----------------------------- | -------------------------------------------------- |
    | 401  | 无 token                      | Bearer realm="fapiao_cn"                           |
    | 401  | token 不匹配                  | Bearer realm="fapiao_cn", error="invalid_token"    |
    | 403  | 作用域不足                    | Bearer realm="fapiao_cn", error="insufficient_scope" |
    | 429  | 限速                          | Bearer realm="fapiao_cn", error="rate_limited"     |

审计日志：把鉴权结果以 ``request.state.auth`` / ``request.state.auth_reason``
挂出，由 :class:`_AuditMiddleware` 统一写出。这样 401/403/429/200 共享同一
条 schema，不会重复记录。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, Response

from fapiao_cn.auth.keys import (
    ALL_SCOPES,
    SCOPE_ADMIN,
    KeyRecord,
    get_key_store,
)
from fapiao_cn.auth.ratelimit import get_rate_limiter
from fapiao_cn.config import settings

log = logging.getLogger("fapiao_cn.auth")


@dataclass(frozen=True)
class AuthContext:
    """通过鉴权后注入 handler 的上下文。"""

    key_id: str
    label: str
    scopes: frozenset[str]
    record: KeyRecord

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or SCOPE_ADMIN in self.scopes


_REALM = 'fapiao_cn'


def _www_authenticate(error: Optional[str] = None, scope: str = "") -> str:
    """构造 ``WWW-Authenticate`` 头。"""

    parts = [f'Bearer realm="{_REALM}"']
    if error:
        parts.append(f'error="{error}"')
    if scope:
        parts.append(f'scope="{scope}"')
    return ", ".join(parts)


def _attach_header(exc: HTTPException, header_value: str) -> None:
    """把 ``WWW-Authenticate`` 挂到 ``HTTPException.headers``。"""

    new_headers: dict[str, str] = dict(exc.headers) if exc.headers else {}
    if "WWW-Authenticate" not in new_headers:
        new_headers["WWW-Authenticate"] = header_value
    exc.headers = new_headers


def _extract_bearer(request: Request) -> Optional[str]:
    """先 ``Authorization: Bearer``，再 ``X-API-Key``。"""

    auth_header = request.headers.get("authorization") or request.headers.get(
        "Authorization"
    )
    if auth_header:
        scheme, _, value = auth_header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        # 不识别 scheme 也算无效 → 不回退到 X-API-Key
        if scheme:
            return None
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key.strip()
    return None


def _set_reason(
    request: Request,
    *,
    key_id: Optional[str],
    label: Optional[str],
    reason: str,
) -> None:
    """供 ``_AuditMiddleware`` 读取的鉴权结果。"""

    request.state.auth_key_id = key_id
    request.state.auth_label = label
    request.state.auth_reason = reason


def auth_required(*required_scopes: str):
    """依赖工厂；返回 FastAPI ``Depends`` 可用的可调用对象。

    校验顺序：present → match → scope → rate-limit。
    """

    if not all(s in ALL_SCOPES for s in required_scopes):
        raise ValueError(
            f"unknown scope(s): {set(required_scopes) - ALL_SCOPES}; "
            f"valid: {sorted(ALL_SCOPES)}"
        )

    async def _dep(request: Request, response: Response) -> Optional[AuthContext]:
        store = get_key_store()

        # ── dev 模式：未配置 key → 全放行 ──
        if store.disabled:
            _set_reason(request, key_id=None, label=None, reason="dev_fallback")
            return None

        presented = _extract_bearer(request)

        # ── 双轨兼容：legacy 模式放行无 token 请求 ──
        if presented is None:
            if settings.api_legacy_allow:
                _set_reason(request, key_id=None, label=None, reason="legacy_allow")
                return None
            exc = HTTPException(
                status_code=401,
                detail="missing_token: provide Authorization: Bearer <key> or X-API-Key",
            )
            _attach_header(exc, _www_authenticate())
            _set_reason(request, key_id=None, label=None, reason="missing_token")
            raise exc

        # ── key 比对 ──
        rec = store.verify(presented)
        if rec is None:
            exc = HTTPException(
                status_code=401,
                detail="invalid_token: API key not recognised",
            )
            _attach_header(exc, _www_authenticate(error="invalid_token"))
            _set_reason(request, key_id=None, label=None, reason="invalid_token")
            raise exc

        # ── 作用域 ──
        missing = [
            s
            for s in required_scopes
            if s not in rec.scopes and SCOPE_ADMIN not in rec.scopes
        ]
        if missing:
            exc = HTTPException(
                status_code=403,
                detail=f"insufficient_scope: need {sorted(missing)}; have {sorted(rec.scopes)}",
            )
            _attach_header(
                exc,
                _www_authenticate(error="insufficient_scope", scope=" ".join(required_scopes)),
            )
            _set_reason(
                request,
                key_id=rec.key_id,
                label=rec.label,
                reason=f"scope:{','.join(missing)}",
            )
            raise exc

        # ── 限速 ──
        limiter = get_rate_limiter()
        ok, retry_after = limiter.consume(rec.key_id, rec)
        if not ok:
            exc = HTTPException(
                status_code=429,
                detail=f"rate_limited: retry after {retry_after}s",
            )
            _attach_header(exc, _www_authenticate(error="rate_limited"))
            new_headers: dict[str, str] = dict(exc.headers) if exc.headers else {}
            new_headers["Retry-After"] = str(int(retry_after) + 1)
            exc.headers = new_headers
            _set_reason(
                request,
                key_id=rec.key_id,
                label=rec.label,
                reason="rate_limited",
            )
            raise exc

        # 成功：把 key 信息挂到 request.state 便于 handler 进一步使用
        ctx = AuthContext(
            key_id=rec.key_id,
            label=rec.label,
            scopes=rec.scopes,
            record=rec,
        )
        request.state.auth = ctx
        _set_reason(request, key_id=ctx.key_id, label=ctx.label, reason="ok")
        return ctx

    return _dep
