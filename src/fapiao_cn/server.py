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
from contextlib import asynccontextmanager
from pathlib import Path
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
from fapiao_cn.editions import (
    Feature,
    capabilities_payload,
    feature_not_available_payload,
    get_entitlements,
    require_feature,
    resolve_entitlements,
    set_entitlements,
)
from fapiao_cn.formats import detect_format, parse_pdf
from fapiao_cn.input import resolve_input
from fapiao_cn.parsers import get_format_descriptions
from fapiao_cn.pipeline import run as run_pipeline
from fapiao_cn.plugins import load_plugin
from fapiao_cn.schemas import CustomsDeclarationHeader, ExtractResponse, PdfRaw
from fapiao_cn.utils import truncate as _truncate_msg
from fapiao_cn.verify import VerifyRequest, verify_invoice

log = logging.getLogger("fapiao_cn.server")

# /formats descriptions are sourced from the parser registry (H7)
# so adding a new parser automatically surfaces here.  注意这里是
# **每次请求现取**而不是模块级常量：企业版插件会在运行期往注册表里
# 注入报关单规则（未授权时再摘掉），模块级快照会漏掉这次变化。


def _resolve_app_root() -> Path:
    """H22/H50：解析与进程 cwd 无关的“应用根目录”。

    ``/health/full`` 的磁盘可写探针与 samples 存在性检查都需要一个稳定基准。

    此处原先用 ``Path(__file__).resolve().parent.parent``，但 ``server.py`` 位于
    ``src/fapiao_cn/``，**少上了一层**，实际基准是 ``src/`` 而不是仓库根。后果：
    ``samples_dir_exists`` 恒为 False（真实样本在仓库根的 ``samples/``），
    磁盘探针也会在 ``src/logs/`` 凭空建目录。就绪探针因此永远 503。

    优先 ``FC_APP_ROOT``（Docker 镜像里设为 ``/app``，即日志卷挂载点）——
    wheel 安装后 ``__file__`` 落在 site-packages，无法再向上定位到 ``/app``。
    未设置时按源码布局从本文件向上两级（``src/fapiao_cn/server.py`` → 仓库根）推导。
    """
    if settings.app_root:
        return Path(settings.app_root).resolve()
    return Path(__file__).resolve().parents[2]


# Field dictionary for the customs-declaration schema, returned by
# ``GET /formats/customs``.  Each entry pairs the wire name with the
# Chinese label, the Python type and a short description so a UI can
# render the form without hard-coding.
_CUSTOMS_FIELD_SCHEMA = (
    ("file", "string", "上传文件名"),
    ("page_count", "int", "页数"),
    ("declaration_variant", "string", "联次（操作联 / 出口退税联）"),
    ("direction", "string", "进出口（出口 / 进口）"),
    ("customs_no", "string", "海关编号"),
    ("pre_entry_no", "string", "预录入编号"),
    ("record_no", "string", "备案号"),
    ("export_date", "date", "出口日期 (YYYY-MM-DD)"),
    ("declaration_date", "date", "申报日期 (YYYY-MM-DD)"),
    ("print_date", "date", "打印日期 (YYYY-MM-DD)，退税联独有"),
    ("shipper_name", "string", "境内发货人 / 境内收发货人 (卖家)"),
    ("shipper_tax_id", "string", "境内发货人统一社会信用代码"),
    ("consignee_name", "string", "境外收货人 (买家；退税联无此字段)"),
    ("consignee_tax_id", "string", "境外收货人统一社会信用代码"),
    ("producer_name", "string", "生产销售单位"),
    ("producer_tax_id", "string", "生产销售单位统一社会信用代码"),
    ("declarant_name", "string", "申报单位 (报关代理)"),
    ("declarant_tax_id", "string", "申报单位统一社会信用代码"),
    ("customs_office_code", "string", "关区代码 (出境关别 / 出口口岸)"),
    ("customs_office_name", "string", "关区名称"),
    ("transport_mode", "string", "运输方式"),
    ("transport_mode_code", "string", "运输方式代码"),
    ("vessel_or_flight", "string", "运输工具名称及航次号"),
    ("bill_of_lading_no", "string", "提运单号"),
    ("supervision_mode_code", "string", "监管方式代码 (0110=一般贸易)"),
    ("supervision_mode_name", "string", "监管方式名称"),
    ("tax_exemption_code", "string", "征免性质代码 (101=一般征税)"),
    ("tax_exemption_name", "string", "征免性质名称"),
    ("settlement_method", "string", "结汇方式 (退税联独有)"),
    ("license_no", "string", "许可证号"),
    ("approval_no", "string", "批准文号 (退税联独有)"),
    ("contract_no", "string", "合同协议号"),
    ("trade_country_code", "string", "贸易国（地区）代码"),
    ("trade_country_name", "string", "贸易国（地区）名称"),
    ("destination_country_code", "string", "运抵国（地区）代码"),
    ("destination_country_name", "string", "运抵国（地区）名称"),
    ("destination_port_code", "string", "指运港代码"),
    ("destination_port_name", "string", "指运港名称"),
    ("exit_port_code", "string", "离境口岸代码 (出口联独有)"),
    ("exit_port_name", "string", "离境口岸名称"),
    ("source_region_code", "string", "境内货源地代码"),
    ("source_region_name", "string", "境内货源地名称"),
    ("package_type_code", "string", "包装种类代码"),
    ("package_type_name", "string", "包装种类名称"),
    ("packages_count", "string", "件数"),
    ("gross_weight_kg", "string", "毛重（千克）"),
    ("net_weight_kg", "string", "净重（千克）"),
    ("trade_term_code", "string", "成交方式代码"),
    ("trade_term_name", "string", "成交方式名 (FOB / C&F / CIF)"),
    ("freight", "string", "运费"),
    ("insurance", "string", "保费"),
    ("misc_charges", "string", "杂费"),
    ("attached_docs", "string", "随附单证及编号 / 随附单据"),
    ("container_no", "string", "集装箱号 (退税联独有)"),
    ("container_summary", "string", "集装箱标箱数及号码 (操作联独有)"),
    ("marks_and_remarks", "string", "标记唛码及备注"),
    ("declarant_person", "string", "报关人员"),
    ("declarant_person_id", "string", "报关人员证号"),
    ("declarant_phone", "string", "电话"),
    ("item_count", "int", "商品项数 (跨页累计)"),
    ("currency", "string", "主导币制 (如 USD)"),
    ("total_amount", "string", "总价累加 (原值)"),
    ("items", "CustomsDeclarationItem[]", "商品明细数组"),
)


