# fapiao_cn

中国电子发票字段提取服务（HTTP API + Python lib）。基于 `pdfplumber`，覆盖 6 类票种 + XML 全电发票，无 JVM 依赖。

## 快速开始

```bash
# 生产（推荐）
docker compose up -d
curl -F file=@samples/购_材料_01.pdf http://localhost:8091/extract

# 本地开发
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
fapiao_cn-server --port 8091
```

只支持两种部署形态：

- **Docker（生产推荐）**：`docker compose up -d`，自带开机自启、崩溃重启、健康检查。
- **dev（本地开发 / CI）**：venv + `pip install -e .` + 控制台脚本或裸 uvicorn。

镜像特性：多阶段 `python:3.12-slim`、非 root、HEALTHCHECK 内置、`/app/logs` volume 挂载、时区 `Asia/Shanghai`、镜像 < 250 MB。

国内网络可切镜像源：

```bash
docker build \
  --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t fapiao_cn:1.0.0 .
```

### dev 模式

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest ruff mypy     # 可选：测试 / lint

# 启动（任选一种）
fapiao_cn start                  # 后台 + PID 文件（stop / restart / status / dev）
fapiao_cn-server --port 8091     # 前台控制台脚本
python -m uvicorn fapiao_cn.server:app --host 0.0.0.0 --port 8091

# 测试 / lint
fapiao_cn test                   # 等价 python -m pytest tests/ -v
ruff check src tests
mypy src/fapiao_cn
```

dev 模式默认需要 API Key（除 `/health`）。手动写入 `.env`：

```bash
# 生成随机 key 并写到 .env
echo "FC_API_KEYS=\"sk_dev_$(openssl rand -base64 24 | tr -d '=' | tr '/+' '_-')\"" >> .env
chmod 600 .env

# 调用时带 key
curl -H "Authorization: Bearer $(grep FC_API_KEYS .env | cut -d= -f2 | tr -d '\"')" \
  http://localhost:8091/extract -F file=@samples/购_材料_01.pdf
```

详见「API 鉴权」章节。

默认端口 `8091`，可用 `FC_PORT` 覆盖。Python 包直接调用：

```python
from fapiao_cn import parse_pdf
resp = parse_pdf(open("samples/购_材料_01.pdf", "rb").read(), filename="购_材料_01.pdf")
print(resp.seller_name, resp.total_amount, resp.message)
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | Liveness probe（公开） |
| GET | `/health/full` | Readiness probe + 依赖 / 缓存状态（需 `admin` scope） |
| GET | `/formats` | 支持的全部票种 |
| POST | `/extract` | 单文件提取 |
| POST | `/extract/batch` | 批量提取（并行，按输入顺序返回） |
| POST | `/raw` | 仅返回 PDF 原始文本，XML 拒绝 |
| POST | `/verify` | 国税查验代理（需 `FC_VERIFY_ENABLED=true`） |
| POST | `/extract-and-verify` | 提取 + 查验一站式 |

`/extract`、`/extract/batch`、`/raw` 支持 `multipart/form-data`（`file` / `files` 字段）和 `application/json`（`file_path` / `file_base64` / `url`）两种入参：

```bash
# multipart
curl -F "file=@samples/购_材料_01.pdf" http://localhost:8091/extract

# JSON
curl -H "Content-Type: application/json" \
  -d '{"file_path": "samples/购_材料_01.pdf"}' \
  http://localhost:8091/extract

# /raw 响应
# {"text": "...", "lines": ["..."], "page_count": 1}
```

`/extract/batch` 限制：最多 50 个文件、单文件 10 MB、总大小 500 MB。

## 输出字段

```json
{
  "file": "购_材料_01.pdf",
  "page_count": 1,
  "invoice_type": "电子发票（增值税专用发票）",
  "invoice_number": "25502000000129241041",
  "issue_date": "2025-11-27",
  "buyer_name": "...",
  "buyer_tax_id": "...",
  "seller_name": "...",
  "seller_tax_id": "...",
  "amount_without_tax": "124018.66",
  "tax_amount": "16122.43",
  "total_amount": "140141.09",
  "total_amount_upper": "壹拾肆万零壹佰肆拾壹圆零玖分",
  "payee": "", "reviewer": "", "drawer": "李春梅",
  "message": "success"
}
```

### `message` 字段语义

`message` 统一表示成功 / 软警告 / 硬错误：

| 值 | HTTP | 含义 |
| --- | --- | --- |
| `"success"` | 200 | 成功 |
| `"not_text"` | 200 | 空 PDF / 图片型 PDF 无文本层（软警告） |
| `"error: empty_file"` | 422 | 上传为空 |
| `"error: file_too_large: X > Y bytes"` | 422 | 超过大小限制 |
| `"error: parse_failed: <exc>"` | 422 | pdfplumber 解析异常 |
| `"error: xml_parse_failed: ..."` | 422 | XML 畸形 |
| `"error: internal_error: <exc>"` | 500 | 服务器内部异常 |

HTTP 422 响应体就是 `ExtractResponse` / `PdfRaw`，错误字符串完整保留在 `message` 字段。400 / 500 / 502-503 仍走 FastAPI 的 `{"detail": ...}` 信封。

## 配置

