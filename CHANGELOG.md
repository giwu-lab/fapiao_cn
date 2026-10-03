# Changelog

All notable changes to **fapiao_cn** are documented here.  The format
follows [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] - 2026-10-03

### Features
- **商品明细 `items[]`**: `/extract` 响应新增 `items` 数组，仅 **XML 全电发票**填充；PDF / OFD 恒为 `[]`。每个 `<IssuItemInformation>` 一行，含 `name` / `category`（从 `*类别*` 前缀拆分）/ `spec` / `unit` / `quantity` / `unit_price` / `amount` / `tax_rate` / `tax_amount` / `total_amount` / `tax_classification_code`（19 位税收分类编码，退税申报必填）/ `is_reversal`（红字冲销行标记）。金额一律保持字符串原值，负数与带单位数量不被破坏。`item_no` 为解析器生成的 1..N 序号（XML 不带行号）。
- **明细自洽校验**: 解析后比对「明细金额和 / 税额和 / 价税合计和」与表头，不一致时写 warning 日志。这是发现**明细被静默截断**的唯一手段——缺失的行不会报错，每一行返回出来都看起来完全正常。
- **Customs declaration**: 海关出口货物报关单专用解析器（操作联 / 出口退税联）。新增 `CustomsDeclarationHeader` / `CustomsDeclarationItem` schema；端点 `POST /extract/customs` 与 `GET /formats/customs`；多页续打（如 6 页退税联 24 个商品）通过 `ctx["full_text"]` 跨页聚合；合并文档（报关单 + 放行条）副页守卫自动忽略 放行条页。Covered by `tests/test_parsers.py::TestCustomsDeclaration`、`tests/test_pipeline.py::TestKnownCustomsDeclarations` 与 `tests/test_api.py::TestExtractCustoms` 共 33 个用例。

### Fixed
- **Windows 启动崩溃**: `server.py` 的 lifespan 无条件使用 `signal.SIGHUP`，而 Windows 没有该信号，`AttributeError` 在进入 `add_signal_handler` 之前抛出，原有的 `except (NotImplementedError, RuntimeError)` 接不住，导致 uvicorn 启动期直接退出。改为先探测信号是否存在（`getattr(signal, "SIGHUP", None)`）；POSIX 行为逐字不变。
- **H22 app root 少上一层**: `/health/full` 用 `Path(__file__).resolve().parent.parent` 定位基准，实际落在 `src/` 而非仓库根，导致 `samples_dir_exists` 恒为 `False`、磁盘探针每次在 `src/logs/` 凭空建目录，就绪探针因此永远 503。新增 `_resolve_app_root()`：优先 `FC_APP_ROOT`（镜像内置 `/app`），否则按源码布局 `parents[2]` 推导，保持 H22 要求的 cwd 无关。
- **H18 告警位语义反转**: `cache_low_hit_ratio` 是告警位（True = 触发告警 = 不健康），而 `pdfplumber` / `disk_writable` 是通过型布尔（True = 正常）。原实现把两者混进同一个 `all()`，语义互相抵消，低命中率告警从来无法降级就绪状态（此前被恒为 False 的 `samples_dir_exists` 意外掩盖）。改为显式区分通过型检查 / 告警位 / 非判定项。

### Changed
- `samples_dir_exists` 不再参与就绪判定：`samples/` 是 `.gitignore` 掉的开发/CI 素材，镜像不 COPY、容器内本就不存在，按它判定会让生产探针必然 503。
- 新增环境变量 `FC_APP_ROOT`（默认空，本地一般无需设置）。

