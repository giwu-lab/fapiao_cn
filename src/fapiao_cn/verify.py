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

    Raises ``RuntimeError`` when disabled / unconfigured; lets
    ``httpx.HTTPError`` propagate so callers can wrap it in a 502.
    """

    if not settings.verify_enabled:
        raise RuntimeError("verify_disabled: set FC_VERIFY_ENABLED=true")
    if not settings.verify_endpoint:
        raise RuntimeError("verify_unconfigured: set FC_VERIFY_ENDPOINT")

    headers = {}
    if settings.verify_api_key:
        headers["Authorization"] = f"Bearer {settings.verify_api_key}"

    async with httpx.AsyncClient(timeout=settings.verify_timeout) as client:
        resp = await client.post(
            settings.verify_endpoint,
            json=req.model_dump(),
            headers=headers,
        )
        resp.raise_for_status()
        body = await resp.json()
        return VerifyResponse(**body)