环境变量前缀 `FC_`，可通过 `.env` 或 `docker-compose.yml` 注入。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FC_HOST` / `FC_PORT` | `0.0.0.0` / `8091` | 绑定地址 / 端口 |
| `FC_CACHE_SIZE` | `128` | LRU 缓存条目数（`0` 关闭） |
| `FC_BATCH_WORKERS` | `4` | `/extract/batch` 并行线程数 |
| `FC_MAX_FILE_MB` | `32` | 单文件最大字节 |
| `FC_MAX_BATCH_FILES` | `50` | 批量最大文件数 |
| `FC_MAX_BATCH_SINGLE_MB` | `10` | 批量单文件大小 |
| `FC_MAX_BATCH_TOTAL_MB` | `500` | 批量总大小 |
| `FC_VERIFY_ENABLED` | `false` | 启用国税查验代理 |
| `FC_VERIFY_ENDPOINT` | （空） | 国税代理 URL |
| `FC_VERIFY_API_KEY` | （空） | 代理侧 Bearer token |
| `FC_VERIFY_TIMEOUT` | `15` | 代理超时（秒） |
| `FC_API_AUTH_ENABLED` | `true` | 鉴权总开关 |
| `FC_API_KEYS` | （空） | 逗号分隔 API key（simple 模式，全作用域） |
| `FC_API_KEYS_FILE` | （空） | JSON 文件路径（结构化模式） |
| `FC_API_KEY_DEFAULT_RPM` | `60` | 默认每分钟请求上限 |
| `FC_API_KEY_DEFAULT_BURST` | `10` | 默认令牌桶容量 |
| `FC_API_LEGACY_ALLOW` | `false` | 双轨兼容：未带 token 也放行（仅迁移期） |

## API 鉴权

服务端点（除 `/health`）默认需要 API Key，Header 两种形式任选：

```bash
curl -H "Authorization: Bearer $FC_KEY" http://localhost:8091/extract -F file=@samples/购_材料_01.pdf
curl -H "X-API-Key: $FC_KEY"            http://localhost:8091/extract -F file=@samples/购_材料_01.pdf
```

### 配置 Key

**Simple 模式**（推荐少量内网调用方）：

```bash
FC_API_KEYS="key-for-n8n,key-for-cli,key-for-frontend"
# 每个 key 自动拥有 extract / verify / admin 全作用域，rpm=60 / burst=10
```

> ⚠️ **Simple 模式下所有 key 自动获得 admin scope**（含 `/health/full`）。
> 多调用方场景建议改用 JSON 模式，给不同 key 分发不同 scopes。
> 「simple 模式 = 全员 admin」是显式设计，**生产部署前必须评估**。

**JSON 模式**（多租户 / 精细控制，推荐生产）：

```bash
FC_API_KEYS_FILE=/etc/fapiao_cn/keys.json
```

```json
[
  {"key": "sk_live_abc...",  "label": "n8n-prod",    "scopes": ["extract"], "rpm": 30, "burst": 5},
  {"key": "sk_live_def...",  "label": "ops-verify", "scopes": ["extract", "verify"], "rpm": 120, "burst": 20},
  {"key": "sk_admin_xyz...", "label": "ops-admin",  "scopes": ["extract", "verify", "admin"]}
]
```

字段：`key`（≥ 32 字节随机串，`openssl rand -base64 32`）/ `label`（写审计日志）/ `scopes`（`admin` 自动获得其他所有）/ `rpm` / `burst`。

### Scope → 端点

| Scope | 端点 |
| --- | --- |
| `extract` | `/extract`、`/extract/batch`、`/raw`、`/extract-and-verify`、`/formats` |
| `verify` | `/verify` |
| `admin` | `/health/full`（超集，含 extract / verify） |

### 错误码

| 状态 | 触发 | Header |
| --- | --- | --- |
| 401 | 缺 token / token 错 | `WWW-Authenticate: Bearer realm="fapiao_cn", error="invalid_token"` |
| 403 | scope 不足 | `WWW-Authenticate: Bearer realm="fapiao_cn", error="insufficient_scope"` |
| 429 | 限速 | `Retry-After: <sec>` + `WWW-Authenticate: ... error="rate_limited"` |

`/health` 始终公开，不消耗配额。`FC_API_KEYS` 与 `FC_API_KEYS_FILE` 都为空 → 启动 WARN + 全量放行（dev 兜底，**生产前必须显式配置**）。

### 审计日志

所有受保护请求（无论成功/失败）写入 `logs/audit.log`（10 MiB × 5 自动轮转）：

```
2026-09-18 10:23:45 [AUDIT] POST /extract -> 200 (42ms) key=a1b2c3d4 label=n8n-prod from=10.0.0.7 in=1258291B out=1834B
2026-09-18 10:23:46 [AUDIT] GET /formats -> 401 (0ms) key=- label=- from=10.0.0.7 in=0B out=0B reason=missing_token
```

`key_id` 是 `sha256(key)[:8]`，原 key 不落盘。Docker / K8s secret 用 mount 文件路径即可，**镜像不包含任何 key**：

```yaml
# docker-compose.yml
volumes:
  - /etc/fapiao_cn/keys.json:/app/keys.json:ro
environment:
  FC_API_KEYS_FILE: /app/keys.json
```

## 测试

```bash
fapiao_cn test                              # 控制台脚本
python -m pytest tests/ -v                  # 直接 pytest
```

235 条用例覆盖 `samples/` 下 34 个样本（29 PDF + 5 XML）。

## 解析器

| 模块 | 匹配类型 |
| --- | --- |
| `parsers/invoice.py` | 新电子发票（标准双栏、京东、京东大药房、水费、通行费变体） |
| `parsers/old_invoice.py` | 旧版电子发票（含密码区、校验码） |
| `parsers/merged_code.py` | 合并代码 / 成品油（坐标解析人名） |
| `parsers/airline_ticket.py` | 航空运输电子客票行程单 |
| `parsers/train_ticket.py` | 铁路电子客票 |
| `parsers/non_tax_receipt.py` | 中央非税收入统一票据（专利年费等） |
| `parsers/xml_invoice.py` | XML 全电发票 |
