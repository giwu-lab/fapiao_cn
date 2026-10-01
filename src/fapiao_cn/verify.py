"""Optional 国家税务总局 invoice verification proxy.

Disabled by default; enable with ``FC_VERIFY_ENABLED=true``.  When
enabled, ``FC_VERIFY_ENDPOINT`` must point at a proxy / aggregator that
implements the actual verification logic (calling the official 国家税务总局
platform, or an internal cache, etc.).

The proxy is intentionally minimal — it just forwards the request and
returns whatever JSON the upstream returns.  We add an ``Authorization
`` ``Bearer`` header when ``FC_VERIFY_API_KEY`` is configured.
"""

from __future__ import annotations

from typing import Optional

import httpx
from pydantic import BaseModel

from fapiao_cn.config import settings

# H11: cap how many bytes the upstream may push back.  Even a malicious
# proxy cannot make us allocate more than this before we abort.
_MAX_VERIFY_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB


class VerifyRequest(BaseModel):
    """Payload sent to the verification proxy.

    Field names mirror the public ``ExtractResponse`` shape so a caller
    can pipe one into the other without remapping.
    """

    invoice_type: str = ""
    invoice_number: str = ""
    issue_date: str = ""
    total_amount: str = ""


class VerifyResponse(BaseModel):
    """Return shape — whatever the proxy emits.

    ``valid`` is the conventional signal field; ``detail`` carries
    upstream-specific extras.  ``source`` lets downstream consumers log
    which proxy answered.
    """

    valid: Optional[bool] = None
    source: str = "proxy"
    detail: dict = {}


async def verify_invoice(req: VerifyRequest) -> VerifyResponse:
    """Forward ``req`` to ``settings.verify_endpoint`` and return result.

    Raises ``RuntimeError`` when disabled / unconfigured; raises
    ``httpx.HTTPError`` on network / 4xx / 5xx; raises
    ``ValueError`` when the upstream body exceeds the size cap (H11).
    """

    if not settings.verify_enabled:
        raise RuntimeError("verify_disabled: set FC_VERIFY_ENABLED=true")
    if not settings.verify_endpoint:
        raise RuntimeError("verify_unconfigured: set FC_VERIFY_ENDPOINT")

    headers = {}
    if settings.verify_api_key:
        headers["Authorization"] = f"Bearer {settings.verify_api_key}"

    timeout = httpx.Timeout(settings.verify_timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                settings.verify_endpoint,
                json=req.model_dump(),
                headers=headers,
            )
            resp.raise_for_status()
            # Stream the body so a runaway upstream cannot allocate
            # unbounded memory.  Abort if Content-Length is known and
            # exceeds the cap, otherwise accumulate and bail mid-flight.
            content_length = resp.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > _MAX_VERIFY_RESPONSE_BYTES:
                        raise ValueError(
                            f"verify response too large: {content_length} bytes"
                        )
                except ValueError:
                    pass
            chunks: list = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_VERIFY_RESPONSE_BYTES:
                    raise ValueError(
                        f"verify response exceeded {_MAX_VERIFY_RESPONSE_BYTES} bytes"
                    )
                chunks.append(chunk)
            raw = b"".join(chunks)
    except httpx.HTTPError:
        raise

    try:
        body = __import__("json").loads(raw)
    except Exception as exc:
        raise ValueError(f"verify response not valid JSON: {exc}") from exc
    return VerifyResponse(**body)