def _customs_field_schema() -> list:
    """Serialize the schema entries as a list of dicts for ``/formats/customs``."""

    return [
        {"name": name, "type": typ, "description": desc}
        for name, typ, desc in _CUSTOMS_FIELD_SCHEMA
    ]


# ── 企业版占位路由 ────────────────────────────────────────────────
# 开源版里没有 fapiao_cn_ee 发行包，报关单与批量提取**只有路径没有实现**：
# 路由照样注册（两版路由表一致，客户端不会因为版本差异拿到 404），
# 但一律返回 403 feature_not_available。真正的实现在企业版发行包里。
def _register_customs_unavailable(app: FastAPI) -> None:
    """注册 ``/extract/customs`` 的 403 占位路由。"""

    @app.post(
        "/extract/customs",
        response_model=CustomsDeclarationHeader,
        dependencies=[
            Depends(auth_required("extract")),
            Depends(require_feature(Feature.CUSTOMS)),
        ],
        openapi_extra={"x-required-edition": "ee"},
    )
    async def extract_customs_unavailable(request: Request):
        # 依赖 require_feature 会先抛 403；这里是兜底，防止有人绕过依赖
        # 直接调函数。
        raise HTTPException(
            status_code=403,
            detail=feature_not_available_payload(Feature.CUSTOMS),
        )


