# fapiao_cn

中国电子发票字段提取服务（HTTP API + Python lib）。基于 `pdfplumber`，覆盖 6 类票种 + XML/OFD 全电发票，无 JVM 依赖。

## 快速开始

两种部署形态：

- **Docker（生产推荐）**：`docker compose up -d`，自带开机自启、崩溃重启、健康检查。
- **dev（本地开发 / CI）**：venv + `pip install -e .` + 控制台脚本或裸 uvicorn。

镜像特性：多阶段 `python:3.12-slim`、非 root、HEALTHCHECK 内置、`/app/logs` volume 挂载、时区 `Asia/Shanghai`、镜像 < 250 MB。Dockerfile 默认走国内 APT 镜像（`APT_MIRROR=mirrors.aliyun.com`），需要切回官方源：

```bash
docker build --build-arg APT_MIRROR= -t fapiao_cn:1.0.0 .
```

### 构建方案

依赖有两条来路，产出同样的镜像：

| | `wheels/` 里有 `.whl` | `wheels/` 为空（全新克隆 / CI） |
| --- | --- | --- |
| 构建期 pip | `--no-index --find-links=/wheels`（**全程不联网**） | 从镜像源在线下载 |
| 前置步骤 | 先跑 `scripts/prepare_wheels.sh` | 无 |

Dockerfile 会自动判断走哪条，**两条路都不需要改 Dockerfile**：

```bash
# 路线 1（离线，推荐给受限网络）：一次性准备 wheels
bash scripts/prepare_wheels.sh           # 等价于 pip wheel --wheel-dir=wheels <deps>
# 或指定 PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ bash scripts/prepare_wheels.sh

docker build -t fapiao_cn:1.0.0 .
```

> wheels/ 不用每次都重生成；只有锁定依赖版本时才需要重跑。脚本会自动用 `.venv/bin/python3`（如有）或系统 `python3`。
> `wheels/` 里的 wheel 本体不入库（`.gitignore`），只保留 `wheels/.gitkeep` 占位，保证全新克隆里 `COPY wheels /wheels` 依然成立。

**受限网络（buildx sandbox）** —— BuildKit 默认把 RUN 步骤网络隔离，受限环境里 DNS 仍 `Temporary failure in name resolution`（即便镜像源配对了也不行）。此时用离线路线（路线 1）即可彻底绕开；也可以共享主机网络栈：

```bash
docker buildx create --use --driver docker-container --network host   # 一次性建 host 网络 builder
docker buildx build -t fapiao_cn:1.0.0 .                               # 或老 builder：DOCKER_BUILDKIT=0 docker build --network=host .
```

**方案 C：完全离线**（路线 1 失败 + 上面共享网络也没法用时）—— 在主机上准备好 wheels **和** 基础 deb 包（apt-offline 工具链），用本地 deb 仓库喂给镜像。绝大多数情况下路线 1 + 共享网络已够用，此处不展开。

### 生产

```bash
docker compose up -d
curl -F file=@samples/新电子发票-标准/购_材料_01.pdf http://localhost:8091/extract
```

### 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest ruff mypy     # 可选：测试 / lint

# 启动（任选一种）
fapiao_cn-server --port 8091              # 前台控制台脚本（日志到 stderr；不走 PID 管理）
python -m uvicorn fapiao_cn.server:app --host 0.0.0.0 --port 8091 --reload  # 裸前台 + reload
fapiao_cn start                           # 后台 + PID 文件（日志 logs/server.log）
fapiao_cn dev                             # 后台 + PID 文件 + --reload（日志 logs/server-dev.log）

# 测试 / lint
fapiao_cn test                            # 等价 python -m pytest tests/ -v
ruff check src tests
mypy src/fapiao_cn

# 看日志
tail -f logs/server.log                    # fapiao_cn start 模式
tail -f logs/server-dev.log                # fapiao_cn dev 模式
```

dev 模式默认需要 API Key，参见「API 鉴权」开头。

#### Windows

CI 只在 ubuntu 上跑，`fapiao_cn` CLI 的生命周期子命令在 Windows 上有两个已知问题，因此单独提供了 `scripts/start-win.ps1`：

1. `cli.py::_alive()` 用 `os.kill(pid, 0)` 探活，Windows 没有信号 0，该调用抛 `OSError: [Errno 22]`，于是 `_alive()` 恒为 `False`——`fapiao_cn status` 会把运行中的服务报成「stale pid file」，`fapiao_cn stop` 则只删 PID 文件、返回 0，**却不杀进程**。
2. venv 的 `Scripts\python.exe` 会再 fork 出真正持有 socket 的子进程，直接拿 `Start-Process -PassThru` 的 PID 写 PID 文件会导致 stop 打错进程。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-win.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start-win.ps1 -Status
powershell -ExecutionPolicy Bypass -File .\scripts\start-win.ps1 -Stop

# 解释器不在仓库 .venv 里时，显式指定
powershell -ExecutionPolicy Bypass -File .\scripts\start-win.ps1 `
  -Python D:\path\to\.venv\Scripts\python.exe
