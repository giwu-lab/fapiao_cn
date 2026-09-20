"""FastAPI application factory + ``fapiao_cn-server`` console script.

The factory pattern (``create_app``) lets tests build a fresh
application instance without triggering CLI argparse side effects.
A module-level ``app`` is also exported as a convenience for the
common ``uvicorn fapiao_cn.server:app`` / ``fapiao_cn-server`` paths.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pdfplumber.utils.exceptions import PdfminerException
from starlette.middleware.base import BaseHTTPMiddleware

from fapiao_cn.auth import KeyStoreError, audit_log, auth_required, get_key_store
from fapiao_cn.cache import get_cache
from fapiao_cn.config import settings
from fapiao_cn.formats import detect_format, parse_pdf
from fapiao_cn.input import resolve_batch_input, resolve_input
from fapiao_cn.pipeline import run as run_pipeline
from fapiao_cn.schemas import ExtractResponse, PdfRaw
from fapiao_cn.verify import VerifyRequest, verify_invoice

log = logging.getLogger("fapiao_cn.server")

# Supported invoice-type descriptions for the ``/formats`` endpoint.
_FORMATS = {
    "new": "电子发票（增值税专用发票/普通发票）- 标准新格式（含京东、水费、通行费变体）",
    "old": "电子发票（普通发票）- 旧格式（含密码区、校验码）",
    "merged_code": "电子发票（普通发票）- 合并代码/成品油变体",
    "airline": "电子发票（航空运输电子客票行程单）",
    "train": "电子发票（铁路电子客票）",
    "non_tax_receipt": "中央非税收入统一票据（电子）（如专利年费、行政事业性收费）",
}


# ── Batch thread pool (re-created lazily if worker count changes) ──
_BATCH_POOL: Optional[ThreadPoolExecutor] = None
_BATCH_POOL_WORKERS: int = 0


def _get_batch_pool(workers: int) -> ThreadPoolExecutor:
    """Return a singleton pool, recreating it if worker count changed."""

    global _BATCH_POOL, _BATCH_POOL_WORKERS
    if _BATCH_POOL is None or _BATCH_POOL_WORKERS != workers:
        if _BATCH_POOL is not None:
            _BATCH_POOL.shutdown(wait=False)
        _BATCH_POOL = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="fapiao-cn-batch"
        )
        _BATCH_POOL_WORKERS = workers
    return _BATCH_POOL


def _parse_one(contents: bytes, filename: str) -> ExtractResponse:
    """Worker entry-point: parse a single invoice, swallow exceptions."""

    try:
        return parse_pdf(contents, filename=filename)
    except Exception as exc:
        return ExtractResponse(file=filename, message=f"error: internal_error: {exc}")


# Module-level state for the /health/full readiness probe.
_START_TIME = time.time()


def create_app() -> FastAPI:
    """Return a freshly-constructed ``FastAPI`` instance.

    Splitting construction from module import keeps tests and ASGI
    servers free of CLI side effects — they call this factory explicitly
    rather than relying on a module-level instance.

    启动期预加载 :class:`KeyStore`。如果 ``FC_API_KEYS_FILE`` 配置了但加载失败
    （文件不可读 / JSON 语法错 / 0 有效 key / 类型错误），**启动失败**——绝不能
    静默回退到 dev 兜底，否则 K8s Secret 没挂上时鉴权会全部消失（H2）。
    """

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            get_key_store()
        except KeyStoreError:
            log.exception("API key store failed to load; refusing to start")
            raise
        yield

    app = FastAPI(title=settings.title, version=settings.version, lifespan=_lifespan)

    # CORS middleware — allows browser frontends (barda at :3000, etc.)
    # to POST files to /extract. Default * for zero-config deployment;
    # override via env (FC_CORS_ALLOW_ORIGINS) for production hardening.
    _allow_origins = [
        o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()
    ]
    _allow_methods = [
        m.strip() for m in settings.cors_allow_methods.split(",") if m.strip()
    ]
    _allow_headers = [
        h.strip() for h in settings.cors_allow_headers.split(",") if h.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allow_origins,
        allow_credentials=False,
        allow_methods=_allow_methods,
        allow_headers=_allow_headers,
    )

    # Audit middleware — 统一记审计日志（200 / 401 / 403 / 429 / 4xx / 5xx）。
    # auth_required 在 request.state 上挂 auth_key_id / auth_label / auth_reason，
    # 这里负责把所有结果统一落盘（避免重复记录 + 保证 schema 一致）。
    class _AuditMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            started = time.monotonic()
            response = await call_next(request)
            key_id = getattr(request.state, "auth_key_id", None)
            label = getattr(request.state, "auth_label", None)
            reason = getattr(request.state, "auth_reason", None)
            if reason is None:
                # 无 auth_required 介入（典型：/health 公开） → 标记 public
                reason = "public" if request.url.path == "/health" else "anonymous"
            audit_log(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                client=request.client.host if request.client else "?",
                key_id=key_id,
                label=label,
                bytes_in=int(request.headers.get("content-length") or 0),
                bytes_out=int(response.headers.get("content-length") or 0),
                reason=reason,
            )
            return response

    app.add_middleware(_AuditMiddleware)

    @app.get("/health")
    def health():
        """Liveness probe — always returns ``{"status":"ok"}`` if the
        process is up and accepting requests."""
        return {"status": "ok"}

    @app.get(
        "/health/full",
        dependencies=[Depends(auth_required("admin"))],
    )
    def health_full():
        """Readiness probe with dependency and metric checks.

        Returns 200 with a ``checks`` payload when healthy, 503 when
        any boolean check fails.  Useful for Kubernetes / load
        balancers that want to gate traffic on more than process
        liveness.
        """
        checks: dict = {}
        try:
            import pdfplumber  # noqa: F401
            checks["pdfplumber"] = True
        except Exception:
            checks["pdfplumber"] = False

        # Writable log directory is required for /extract to leave
        # audit traces in non-streaming mode.
        try:
            from pathlib import Path
            Path("logs").mkdir(parents=True, exist_ok=True)
            test_file = Path("logs") / ".health_probe"
            test_file.write_text("ok")
            test_file.unlink()
            checks["disk_writable"] = True
        except Exception:
            checks["disk_writable"] = False

        checks["parsers_count"] = 6  # airline/train/merged_code/non_tax_receipt/old/new (+ xml)
        checks["samples_dir_exists"] = (
            Path("samples").is_dir() if Path("samples").exists() else True
        )
        checks["uptime_seconds"] = int(time.time() - _START_TIME)
        checks["cache"] = get_cache().stats()

        bool_checks = [v for v in checks.values() if isinstance(v, bool)]
        ok = all(bool_checks) if bool_checks else True
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "checks": checks},
        )

    @app.get("/formats", dependencies=[Depends(auth_required("extract"))])
    def list_formats():
        """Return the list of supported invoice types."""
        return {"formats": _FORMATS}

    @app.post(
        "/extract",
        response_model=ExtractResponse,
        dependencies=[Depends(auth_required("extract"))],
    )
    async def extract(request: Request):
        """Extract invoice fields from an uploaded PDF.

        Accepts:
        - ``multipart/form-data`` with field ``file`` (binary PDF) **or**
          fields ``url`` / ``file_path`` / ``file_base64``
        - ``application/json`` with ``{"url": "..."}`` /
          ``{"file_path": "..."}`` / ``{"file_base64": "..."}``
        """

        contents, filename = await resolve_input(request)
        if contents is None:
            raise HTTPException(
                status_code=400,
                detail="Provide file, url, file_path, or file_base64",
            )

        try:
            resp = parse_pdf(contents, filename=filename)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content=ExtractResponse(
                    message=f"error: internal_error: {exc}"
                ).model_dump(exclude_none=True),
            )

        if resp.message.startswith("error:"):
            return JSONResponse(
                status_code=422,
                content=resp.model_dump(exclude_none=True),
            )

        return resp

    @app.post(
        "/extract/batch",
        dependencies=[Depends(auth_required("extract"))],
    )
    async def extract_batch(request: Request):
        """Extract invoice fields from multiple uploaded PDFs.

        Accepts ``multipart/form-data`` with repeated field ``files``.
        Files are parsed in parallel via a thread pool of size
        ``settings.batch_workers``; results keep the input order.
        Individual parse errors land in the ``message`` field of the
        corresponding item rather than failing the whole request.
        """

        uploaded, error_msg = await resolve_batch_input(request)
        if uploaded is None:
            raise HTTPException(status_code=400, detail=error_msg)

        workers = max(1, settings.batch_workers)
        pool = _get_batch_pool(workers)
        loop = asyncio.get_running_loop()
        futures = [
            loop.run_in_executor(pool, _parse_one, data, fname)
            for data, fname in uploaded
        ]
        results = await asyncio.gather(*futures)
        return results

    @app.post(
        "/raw",
        response_model=PdfRaw,
        dependencies=[Depends(auth_required("extract"))],
    )
    async def extract_raw(request: Request):
        """Return only the PDF raw text payload (no field extraction).

        Useful for debugging a PDF outside the parse pipeline.  Same
        input shapes as ``/extract``.  XML inputs are rejected with 400
        because the raw-text view only makes sense for PDFs.
        """

        contents, filename = await resolve_input(request)
        if contents is None:
            raise HTTPException(
                status_code=400,
                detail="Provide file, url, file_path, or file_base64",
            )
        if detect_format(contents, filename) == "xml":
            raise HTTPException(
                status_code=400,
                detail="raw_unsupported: pdf_only",
            )
        try:
            result = run_pipeline(contents, with_raw=True)
        except (ValueError, KeyError, AttributeError, PdfminerException) as exc:
            return JSONResponse(
                status_code=422,
                content=PdfRaw(message=f"error: parse_failed: {exc}").model_dump(),
            )
        return result.pdf_raw

    @app.post(
        "/verify",
        dependencies=[Depends(auth_required("verify"))],
    )
    async def verify(req: VerifyRequest):
        """Proxy a request to the optional 国家税务总局 verification endpoint.

        Disabled by default.  Enable via ``FC_VERIFY_ENABLED=true`` and
        point ``FC_VERIFY_ENDPOINT`` at your proxy / aggregator URL.
        Returns 503 when disabled or unconfigured; 502 on proxy error.
        """
        if not settings.verify_enabled:
            raise HTTPException(
                status_code=503,
                detail="verify_disabled: set FC_VERIFY_ENABLED=true to enable",
            )
        if not settings.verify_endpoint:
            raise HTTPException(
                status_code=503,
                detail="verify_unconfigured: set FC_VERIFY_ENDPOINT to a proxy URL",
            )
        try:
            return await verify_invoice(req)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"verify_proxy_error: {exc}")

    @app.post(
        "/extract-and-verify",
        dependencies=[Depends(auth_required("extract"))],
    )
    async def extract_and_verify(request: Request):
        """One-shot: parse the uploaded invoice, then call the verify
        proxy (when enabled) and return both.  Falls back to plain
        extraction when verify is disabled or fails.
        """
        contents, filename = await resolve_input(request)
        if contents is None:
            raise HTTPException(
                status_code=400,
                detail="Provide file, url, file_path, or file_base64",
            )
        try:
            resp = parse_pdf(contents, filename=filename)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content=ExtractResponse(
                    message=f"error: internal_error: {exc}"
                ).model_dump(exclude_none=True),
            )
        if resp.message.startswith("error:"):
            return JSONResponse(
                status_code=422,
                content=resp.model_dump(exclude_none=True),
            )

        out = resp.model_dump(exclude_none=True)
        if settings.verify_enabled and settings.verify_endpoint:
            try:
                v = await verify_invoice(VerifyRequest(
                    invoice_type=resp.invoice_type,
                    invoice_number=resp.invoice_number,
                    issue_date=resp.issue_date,
                    total_amount=resp.total_amount,
                ))
                out["verify_result"] = v.model_dump()
            except Exception as exc:
                out["verify_error"] = str(exc)
        return out

    return app


# Convenience for ``uvicorn fapiao_cn.server:app`` and the test suite.
app = create_app()


def main(argv: Optional[list] = None) -> int:
    """Entry point for the ``fapiao_cn-server`` console script."""

    parser = argparse.ArgumentParser(
        prog="fapiao_cn-server",
        description="Run the fapiao_cn HTTP server (uvicorn under the hood).",
    )
    parser.add_argument("--host", default=settings.host, help="bind host")
    parser.add_argument("--port", type=int, default=settings.port, help="bind port")
    parser.add_argument("--workers", type=int, default=1, help="uvicorn workers")
    parser.add_argument("--reload", action="store_true", help="enable auto-reload (dev)")
    uvicorn.run(
        "fapiao_cn.server:app",
        host=parser.parse_args(argv).host,
        port=parser.parse_args(argv).port,
        workers=parser.parse_args(argv).workers,
        reload=parser.parse_args(argv).reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())