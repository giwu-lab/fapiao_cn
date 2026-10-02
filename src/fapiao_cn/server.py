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
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, MutableMapping, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pdfplumber.utils.exceptions import PdfminerException

from fapiao_cn.auth import (
    KeyStoreError,
    audit_log,
    auth_required,
    get_key_store,
    reload_key_store,
)
from fapiao_cn.cache import get_cache
from fapiao_cn.cache import reload_if_settings_changed as _reload_cache
from fapiao_cn.config import settings
from fapiao_cn.formats import detect_format, parse_pdf
from fapiao_cn.input import resolve_batch_input, resolve_input
from fapiao_cn.parsers import get_format_descriptions
from fapiao_cn.pipeline import run as run_pipeline
from fapiao_cn.schemas import ExtractResponse, PdfRaw
from fapiao_cn.utils import truncate as _truncate_msg
from fapiao_cn.verify import VerifyRequest, verify_invoice

log = logging.getLogger("fapiao_cn.server")

# /formats descriptions are sourced from the parser registry (H7)
# so adding a new parser automatically surfaces here.
_FORMATS = get_format_descriptions()


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
    """Worker entry-point: parse a single invoice, swallow exceptions.

    Per-file failures must not abort the batch, but the underlying
    traceback is logged so an operator can investigate (H4).
    """

    try:
        return parse_pdf(contents, filename=filename)
    except Exception as exc:
        log.exception(
            "batch worker failed for %s (%d bytes): %s",
            filename,
            len(contents),
            exc,
        )
        return ExtractResponse(file=filename, message=f"error: internal_error: {exc}")


# Module-level state for the /health/full readiness probe.
_START_TIME = time.time()