```

脚本会等 `/health` 真正返回 200 才算启动成功，从 TCP 监听表反查**真实**监听 PID 写入 `logs/fapiao_cn.pid`，并在端口被非本项目进程占用时拒绝启动 / 拒绝终止。

#### `logs/` 目录约定

`logs/` 下只会出现以下 5 类文件，**除此以外**的 `server-*.log` 都是历史临时副产物，可以删除：

| 文件 | 来源 |
| --- | --- |
| `logs/server.log` | `fapiao_cn start` 写的 uvicorn 日志（结构化 `deploy/log_config.yaml`） |
| `logs/server-dev.log` | `fapiao_cn dev` 写的 uvicorn 日志（带 `--reload`） |
| `logs/audit.log` | 受保护请求审计日志（10 MiB × 5 自动轮转） |
| `logs/fapiao_cn.pid` | `start` / `dev` 写的进程 PID；`stop` / `restart` / `status` 读它 |
| `logs/fapiao_cn.endpoint` | `start` / `dev` 写的实际绑定端点 `host:port`；`stop` / `restart` 据此等待端口释放 |

> `start --port 9000` 之后，`FC_PORT` / `settings.port` 里的默认值并不是真正占用的端口。端点文件就是为了让 `stop` / `restart` 等到**真正被占用**的那个 socket 被释放。命令行 `--host` / `--port` 优先级高于该文件。

`fapiao_cn-server`（前台 console_script）和裸 `python -m uvicorn ...`（裸前台）都不重定向 stdout，**不会写日志文件**——它们的输出直接到调用方 shell 的 stderr。

### Python 包直接调用

```python
from fapiao_cn import parse_pdf
resp = parse_pdf(open("samples/新电子发票-标准/购_材料_01.pdf", "rb").read(), filename="购_材料_01.pdf")
print(resp.seller_name, resp.total_amount, resp.message)
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | Liveness probe（公开） |
| GET | `/health/full` | Readiness probe + 依赖 / 缓存状态（需 `admin` scope） |
| GET | `/formats` | 支持的全部票种 |
| GET | `/formats/customs` | 海关出口货物报关单字段 schema |
| POST | `/extract` | 单文件提取 |
| POST | `/extract/customs` | 海关出口货物报关单提取（操作联 / 出口退税联） |
| POST | `/extract/batch` | 批量提取（并行，按输入顺序返回） |
| POST | `/raw` | 仅返回 PDF 原始文本，XML/OFD 拒绝 |
| POST | `/verify` | 国税查验代理（需 `FC_VERIFY_ENABLED=true`） |
| POST | `/extract-and-verify` | 提取 + 查验一站式 |

`/extract`、`/extract/batch`、`/raw` 支持 `multipart/form-data`（`file` / `files` 字段）和 `application/json`（`file_path` / `file_base64` / `url`）两种入参：

```bash
# multipart
curl -F "file=@samples/新电子发票-标准/购_材料_01.pdf" http://localhost:8091/extract

# JSON
curl -H "Content-Type: application/json" \
  -d '{"file_path": "samples/新电子发票-标准/购_材料_01.pdf"}' \
  http://localhost:8091/extract

# /raw 响应
# {"text": "...", "lines": ["..."], "page_count": 1}
```