def _register_batch_unavailable(app: FastAPI) -> None:
    """注册 ``/extract/batch`` 的 403 占位路由。"""

    @app.post(
        "/extract/batch",
        dependencies=[
            Depends(auth_required("extract")),
            Depends(require_feature(Feature.BATCH)),
        ],
        openapi_extra={"x-required-edition": "ee"},
    )
    async def extract_batch_unavailable(request: Request):
        raise HTTPException(
            status_code=403,
            detail=feature_not_available_payload(Feature.BATCH),
        )


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

    # ── 发行版 / 能力解析 ─────────────────────────────────────────
    # 版本**不能**由环境变量决定：开源版用户改一个 FC_EDITION 就能解锁
    # 企业版能力，整套商业边界就作废。版本只由两件事决定：
    #   1. 企业版发行包 fapiao_cn_ee 在不在（不在 → 开源版）；
    #   2. 许可证有效不有效（无效 → 同样降级成开源版行为）。
    plugin = load_plugin()
    entitlements = resolve_entitlements(plugin)
    set_entitlements(entitlements)
    if plugin is not None:
        try:
            plugin.sync_parsers(entitlements.has(Feature.CUSTOMS))
        except Exception:  # noqa: BLE001 — 插件坏了不能拖垮启动
            log.exception("failed to sync enterprise parsers; continuing without them")
    log.info(
        "edition=%s features=%s license=%s",
        entitlements.edition.value,
        ",".join(sorted(f.value for f in entitlements.features)),
        entitlements.license_id or entitlements.reason or "none",
    )

    # SIGHUP 是 POSIX-only 信号：Windows 上 ``signal.SIGHUP`` 根本不存在，
    # 读取属性本身就会抛 AttributeError（在进入 add_signal_handler 之前），
    # 因此这里必须先探测信号是否存在，而不能只靠捕获 add_signal_handler 的异常。
    _sighup = getattr(signal, "SIGHUP", None)

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
            # 许可证续期 / 换证：SIGHUP 后立即生效，不用重启容器。
            if plugin is not None:
                try:
                    fresh_ent = resolve_entitlements(plugin)
                    set_entitlements(fresh_ent)
                    plugin.sync_parsers(fresh_ent.has(Feature.CUSTOMS))
                    log.info(
                        "SIGHUP received; reloaded license "
                        "(edition=%s features=%s license=%s)",
                        fresh_ent.edition.value,
                        ",".join(sorted(f.value for f in fresh_ent.features)),
                        fresh_ent.license_id or fresh_ent.reason or "none",
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error("SIGHUP reload (license) failed: %s", exc)

        if _sighup is not None:
            try:
                loop.add_signal_handler(_sighup, _reload_on_sighup)
            except (NotImplementedError, RuntimeError):
                # Windows / non-main thread — silently skip.
                pass
        yield
        if _sighup is not None:
            try:
                loop.remove_signal_handler(_sighup)
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

        # H22: 可写日志目录是 /extract 留下审计记录的前提。用与 cwd 无关的
        # app root 定位，避免探针在 src/ 下凭空建目录。
        try:
            log_dir = _resolve_app_root() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            test_file = log_dir / ".health_probe"
            test_file.write_text("ok")
            test_file.unlink()
            checks["disk_writable"] = True
        except Exception:
            checks["disk_writable"] = False

        # H27: parsers_count derived from the registry, not a hard-coded
        # literal that drifts as parsers get added.
        from fapiao_cn.parsers import _RULES as _PARSER_RULES  # noqa: PLC0415
        checks["parsers_count"] = len(_PARSER_RULES) + 1  # +1 for the XML parser
        checks["samples_dir_exists"] = (_resolve_app_root() / "samples").is_dir()
        # 发行版 / 能力：部署形态一眼可见。注意这些是**非布尔**字段，
        # 不参与就绪判定 —— 许可证无效时正确的行为是降级成 CE 继续服务，
        # 而不是让容器不健康。
        ent = get_entitlements()
        checks["edition"] = ent.edition.value
        checks["edition_features"] = sorted(f.value for f in ent.features)
        checks["license_status"] = "active" if ent.license_id else ent.reason
        checks["uptime_seconds"] = int(time.time() - _START_TIME)
        cache_stats = get_cache().stats()
        # H18: warn when the cache hit ratio is suspiciously low.
        if (
            cache_stats["capacity"] > 0
            and cache_stats["hits"] + cache_stats["misses"] > 50
        ):
            checks["cache_low_hit_ratio"] = cache_stats["hit_ratio"] < 0.05
        checks["cache"] = cache_stats

        # 就绪判定。这里有两类语义相反的布尔，不能直接丢进同一个 all()：
        #
        #   通过型检查 (pass check)：True = 正常。False 才算不健康。
        #   告警位   (warning flag)：True = **触发了**告警，即不健康。
        #
        # 早期实现把两者混在 ``all(bool_checks)`` 里，语义正好抵消，导致 H18 的
        # 低命中率告警永远无法把就绪状态打成 degraded（当时被恒为 False 的
        # samples_dir_exists 意外掩盖）。这里显式区分。
        #
        # H50: ``samples/`` 是 .gitignore 掉的开发/CI 素材（真实发票含敏感购销方
        # 数据），镜像内不 COPY、容器里本就不存在。它缺失不代表服务不可用，
        # 因此只作信息展示，不参与判定——否则生产环境该探针必然 503。
        _NON_GATING_CHECKS = {"samples_dir_exists"}
        _WARNING_FLAGS = {"cache_low_hit_ratio"}

        failing: list[str] = []
        for key, value in checks.items():
            if not isinstance(value, bool) or key in _NON_GATING_CHECKS:
                continue
            if key in _WARNING_FLAGS:
                if value:
                    failing.append(key)
            elif not value:
                failing.append(key)

        ok = not failing
        if not ok:
            log.warning("readiness degraded; failing checks: %s", ", ".join(sorted(failing)))
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "checks": checks},
        )

    @app.get("/formats", dependencies=[Depends(auth_required("extract"))])
    def list_formats():
        """Return the list of supported invoice types.

        现取注册表：企业版插件会在运行期注入/摘除报关单规则，模块级
        快照会漏掉这次变化。
        """
        return {"formats": get_format_descriptions()}

    @app.get("/capabilities")
    def capabilities():
        """报告当前发行版与可用能力。

        前端据此显示/隐藏企业版入口（批量提取、报关单解析）——这是
        「看得见的墙」：用户看得见能力边界，也知道该买什么。
        """
        return capabilities_payload()

    @app.get(
        "/formats/customs",
        dependencies=[Depends(auth_required("extract"))],
    )
    def list_customs_format():
        """Return the field dictionary for the customs-declaration schema.

        Surfaces every field of ``CustomsDeclarationHeader`` plus its
        Chinese label so downstream consumers can build a UI without
        hard-coding field names.
        """
        return {"fields": _customs_field_schema()}

    # 报关单解析是企业版能力：插件在 → 注册真实实现；不在 → 注册 403
    # 占位路由（路径保持一致，客户端不会因为版本差异拿到 404）。
    if plugin is not None:
        plugin.register_customs_route(app)
    else:
        _register_customs_unavailable(app)

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

    # 批量提取同样是企业版能力。
    if plugin is not None:
        plugin.register_batch_route(app)
    else:
        _register_batch_unavailable(app)

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