### Added
- **`scripts/start-win.ps1`**: Windows 专用启停脚本（`start` / `-Stop` / `-Status`）。绕开 `fapiao_cn` CLI 在 Windows 上的两个缺陷——`cli.py::_alive()` 用 `os.kill(pid, 0)` 探活会抛 `OSError: [Errno 22]`（Windows 无信号 0），导致 `status` 误报 stale、`stop` 只删 PID 文件却不杀进程；以及 venv 的 `Scripts\python.exe` 会再 fork 出真正持有 socket 的子进程，`Start-Process -PassThru` 拿到的是 wrapper PID。脚本改用 TCP 监听表判断存活与反查真实监听 PID，启动前预检解释器能否 import 依赖，端口被非本项目进程占用时拒绝启动与终止。

### Tests
- 新增 5 条 `items[]` 回归测试：字段映射与 `*类别*` 拆分、红字冲销行负值与缺失字段、38 行跨页累加、**全部 5 个 XML 样本金额自洽**、以及锁死「PDF / OFD 的 `items` 必须为空」以防 OFD 日后被误开启。
- Test count: **303 → 376** passing (1 failed: `test_cli.py::TestAtomicPidWrite::test_writes_with_mode_0600` 在 Windows 上不适用——`st_mode` 恒为 `0o666`，POSIX 权限位未实现)。
- `mypy src/fapiao_cn`：27 文件零问题。`ruff check src tests`：仅剩 `tests/test_parsers.py` 一处既有 E501（中文样本数据行超长）。

## [1.1.0] - 2026-10-02