`/extract/batch` 限制：最多 50 个文件、单文件 10 MB（与 `/extract` 一致，由 `FC_MAX_FILE_MB` 统一控制）、总大小 500 MB。

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
  "items": [],
  "expense_category": "transportation",
  "expense_category_label": "交通",
  "expense_category_source": "seller",
  "message": "success"
}
```

### `items[]` 商品明细

`/extract` 的响应里带一个 `items` 数组。**目前仅 XML 全电发票填充；PDF 与 OFD 恒为 `[]`**
（OFD 的明细可解析但暂未启用——其 `CustomTag.xml` 同一文件内混用「`GoodsInfos` 包装」
与「`root` 直挂」两套布局，漏掉后者会静默少数据）。

| 字段 | 来源元素 | 说明 |
| --- | --- | --- |
| `item_no` | 解析器生成 | 1..N 按出现顺序（XML 不带行号） |
| `name` | `ItemName` | 已剥离 `*类别*` 前缀 |
| `category` | 从 `ItemName` 拆分 | 如 `齿轮` |
| `spec` | `SpecMod` | 规格型号 |
| `unit` | `MeaUnits` | 计量单位 |
| `quantity` | `Quantity` | 可能是 `4333千克` 这类带单位的串 |
| `unit_price` | `UnPrice` | 单价 |
| `amount` | `Amount` | 不含税金额（红字行为负数） |
| `tax_rate` | `TaxRate` | 如 `0.13` |
| `tax_amount` | `ComTaxAm` | 税额 |
| `total_amount` | `TotaltaxIncludedAmount` | 价税合计 |
| `tax_classification_code` | `TaxClassificationCode` | 19 位税收分类编码，退税申报必填 |
| `is_reversal` | 解析器推导 | 红字冲销行标记，报税时要单独统计 |

**全部金额保持字符串原值**，不做 float 转换：红字行带负号，且税率写法在不同票种间
不统一。

⚠️ **红字冲销行**（`is_reversal: true`）只有金额三项和税收分类编码，
`spec` / `unit` / `quantity` / `unit_price` 会是空串——这是票面本身就没有，
不是解析失败。

服务端会对「明细金额和 / 税额和 / 价税合计和」与表头做**自洽校验**，不一致时写
warning 日志。这是发现「明细被静默截断」的唯一手段：少掉的行不会报错，
每一行返回出来都看起来完全正常。

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
| `"error: ofd_parse_failed: ..."` | 422 | OFD 容器/ZIP/XML 异常或非数电发票 |
| `"error: internal_error: <exc>"` | 500 | 服务器内部异常 |

HTTP 422 响应体就是 `ExtractResponse` / `PdfRaw`，错误字符串完整保留在 `message` 字段。400 / 500 / 502-503 仍走 FastAPI 的 `{"detail": ...}` 信封。

### `expense_category` 消费类别

`/extract` 响应里带 `expense_category` / `expense_category_label` / `expense_category_source` 三字段，对单张发票做企业报销常用的归类（适合按类目做预算统计、凭证归档、报销审批路由）。

| `expense_category` | `expense_category_label` | 含义 | 命中来源示例 |
| --- | --- | --- | --- |
| `transportation` | 交通 | 航空 / 铁路 / 网约车 / 出租车 | 票种 `airline`/`train`；卖方「滴滴/携华/出行/12306」；明细「交通运输服务/客运服」 |
| `lodging` | 住宿 | 酒店 / 民宿 / 公寓 | 卖方「如家/汉庭/锦江/民宿」；明细「住宿服务/房费」 |
| `dining` | 餐饮 | 餐厅 / 团餐 / 外卖 | 卖方「海底捞/美团/餐饮」；明细「餐饮服务/餐费」 |
| `courier` | 快递 | 顺丰 / 中通 / 京东物流 等 | 卖方「顺丰/京东物流/中通/圆通/申通/韵达/邮政」；明细「快递/收派服务费」 |
| `telecom` | 通信 | 三大运营商 / 广电 | 卖方「中国电信/中国移动/中国联通」；明细「电信服务/通信服务费」 |
| `water` | 水费 | 自来水 / 供水 | 卖方「自来水/供水/水资源」 |
| `electricity` | 电费 | 供电 / 电网 | 卖方「供电/电力/国网」 |
| `gas` | 燃气 | 燃气公司 / 天然气 | 卖方「燃气/港华燃气/华润燃气」；明细「燃气费」 |
| `fuel` | 成品油 | 加油站 / 中石化 / 中石油 | 票种 `merged_code`；卖方「中石化/中石油/加油站」；明细「成品油/汽油/柴油」 |
| `other` | 其他 | 票种级命中（行政事业性收费） | 票种 `non_tax_receipt` |
| `unclassified` | 未归类 | **兜底**：L1–L4 全未命中。**与 `other` 独立**——`other` 表示"识别为其他类目"，`unclassified` 表示"我们没规则命中它"。调用方按 `category == "unclassified"` 判断未识别票，按 `category == "other"` 判断显式归类的杂项。 | 所有规则未命中（`source=""`） |

`expense_category_source` 标识命中层级：

| 值 | 含义 |
| --- | --- |
| `"ticket"` | L1 票种命中（`parsers.dispatch` 直接路由） |
| `"seller"` | L2 卖方名称关键词命中 |
| `"item"` | L3 商品明细（XML 全电发票的 `items[]`）关键词命中 |
| `"code"` | L4 19 位 税分类编码前缀命中 |
| `""` | 兜底 `unclassified`（`category` 不为空字符串）；或报关单显式跳过 / `FC_EXPENSE_CATEGORY_ENABLED=false` 关闭（这两种 `category` 也是空字符串，调用方按上下文区分） |

**优先级**：`ticket` > `seller` > `item` > `code` > 兜底归 `unclassified`。同层冲突按规则 `priority` 数字从大到小取第一。完整规则表见 `src/fapiao_cn/expense_category.py`。

> **`other` vs `unclassified` 区分**：
> - `other` = "中央非税" 票种被显式归类（`source="ticket"`）—— 已知是行政事业性收费
> - `unclassified` = "没规则命中"（`source=""`）—— 卖方 / 明细 / 编码都不认识，待人工或后续 PR 补规则
>
> 报关单（`/extract/customs`）不属于企业报销场景，分类逻辑显式跳过该票种——即使卖方碰巧含「运输」「餐饮」等关键词，分类三字段也保持空字符串（与 `unclassified` 不同）。

> PDF 票面的 `items[]` 恒为空，所以 PDF 走 `seller` 路径（依赖卖方关键词）；XML 全电发票因有明细，`item` / `code` 也参与判定，**同一份内容（PDF 转 XML）可能归到不同细分类**——例如「购_零售_01」XML 因明细含「*餐饮服务*」被归到 `dining`，PDF 因无明细落到兜底空。
>
> OFD 全电票与 PDF 一样走 `seller` 路径（OFD 暂不解析明细），三条路径（PDF / XML / OFD）都会产出类目，不会出现「某个格式恒为空」的情况。

## API 鉴权

服务端点（除 `/health`）默认需要 API Key，Header 两种形式任选：

```bash
curl -H "Authorization: Bearer $FC_KEY" http://localhost:8091/extract -F file=@samples/新电子发票-标准/购_材料_01.pdf
curl -H "X-API-Key: $FC_KEY"            http://localhost:8091/extract -F file=@samples/新电子发票-标准/购_材料_01.pdf
```

dev 模式首次启动需要先写 key 到 `.env`：

```bash
echo "FC_API_KEYS=\"sk_dev_$(openssl rand -base64 24 | tr -d '=' | tr '/+' '_-')\"" >> .env
chmod 600 .env
curl -H "Authorization: Bearer $(grep FC_API_KEYS .env | cut -d= -f2 | tr -d '\"')" \
  http://localhost:8091/extract -F file=@samples/新电子发票-标准/购_材料_01.pdf
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

