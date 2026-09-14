# Changelog

All notable changes to **fapiao_cn** are documented here.  The format
follows [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Renamed
- Project: `fpapi` → `fapiao_cn` (包名、控制台脚本、systemd unit、Docker image、env prefix 全部改名)。
  - 包导入: `import fpapi` → `import fapiao_cn`
  - CLI scripts: `fpapi` / `fpapi-server` → `fapiao_cn` / `fapiao_cn-server`
  - systemd service: `fapiao_cn.service`（unit 模板由 `scripts/install_portable.sh` 渲染）
  - Docker image: `fapiao_cn:0.2.0`
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