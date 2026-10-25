"""Application configuration.

All values can be overridden via environment variables with prefix ``FC_``.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FC_",
        case_sensitive=False,
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    host: str = "0.0.0.0"
    port: int = 8091
    title: str = "fapiao_cn"
    version: str = "1.0.0"

    # pdfplumber does not render PDF to images; keep for future OCR fallback.
    # /extract 与 /extract/batch 共用这一个上限，调大它等于同时放宽两个入口。
    max_file_mb: int = 10

    # Batch extraction limits
    # 单文件上限不在这里：/extract 与 /extract/batch 共用 max_file_mb。
    max_batch_files: int = 50          # max files per batch request
    max_batch_total_mb: int = 500      # max total size per batch request

    # Performance
    cache_size: int = 128              # LRU parse-result cache; 0 disables
    batch_workers: int = 4             # /extract/batch thread-pool size

    # Optional 国家税务总局 verification proxy (FC_VERIFY_*)
    verify_enabled: bool = False
    verify_endpoint: str = ""
    verify_api_key: str = ""
    verify_timeout: int = 15

    # CORS — allow browser frontends (e.g. barda at :3000) to call this API
    cors_allow_origins: str = "*"            # CSV; default * for zero-config deployment
    cors_allow_methods: str = "GET,POST,OPTIONS"
    cors_allow_headers: str = "Content-Type,Authorization"

    # file_path 输入白名单 (FC_ALLOWED_FILE_PATHS) —— CSV 目录列表。
    # 留空时 file_path 输入被完全禁用(H1 LFI 防护);非空时仅允许
    # resolve() 后落在任一白名单根目录内的文件,符号链接与 ../ 同步拦截。
    allowed_file_paths: str = ""

    # 应用根目录 (FC_APP_ROOT) —— 留空时按源码布局从 server.py 向上两级推导。
    # 用于 /health/full 的磁盘可写探针与 samples 检查：这两项需要与进程 cwd
    # 无关的稳定基准（H22）。Docker 里日志卷挂在 /app/logs，wheel 安装后
    # __file__ 落在 site-packages，无法再向上定位，故由镜像显式指定为 /app。
    app_root: str = ""

    # API 鉴权 (FC_API_*)
    # 双轨模式：
    #   - FC_API_KEYS 优先（逗号分隔，简单模式，全部为 extract 作用域，用默认 rpm/burst）
    #   - 否则读 FC_API_KEYS_FILE 指向的 JSON 文件（结构化模式，可配 label/scopes/rpm/burst）
    #   - 两值都空 + api_auth_enabled=True → 启动 WARN，仍放行所有请求（dev 友好）
    #   - 两值都空 + api_auth_enabled=False → 显式关闭鉴权
    # 鉴权失败统一返回 401/403/429 并携带 RFC 6750 标准 WWW-Authenticate 头。
    api_auth_enabled: bool = True
    api_keys: str = ""
    api_keys_file: str = ""
    api_key_default_rpm: int = 60
    api_key_default_burst: int = 10
    # H12: /verify 端点单独限速（默认 30 RPM）。
    api_key_default_verify_rpm: int = 30
    # 双轨兼容：未带 token 的旧调用方是否放行（仅在 key 已配置时生效）；默认 false
    api_legacy_allow: bool = False
    # H10/H47：生产模式强制 — dev 兜底（KeyStore.disabled）默认拒绝启动。
    # 仅在显式 FC_REQUIRE_CONFIGURED_KEYS=false 时回退到 dev 模式。
    require_configured_keys: bool = True

    # 消费类别分类（FC_EXPENSE_CATEGORY_ENABLED）—— 把 InvoiceRecord 归到
    # 8 大类目（交通/住宿/餐饮/快递/通信/水费/电费/燃气/成品油）+ 兜底。
    # false 时 expense_category/label/source 三个字段恒为空字符串，
    # 不消耗额外计算（分类函数早返回）。
    expense_category_enabled: bool = True

    # 企业版许可证路径 (FC_LICENSE_FILE) —— 只被企业版发行包
    # (fapiao_cn_ee) 解释：指向一份 Ed25519 签名的 JSON 许可证。
    # 缺失 / 过期 / 签名不合法时**降级成开源版行为**（企业版端点返回
    # 403，服务本身照常工作），不拒绝启动。
    #
    # 这里刻意**没有** FC_EDITION 之类的版本开关：开源版用户改一个
    # 环境变量就能解锁企业版能力，整套商业边界就作废了。发行版只由
    # 「装了哪个发行包 + 许可证是否有效」决定。
    license_file: str = ""


settings = Settings()