字段：`key`（≥ 32 字节随机串，`openssl rand -base64 32`）/ `key_id`（审计日志里 `key=` 的 8 字符 sha256 前缀；**可选填**，填了启动期必须与 `sha256(key)[:8]` 一致，否则 `KeyStoreError: mismatched_key_id`。运维可填便于一眼对应；缺省填则启动期自动派生）/ `label`（写审计日志）/ `scopes`（`admin` 自动获得其他所有）/ `rpm` / `burst`。

### Scope → 端点

| Scope | 端点 |
| --- | --- |
| `extract` | `/extract`、`/extract/batch`、`/extract/customs`、`/raw`、`/extract-and-verify`、`/formats`、`/formats/customs` |
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

```
# 在 audit.log 里看到 key=339740cb？反查对应 label + 截短的 key：
python3 scripts/show_key_ids.py keys.json
# label              key_id     key (trunc)      status
# frontend-spa       339740cb   OpEAA5V6qoPKSE…  match
```

> ⚠️ **关于本地 `keys.json` / `.env`**:两者均已 `.gitignore` 排除,不会进入 git 历史。但仓库根目录下的 `keys.json` 是开发测试副本——若与生产工作目录共用,可能误传到运维环境。**生产部署请用 secret manager / K8s Secret / Vault 注入,**不要**把 `keys.json` 复制进镜像,也不要把它和真实生产文件备份到一起。**

```yaml
# docker-compose.yml
volumes:
  - /etc/fapiao_cn/keys.json:/app/keys.json:ro
environment:
  FC_API_KEYS_FILE: /app/keys.json
```

## 配置

