# Changelog

All notable changes to **fapiao_cn** are documented here.  The format
follows [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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