# ── Pure-ASGI audit middleware (H5) ───────────────────────────────
# We intentionally avoid Starlette's BaseHTTPMiddleware because it has
# known issues with streaming responses and re-entrancy.  This wrapper
# captures the response status + headers via a custom ``send`` callable
# without buffering the body, so JSONResponse / StreamingResponse / SSE
# all flow through unchanged.
class _AuditMiddleware:
    def __init__(
        self,
        app: Callable[
            [MutableMapping[str, object], Callable, Callable], Awaitable[None]
        ],
    ) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, object],
        receive: Callable[[], Awaitable[MutableMapping[str, object]]],
        send: Callable[[MutableMapping[str, object]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        status_holder: dict[str, int] = {"code": 500}
        response_headers: list = []

        async def _send(message: MutableMapping[str, object]) -> None:
            if message["type"] == "http.response.start":
                status = message.get("status", 500)
                status_holder["code"] = int(status) if isinstance(status, int) else 500
                raw_headers = message.get("headers") or []
                response_headers.extend(list(raw_headers) if isinstance(raw_headers, list) else [])
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            path_raw = scope.get("path", "?")
            method_raw = scope.get("method", "?")
            path = str(path_raw)
            method = str(method_raw)
            state = scope.setdefault("state", {})
            # Starlette's Request.state proxies writes to this same dict,
            # so auth_required()'s ``request.state.auth_key_id = …`` flows
            # through here as a dict write.
            key_id = state.get("auth_key_id") if isinstance(state, dict) else None
            label = state.get("auth_label") if isinstance(state, dict) else None
            reason = state.get("auth_reason") if isinstance(state, dict) else None
            if reason is None:
                reason = "public" if path == "/health" else "anonymous"
            client_obj = scope.get("client")
            client_str = client_obj[0] if client_obj else "?"  # type: ignore[index]
            bytes_in = -1
            raw_headers = scope.get("headers")
            if isinstance(raw_headers, list):
                for hk, hv in raw_headers:
                    if hk == b"content-length":
                        try:
                            bytes_in = int(hv)
                        except ValueError:
                            bytes_in = -1
                        break
            bytes_out = -1
            for hk, hv in response_headers:
                if hk == b"content-length":
                    try:
                        bytes_out = int(hv)
                    except ValueError:
                        bytes_out = -1
                    break
            audit_log(
                method=method,
                path=path,
                status=status_holder["code"],
                duration_ms=int((time.monotonic() - started) * 1000),
                client=client_str,
                key_id=key_id,
                label=label,
                bytes_in=bytes_in,
                bytes_out=bytes_out,
                reason=reason,
            )


def create_app() -> FastAPI:
    """Return a freshly-constructed ``FastAPI`` instance.

    Splitting construction from module import keeps tests and ASGI
    servers free of CLI side effects — they call this factory explicitly
    rather than relying on a module-level instance.

    启动期预加载 :class:`KeyStore`。如果 ``FC_API_KEYS_FILE`` 配置了但加载失败
    （文件不可读 / JSON 语法错 / 0 有效 key / 类型错误），**启动失败**——绝不能
    静默回退到 dev 兜底，否则 K8s Secret 没挂上时鉴权会全部消失（H2）。

    H10/H39/H47：KeyStore 校验在工厂构造时立即触发，违反时直接抛 KeyStoreError，
    uvicorn 启动期就会失败，不会等到第一个请求。
    """

    try:
        get_key_store()
    except KeyStoreError as exc:
        log.error("API key store failed to load; refusing to start: %s", exc)
        raise

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # Register SIGHUP for hot-reloading KeyStore (H6) and cache (H42).
        loop = asyncio.get_running_loop()

        def _reload_on_sighup() -> None:
            try:
                fresh = reload_key_store()
                log.info(
                    "SIGHUP received; reloaded %d API keys (source=%s, disabled=%s)",
                    len(fresh),
                    fresh.source,
                    fresh.disabled,
                )
            except KeyStoreError as exc:
                log.error("SIGHUP reload (keys) failed: %s", exc)
            try:
                if _reload_cache():
                    log.info("SIGHUP received; cache size adjusted")
            except Exception as exc:  # noqa: BLE001
                log.error("SIGHUP reload (cache) failed: %s", exc)

        try:
            loop.add_signal_handler(signal.SIGHUP, _reload_on_sighup)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main thread — silently skip.
            pass
        yield
        try:
            loop.remove_signal_handler(signal.SIGHUP)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

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
    # Add audit middleware last so it wraps everything (incl. CORS).
    # FastAPI applies middlewares in reverse-add order, so the outermost
    # wrapper is the *last* one added.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allow_origins,
        allow_credentials=False,
        allow_methods=_allow_methods,
        allow_headers=_allow_headers,
    )
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

        # H22: writable log directory is required for /extract to leave
        # audit traces.  Resolve relative to the package root rather
        # than the process cwd.
        try:
            from pathlib import Path
            pkg_root = Path(__file__).resolve().parent.parent
            log_dir = pkg_root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            test_file = log_dir / ".health_probe"
            test_file.write_text("ok")
            test_file.unlink()
            checks["disk_writable"] = True
        except Exception:
            checks["disk_writable"] = False

        checks["parsers_count"] = 6  # airline/train/merged_code/non_tax_receipt/old/new (+ xml)
        # H22: resolve paths relative to the fapiao_cn package, not the
        # process cwd.  ``fapiao_cn start`` may have a different cwd
        # than the package install root.
        from pathlib import Path as _Path
        pkg_root = _Path(__file__).resolve().parent.parent
        # H27: parsers_count derived from the registry, not a hard-coded
        # literal that drifts as parsers get added.
        from fapiao_cn.parsers import _RULES as _PARSER_RULES  # noqa: PLC0415
        checks["parsers_count"] = len(_PARSER_RULES) + 1  # +1 for the XML parser
        checks["samples_dir_exists"] = (pkg_root / "samples").is_dir()
        checks["uptime_seconds"] = int(time.time() - _START_TIME)
        cache_stats = get_cache().stats()
        # H18: warn when the cache hit ratio is suspiciously low.
        if (
            cache_stats["capacity"] > 0
            and cache_stats["hits"] + cache_stats["misses"] > 50
        ):
            checks["cache_low_hit_ratio"] = cache_stats["hit_ratio"] < 0.05
        checks["cache"] = cache_stats

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
            log.exception("parse_pdf failed for %s", filename)
            return JSONResponse(
                status_code=500,
                content=ExtractResponse(
                    message=f"error: internal_error: {_truncate_msg(exc)}"
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
        if detect_format(contents, filename) in ("xml", "ofd"):
            raise HTTPException(
                status_code=400,
                detail="raw_unsupported: pdf_only",
            )
        try:
            result = run_pipeline(contents, with_raw=True)
        except (ValueError, KeyError, AttributeError, PdfminerException) as exc:
            return JSONResponse(
                status_code=422,
                content=PdfRaw(message=f"error: parse_failed: {_truncate_msg(exc)}").model_dump(),
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
            log.exception("verify proxy error: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=f"verify_proxy_error: {_truncate_msg(exc)}",
            )

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
            log.exception("parse_pdf failed for %s", filename)
            return JSONResponse(
                status_code=500,
                content=ExtractResponse(
                    message=f"error: internal_error: {_truncate_msg(exc)}"
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
                log.exception("verify proxy error in extract-and-verify: %s", exc)
                out["verify_error"] = _truncate_msg(exc)
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