环境变量前缀 `FC_`，可通过 `.env` 或 `docker-compose.yml` 注入。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FC_HOST` / `FC_PORT` | `0.0.0.0` / `8091` | 绑定地址 / 端口 |
| `FC_CACHE_SIZE` | `128` | LRU 缓存条目数（`0` 关闭） |
| `FC_BATCH_WORKERS` | `4` | `/extract/batch` 并行线程数 |
| `FC_MAX_FILE_MB` | `10` | 单文件最大字节（`/extract` 与 `/extract/batch` 共用） |
| `FC_MAX_BATCH_FILES` | `50` | 批量最大文件数 |
| `FC_MAX_BATCH_TOTAL_MB` | `500` | 批量总大小 |
| `FC_VERIFY_ENABLED` | `false` | 启用国税查验代理 |
| `FC_VERIFY_ENDPOINT` | （空） | 国税代理 URL |
| `FC_VERIFY_API_KEY` | （空） | 代理侧 Bearer token |
| `FC_VERIFY_TIMEOUT` | `15` | 代理超时（秒） |
| `FC_API_AUTH_ENABLED` | `true` | 鉴权总开关 |
| `FC_API_KEYS` | （空） | 逗号分隔 API key（simple 模式，全作用域） |
| `FC_API_KEYS_FILE` | （空） | JSON 文件路径（结构化模式） |
| `FC_API_KEY_DEFAULT_RPM` | `60` | 默认每分钟请求上限（`extract` 作用域） |
| `FC_API_KEY_DEFAULT_BURST` | `10` | 默认令牌桶容量 |
| `FC_API_KEY_DEFAULT_VERIFY_RPM` | `30` | `/verify` 端点独立每分钟上限（0 关闭） |
| `FC_REQUIRE_CONFIGURED_KEYS` | `true` | dev 兜底：未配置任何 key 时是否拒绝启动（生产建议保持 true） |
| `FC_API_LEGACY_ALLOW` | `false` | 双轨兼容：未带 token 也放行（仅迁移期） |
| `FC_ALLOWED_FILE_PATHS` | （空） | `file_path` 输入白名单目录（CSV）；留空则禁用该入参 |
| `FC_APP_ROOT` | （空） | 应用根目录，仅 `/health/full` 用。留空时按源码布局推导；镜像内置为 `/app` |
| `FC_CORS_ALLOW_ORIGINS` | `*` | CORS 允许来源（CSV）；浏览器前端跨域用 |
| `FC_CORS_ALLOW_METHODS` | `GET,POST,OPTIONS` | CORS 允许方法 |
| `FC_CORS_ALLOW_HEADERS` | `Content-Type,Authorization` | CORS 允许请求头 |
| `FC_EXPENSE_CATEGORY_ENABLED` | `true` | 消费类别分类总开关；false 时三字段恒为空字符串 |

## 测试

```bash
fapiao_cn test                              # 控制台脚本
python -m pytest tests/ -v                  # 直接 pytest
```

219 条用例覆盖 `samples/` 下 41 个样本（34 PDF + 5 XML + 2 OFD）。

> `samples/` 按票种子目录组织：`新电子发票-标准/` / `新电子发票-旧格式/` / `成品油合并代码/` / `铁路电子客票/` / `航空运输电子客票/` / `中央非税收入统一票据/` / `报关单/` / `全电发票-XML/` / `全电发票-OFD/`。完整归类表见 [`samples/SAMPLES_INDEX.md`](samples/SAMPLES_INDEX.md)（由 `scripts/sync_samples_layout.py --regen-index` 自动维护）。

## 解析器

| 模块 | 匹配类型 |
| --- | --- |
| `parsers/invoice.py` | 新电子发票（标准双栏、京东、京东大药房、水费、通行费变体） |
| `parsers/old_invoice.py` | 旧版电子发票（含密码区、校验码） |
| `parsers/merged_code.py` | 合并代码 / 成品油（坐标解析人名） |
| `parsers/airline_ticket.py` | 航空运输电子客票行程单 |
| `parsers/train_ticket.py` | 铁路电子客票 |
| `parsers/non_tax_receipt.py` | 中央非税收入统一票据（专利年费等） |
| `parsers/customs_declaration.py` | 海关出口货物报关单（操作联 / 出口退税联；多页合并时只取报关单页） |
| `parsers/xml_invoice.py` | XML 全电发票（`EInvoice` schema） |
| `parsers/ofd_invoice.py` | OFD 全电票 (GB/T 33190，结构化 `CustomTag.xml` + 布局兜底) |

> ⚠️ **H43: XML schema 同步责任**:`parsers/xml_invoice.py` 通过 `defusedxml.ElementTree` 解析 XML 文档,字段名（如 `SellerInformation/SellerName`、`BasicInformation/TotalTax-includedAmount`）与发票平台当前的 ``EInvoice`` schema **硬编码对应**。若国家税务总局 / 发票平台升级 schema（命名空间 URI 变化、字段重命名等）需要手动同步本文件（同时影响 `parsers/ofd_invoice.py`,因为 OFD 内部嵌入的 `<EInvoice>` 复用同一 schema）。建议在升级前先跑一次 XML/OFD 样本回归。
