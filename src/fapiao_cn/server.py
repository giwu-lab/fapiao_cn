"""FastAPI application factory + ``fapiao_cn-server`` console script.

The factory pattern (``create_app``) lets tests build a fresh
application instance without triggering CLI argparse side effects.
A module-level ``app`` is also exported as a convenience for the
common ``uvicorn fapiao_cn.server:app`` / ``fapiao_cn-server`` paths.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from fapiao_cn.cache import get_cache
from fapiao_cn.config import settings
from fapiao_cn.formats import parse_pdf
from fapiao_cn.input import resolve_batch_input, resolve_input
from fapiao_cn.schemas import ExtractResponse
from fapiao_cn.verify import VerifyRequest, verify_invoice

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


def _parse_one(contents: bytes, filename: str, with_raw: bool) -> ExtractResponse:
    """Worker entry-point: parse a single invoice, swallow exceptions."""

    try:
        return parse_pdf(contents, filename=filename, with_raw=with_raw)
    except Exception as exc:
        return ExtractResponse(file=filename, error=f"internal_error: {exc}")


# Module-level state for the /health/full readiness probe.
_START_TIME = time.time()


def create_app() -> FastAPI:
    """Return a freshly-constructed ``FastAPI`` instance.

    Splitting construction from module import keeps tests and ASGI
    servers free of CLI side effects — they call this factory explicitly
    rather than relying on a module-level instance.
    """

    app = FastAPI(title=settings.title, version=settings.version)

    @app.get("/health")
    def health():
        """Liveness probe — always returns ``{"status":"ok"}`` if the
        process is up and accepting requests."""
        return {"status": "ok"}

    @app.get("/health/full")
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

    @app.get("/formats")
    def list_formats():
        """Return the list of supported invoice types."""
        return {"formats": _FORMATS}

    @app.post("/extract", response_model=ExtractResponse)
    async def extract(request: Request, raw: bool = Query(False)):
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
            resp = parse_pdf(contents, filename=filename, with_raw=raw)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content=ExtractResponse(
                    error=f"internal_error: {exc}"
                ).model_dump(exclude_none=True),
            )

        if resp.error:
            raise HTTPException(status_code=422, detail=resp.error)

        return resp

    @app.post("/extract/batch")
    async def extract_batch(request: Request, raw: bool = Query(False)):
        """Extract invoice fields from multiple uploaded PDFs.

        Accepts ``multipart/form-data`` with repeated field ``files``.
        Files are parsed in parallel via a thread pool of size
        ``settings.batch_workers``; results keep the input order.
        Individual parse errors land in the ``error`` field of the
        corresponding item rather than failing the whole request.
        """

        uploaded, error_msg = await resolve_batch_input(request)
        if uploaded is None:
            raise HTTPException(status_code=400, detail=error_msg)

        workers = max(1, settings.batch_workers)
        pool = _get_batch_pool(workers)
        loop = asyncio.get_running_loop()
        futures = [
            loop.run_in_executor(pool, _parse_one, data, fname, raw)
            for data, fname in uploaded
        ]
        results = await asyncio.gather(*futures)
        return results

    @app.post("/verify")
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

    @app.post("/extract-and-verify")
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
            resp = parse_pdf(contents, filename=filename, with_raw=False)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content=ExtractResponse(
                    error=f"internal_error: {exc}"
                ).model_dump(exclude_none=True),
            )
        if resp.error:
            raise HTTPException(status_code=422, detail=resp.error)

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