### Security
- **H1 LFI**: `/extract` `file_path` input now gated by `FC_ALLOWED_FILE_PATHS` (CSV directory whitelist). Symlinks and `../` escapes are blocked via `Path.resolve()` + `is_relative_to()`. Empty whitelist disables `file_path` entirely. (#1)
- **H2 SSRF**: `/extract` URL fetches now block cloud-metadata destinations (`169.254.169.254`, `fd00:ec2::254`). Other internal networks are delegated to Docker / K8s network policy per agreed risk model. Response body is streamed with a `max_file_mb` byte cap. (#2)
- **H3 OOM**: `file_base64` decoding now size-checked **before** allocation so a 1 GB payload no longer forces a 1 GB allocation. (#3)
- **H14 XXE**: XML parser now uses `defusedxml.ElementTree` instead of the stdlib `xml.etree`, blocking external-entity and billion-laughs attacks. New runtime dependency: `defusedxml>=0.7.1`. (#14)
- **H10/H47 production guard**: `FC_REQUIRE_CONFIGURED_KEYS=true` (default) now causes uvicorn to fail fast at startup when no keys are configured, preventing accidental dev-fallback in production. Devs opt out with `FC_REQUIRE_CONFIGURED_KEYS=false`. (#10, #47)

### Reliability
- **H4**: Batch worker `_parse_one`, `/extract`, `/extract-and-verify`, `/verify` now log full tracebacks via `log.exception` instead of silently swallowing them. (#4)
- **H8**: docker-compose healthcheck interval restored to `30s` (was `3600s`). (#8)
- **H13**: New `utils.truncate(value, limit=200)` clamps error-message echoes; `PdfminerException` trailer text no longer leaks onto the wire. All error-producing endpoints (`/extract`, `/raw`, `/verify`, `/extract-and-verify`) routed through the helper. (#13)
- **H5**: `BaseHTTPMiddleware` replaced with a pure ASGI middleware that captures response status + headers via a custom `send` wrapper. Streaming / SSE / JSONResponse all flow through unchanged. (#5)
- **H6**: SIGHUP reload handler hot-reloads `KeyStore` (and LRU cache via H42) so operators can rotate `keys.json` without restarting the service. (#6)
- **H11**: `/verify` proxy now streams the upstream body via `aiter_bytes` and enforces a 10 MiB cap, so a malicious proxy cannot OOM the service. (#11)
- **H18**: `/health/full` now flags `cache_low_hit_ratio=True` when the LRU hit ratio drops below 5% over 50+ lookups (503 degraded). (#18)
- **H22**: `/health/full` resolves `logs/` and `samples/` relative to the installed package, not the process cwd. (#22)
- **H42**: New `cache.reload_if_settings_changed()` rebuilds the LRU singleton on SIGHUP when `FC_CACHE_SIZE` changes. (#42)

### CLI / Deployment
- **H20**: `fapiao_cn start` now probes `(host, port)` via `SO_REUSEADDR` and refuses to spawn uvicorn when the port is busy. (#20)
- **H23**: PID file is now written via `tempfile + os.replace()` with `0600` mode so concurrent readers cannot observe partial writes. (#23)
- **H28**: CLI docstring notes that production crash-restart relies on Docker / systemd / supervisord rather than an in-process supervisor. (#28)
- **H45**: Dockerfile builder stage now splits `pyproject.toml` from `src/` so a source-only change doesn't invalidate the (slow) pip download layer. (#45)
- **H46**: `fapiao_cn start` / `dev` now pin `cwd` to the package root so log paths and `/health/full` file lookups are deterministic. (#46)

### Configuration
- **H39**: KeyStore loading moved from lifespan into `create_app()` itself, so uvicorn fails fast at boot — not on the first request — when `FC_API_KEYS_FILE` is misconfigured. (#39)

### Parsers
- **H7**: `/formats` descriptions now sourced from the parser registry via `parsers.get_format_descriptions()` so adding a new parser surfaces documentation automatically. (#7)
- **H9/H34**: Documented that `_KNOWN_TAX_IDS` is process-local; multi-worker / multi-pod deployments don't share the cache. Marked as best-effort, not a contract (GDPR / PIPL advisory). (#9, #34)
- **H12**: `KeyRecord.verify_rpm` field + per-scope rate limiting so `/verify` has its own quota independent of `/extract`. New env: `FC_API_KEY_DEFAULT_VERIFY_RPM` (default 30). (#12)
- **H15/H40**: Expanded company-name keyword set (集团/银行/医院/学校/事务所/工作室/酒店/餐厅/商店) plus a negative list (部/办公室/办事处/中心) so department labels aren't mistaken for company names. (#15, #40)
- **H21**: New e-invoice parser now recognises `名称含义` / `名称查重` / `销售方名称` as parts of a name rather than as separators. (#21)
- **H25**: `_INVOICE_NO_EMBEDDED_RE` anchored with `(?=\s|$)` so it doesn't grab digits from a later line. (#25)
- **H26**: `_TOTAL_CN_RE` capture now stops at `备注` / `小写` so a 大写 amount in the 备注 row can no longer shadow the actual total. (#26)
- **H30**: `train_ticket` tax-id regex lengthened to 15-22 chars. (#30)

### Tests / Tooling
- **H32**: pytest `filterwarnings` extended to silence the `anyio.abc.BlockingPortal` deprecation. (#32)
- **H37**: `tests/conftest.py` now hard-fails if `FC_API_KEYS_FILE` is set, preventing accidental prod-key use. (#37)
- **H43**: README documents that the XML parser's field names are hard-coded against the current `EInvoice` schema; schema upgrades require manual sync. (#43)
- Test count: **235 → 303** passing (2 xfailed).

### Migration Notes
```bash
# 1) Add new dependency
pip install -e .

# 2) Production hardening (one-time):
#    - 显式设置 FC_API_KEYS 或 FC_API_KEYS_FILE (H10)
#    - 设置 FC_ALLOWED_FILE_PATHS=/srv/inbox (H1)
#    - (可选) 升级 keys.json 加入 verify_rpm 字段 (H12)
#
# 3) 重新构建镜像以引入 defusedxml (H14)
docker compose build --pull
```

### Breaking
- `pdf_raw` field removed from `/extract`, `/extract/batch`, `/extract-and-verify`, and `parse_pdf()` response. Callers passing `?raw=true` on the regular endpoints will no longer receive a raw-text payload (the query parameter is silently ignored).

### Added
- `POST /raw` — dedicated endpoint returning only the PDF raw text payload (`text` / `lines` / `page_count`). XML inputs are rejected with `400 raw_unsupported: pdf_only`. Useful for debugging a PDF outside the parse pipeline.
- `PdfRaw.message` field (default `"success"`), so the `/raw` endpoint can surface 422 errors via the `message` field just like `/extract`.
- **API 鉴权** — 受保护端点（除 `/health`）需要 API Key
  - Bearer Token / `X-API-Key` 双形式，按 `key_id` 维度限速
  - Simple 模式：`FC_API_KEYS="k1,k2,k3"` 逗号分隔，全作用域
  - JSON 模式：`FC_API_KEYS_FILE=...` 支持 `label` / `scopes` / `rpm` / `burst` 精细配置
  - 作用域：`extract` / `verify` / `admin`（admin 为超集）
  - 错误响应符合 RFC 6750：`WWW-Authenticate: Bearer realm="fapiao_cn", error="..."`
  - 审计日志 `logs/audit.log`（10 MiB × 5 自动轮转）
  - Dev 兜底：未配 key 时启动 WARN 仍放行（迁移友好）
  - 双轨兼容开关 `FC_API_LEGACY_ALLOW`（仅迁移期使用）
  - 25 条专项测试 (`tests/test_auth.py`)
- **`fapiao_cn gen-key`** — dev 一键生成 API key（`secrets.token_urlsafe`，32-char base64）
  - `--write-env` / `-w` 自动写入 `.env`；已存在 `FC_API_KEYS` 时不覆盖，仅提示手动合并
  - 新建 `.env` 自动收紧到 `0600` 权限

### Removed
- **`fapiao_cn gen-key`** — CLI 子命令删除（功能过于狭窄，与 `openssl rand` 等价）
  - 改用 `openssl rand -base64 24` + 手写 `.env` 即可
  - 删 `tests/test_cli.py`（4 条测试随之移除）

### Changed
- PID file moved from `.fapiao_cn.pid` (project root) to `logs/fapiao_cn.pid` — keeps all runtime artefacts under the logs directory. `.gitignore`, `.dockerignore`, `scripts/install_portable.sh`, and `build-scripts/package_portable.py` updated accordingly.
- `/extract`, `/extract-and-verify`, `/raw` HTTP 422 responses now return the full `ExtractResponse` / `PdfRaw` body with the complete error string in `message` (including the `error: ` prefix). Previously the error string was stripped of the prefix and stuffed into FastAPI's `{"detail": ...}` envelope, which contradicted the documented client contract. 400 / 500 / 502 / 503 behaviour unchanged — those still use `{"detail": ...}` since they correspond to request-structure errors or proxy errors, not parse errors.
- LRU cache key simplified to `sha256(contents)[:32] : filename` (no more `with_raw` dimension). `ParseResultCache.get` / `put` signatures updated.
- `formats.detect_format` is now public (renamed from `_detect_format`) so the `/raw` endpoint can reuse the format-sniffing logic.

### Removed
- **部署形态合并**：只保留 dev + Docker 两种部署方式
  - 删 `parse_vevn/`（in-tree venv，~180 MiB）
  - 删 `scripts/setup.sh` / `scripts/cli.sh` / `scripts/lint.sh`
  - 删 `scripts/install_portable.sh` / `scripts/uninstall_systemd.sh` / `scripts/build-portable.sh`
  - 删 `scripts/fapiao_cn.service.template` / `scripts/fapiao_cn.supervisor.conf`
  - 删 `build-scripts/`（`package_portable.py` / `verify_portable.py`）
  - 改 `README.md`：删 systemd / Supervisor / portable tarball 三段，TL;DR + 快速开始改为 Docker / dev 双形态
  - 改 `pyproject.toml` / `.gitignore` / `.dockerignore`：去掉 `parse_vevn` 引用
  - 改 `src/fapiao_cn/cli.py` docstring：去掉 cli.sh 引用，明确只面向 dev

## [1.0.0] - 2026-09-18

### Renamed
- Project: `fpapi` → `fapiao_cn` (包名、控制台脚本、systemd unit、Docker image、env prefix 全部改名)。
  - 包导入: `import fpapi` → `import fapiao_cn`
  - CLI scripts: `fpapi` / `fpapi-server` → `fapiao_cn` / `fapiao_cn-server`
  - systemd service: `fapiao_cn.service`（unit 模板由 `scripts/install_portable.sh` 渲染）
  - Docker image: `fapiao_cn:1.0.0`
  - 环境变量前缀: `PDF_` → `FC_`（所有 `FC_HOST` / `FC_PORT` / `FC_CACHE_SIZE` 等）
  - PID 文件: `.uvicorn.pid` → `.fapiao_cn.pid`
  - 默认安装路径: `/opt/fpapi` → `/opt/fapiao_cn`

### Added
- **Docker** (`Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `deploy/prometheus.yml`)
  多阶段构建，非 root 运行，HEALTHCHECK 内置，slim 镜像
- **`/health/full` readiness probe** (E3)
  报告 pdfplumber / disk / parsers / uptime / cache 状态；失败时返回 503
- **`/verify` 国家税务总局查验代理** (C3)
  通过 `FC_VERIFY_ENABLED` / `FC_VERIFY_ENDPOINT` / `FC_VERIFY_API_KEY` 开关与配置
- **`/extract-and-verify` 一站式端点**
  提取 + 查验（默认 disable 查验时退化为普通提取）
- **LRU 解析结果缓存** (D2) `src/fapiao_cn/cache.py`
  按 `sha256(contents) + filename + with_raw` key，配置 `FC_CACHE_SIZE`（默认 128，0 关闭）
- **Batch 并行解析** (D1)
  `/extract/batch` 改为 `ThreadPoolExecutor` 并行，配置 `FC_BATCH_WORKERS`（默认 4），保持输入顺序
- **惰性 `extract_words()`** (D3)
  `PageView.words()` 首次访问时提取，后续复用缓存；普通发票解析器不再付 layout 代价
- **GitHub Actions CI** (G3) `.github/workflows/ci.yml`
  多 Python 版本（3.10/3.11/3.12）跑 ruff + pytest + mypy advisory + Docker smoke
- **mypy 类型检查** (G4)
  `pyproject.toml` 接入 `strict_optional` / `warn_unused_ignores` 等规则

### Changed
- `server.py` 重写为包含 `/health/full`、`/verify`、`/extract-and-verify`
- `pipeline.py` 引入 `PageView` 类（lazy words），API 内部传递 `ctx["page"]`
- `formats.py` 接入 LRU 缓存（只缓存成功响应，错误不缓存）
- `config.py` env prefix 改为 `FC_`，新增 `cache_size` / `batch_workers` / `verify_*` 字段
- `cli.py` PID 文件名改 `.fapiao_cn.pid`，prog 改 `fapiao_cn`
- `setup.sh` / `cli.sh` / `install_portable.sh` / `uninstall_systemd.sh` / `lint.sh` / `fapiao_cn.service.template` 全部以新名称渲染
- `pyproject.toml` name 改为 `fapiao_cn`，version 升至 `0.2.0`
- `scripts/lint.sh` 加入 ruff + mypy + pytest 三件套

### Removed
- 旧 PID 文件名 `.uvicorn.pid` 的硬编码引用（tarball 排除列表中保留兼容项以避免清理旧部署）

### Migration Notes (from fpapi)
```bash
# 旧 → 新
pip install -e .                            # 自动按新 name 装
fapiao_cn  start                            # was: fpapi start
FC_PORT=18091 fapiao_cn-server              # was: FPAPI_PORT=18091 fpapi-server
cat /etc/systemd/system/fapiao_cn.service   # was: fpapi.service
```

## [0.1.0] - 2026-09-08
- Initial release with 6 invoice types + XML + systemd + portable tarball
- 211 passing tests covering 新/旧/合并代码/航空/铁路/非税/XML 7 种格式