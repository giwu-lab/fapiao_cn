# fapiao_cn

电子发票字段提取服务（HTTP API）。基于 `pdfplumber`，无 JVM 依赖。

## 特点

- **纯 Python** —— 无 JVM / PDFBox 依赖
- **多页聚合** —— 金额末页优先、购销方首见优先
- **覆盖 6 类解析器**：新电子格式（含京东/京东大药房/水费/通行费变体）、旧版、合并代码/成品油、航空运输行程单、铁路电子客票、中央非税收入统一票据
- **坐标解析** —— 合并代码发票利用 `extract_words()` 坐标定位人名，避免文本行分割歧义
- **字段去空格** —— 输出时自动清除所有字段中的空白字符
- **XML 全电发票** —— 同时支持 PDF 与 XML 输入，输出 schema 一致

## 项目结构

```
fapiao_cn/
├── src/fapiao_cn/                # Python 包（src 布局）
│   ├── cli.py                 # 控制台脚本 fapiao_cn 入口（test/start/stop/restart/...）
│   ├── server.py             # 控制台脚本 fapiao_cn-server 入口 + create_app() 工厂
│   ├── config.py             # Settings（pydantic-settings）
│   ├── schemas.py            # InvoiceRecord, PdfRaw, ExtractResponse
│   ├── pipeline.py           # PDF 文本清洗 + 多页调度 + 聚合
│   ├── formats.py            # 顶层 parse_pdf 入口（XML/PDF 分发）
│   ├── input.py              # HTTP 请求体解析
│   ├── utils.py              # 通用正则 / 工具
│   └── parsers/              # 各票种解析器（dispatch 收纳在 __init__.py）
├── tests/                    # 211 条用例
├── samples/                  # 24 PDF + 6 XML 样本
├── scripts/                  # 运维脚本
│   ├── setup.sh              # 创建 parse_vevn/ + pip install -e .
│   ├── cli.sh                # start/stop/restart/test（开发用）
│   ├── build-portable.sh     # 打 fapiao_cn-portable.tar.gz
│   ├── install_portable.sh   # 离线目标机解压+启动（systemd 或 nohup）
│   ├── fapiao_cn.service.template# systemd unit 模板（被 install_portable.sh 渲染）
│   ├── uninstall_systemd.sh  # 清理 systemd 注册（保留 deployment 目录）
│   └── lint.sh               # ruff 检查
├── build-scripts/            # 发布相关 Python 脚本
│   ├── package_portable.py   # 含 wheel 预下载 + 离线模拟安装 + 打包 + 清理
│   └── verify_portable.py    # 解压并跑 /health 验证
├── parse_vevn/               # in-tree venv（保留，便携部署核心）
└── pyproject.toml            # hatchling 构建
```

## 输出字段

```json
{
  "file": "1.pdf",
  "page_count": 1,
  "invoice_type": "电子发票（增值税专用发票）",
  "invoice_number": "25502000000129241041",
  "issue_date": "2025年11月27日",
  "buyer_name": "...",
  "buyer_tax_id": "...",
  "seller_name": "...",
  "seller_tax_id": "...",
  "amount_without_tax": "124018.66",
  "tax_amount": "16122.43",
  "total_amount": "140141.09",
  "total_amount_upper": "壹拾肆万零壹佰肆拾壹圆零玖分",
  "payee": "",
  "reviewer": "",
  "drawer": "李春梅"
}
```

## 安装（开发 / 在线环境）

```bash
# 方式 1：使用项目自带脚本创建 in-tree venv
bash scripts/setup.sh

# 方式 2：uv 同步（推荐）
uv sync

# 方式 3：手动创建 venv 后 pip install
python3 -m venv parse_vevn
source parse_vevn/bin/activate
pip install -e .
pip install pytest        # dev 依赖（仅测试需要）
```

`pip install -e .` 后会注册两个控制台脚本：

| 命令          | 作用                          |
| ----------- | --------------------------- |
| `fapiao_cn`      | CLI：test/start/stop/restart/dev |
| `fapiao_cn-server` | 启动 uvicorn（前台）              |

## 启动

```bash
# 方式 1：控制台脚本（venv 激活后）
fapiao_cn start               # 后台启动 uvicorn
fapiao_cn stop
fapiao_cn restart
fapiao_cn dev                 # 前台 + auto-reload
fapiao_cn status

# 方式 2：项目自带 shell 封装（自动激活 parse_vevn）
bash scripts/cli.sh start [PORT]
bash scripts/cli.sh stop
bash scripts/cli.sh restart [PORT]
bash scripts/cli.sh test
bash scripts/cli.sh status

# 方式 3：直接 uvicorn（生产 / 调试）
python -m uvicorn fapiao_cn.server:app --host 0.0.0.0 --port 8091

# 方式 4：fapiao_cn-server 控制台脚本
fapiao_cn-server --host 0.0.0.0 --port 8091
```

默认端口 `8091`（可用 `PORT` / `FPAPI_PORT` 环境变量覆盖）。

## API

| 方法   | 路径                    | 说明                                       |
| ---- | --------------------- | ---------------------------------------- |
| GET  | `/health`             | Liveness probe — 进程存活即可            |
| GET  | `/health/full`        | Readiness probe — 依赖检查 + cache 状态       |
| GET  | `/formats`            | 返回支持的全部票种列表                            |
| POST | `/extract`            | 提取发票字段（单文件）                            |
| POST | `/extract/batch`      | 批量提取（多文件，并行解析，按输入顺序返回）                |
| POST | `/verify`             | 国家税务总局查验代理（需 `FC_VERIFY_ENABLED=true`） |
| POST | `/extract-and-verify` | 一站式：提取 + 查验                               |

### 性能与环境变量（v0.2.0）

所有环境变量前缀为 `FC_`（如 `FC_PORT`、`FC_CACHE_SIZE`），可通过 `.env` 文件或 `docker-compose.yml` 注入。

| 变量                          | 默认值     | 说明                                       |
| --------------------------- | ------- | ---------------------------------------- |
| `FC_HOST`                   | `0.0.0.0` | 绑定地址                                     |
| `FC_PORT`                   | `8091`  | 绑定端口                                     |
| `FC_CACHE_SIZE`             | `128`   | LRU 缓存条目数；`0` 关闭                          |
| `FC_BATCH_WORKERS`          | `4`     | `/extract/batch` 并行线程数                    |
| `FC_VERIFY_ENABLED`         | `false` | 是否启用国税查验代理                               |
| `FC_VERIFY_ENDPOINT`        | (空)     | 国税代理 URL（如 `http://internal-verify/...`）   |
| `FC_VERIFY_API_KEY`         | (空)     | Bearer token（可选）                        |
| `FC_VERIFY_TIMEOUT`         | `15`    | 国税代理超时（秒）                                |
| `FC_MAX_FILE_MB`            | `32`    | 单文件最大字节                                  |
| `FC_MAX_BATCH_FILES`        | `50`    | 批量请求最大文件数                                |
| `FC_MAX_BATCH_TOTAL_MB`     | `500`   | 批量请求总大小                                  |
| `FC_MAX_BATCH_SINGLE_MB`    | `10`    | 批量请求中单文件大小                                |

### `/extract` 请求方式

**multipart/form-data**（文件上传）：

```bash
curl -X POST http://127.0.0.1:8091/extract -F "file=@samples/购_材料_01.pdf"
```

**application/json**（文件路径、Base64 或 URL）：

```bash
curl -X POST http://127.0.0.1:8091/extract \
  -H "Content-Type: application/json" \
  -d '{"file_path": "samples/购_材料_01.pdf"}'
curl -X POST http://127.0.0.1:8091/extract \
  -H "Content-Type: application/json" \
  -d '{"file_base64": "...base64..."}'
curl -X POST http://127.0.0.1:8091/extract \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/invoice.pdf"}'
```

**返回原始文本**：

```bash
curl -X POST 'http://127.0.0.1:8091/extract?raw=true' -F "file=@samples/购_材料_01.pdf"
```

### `/extract/batch` 批量提取

同时支持 `multipart/form-data` 和 `application/json`。

**multipart**（字段 `files` 可重复）：

```bash
curl -X POST http://127.0.0.1:8091/extract/batch \
  -F "files=@samples/火车票01.pdf" \
  -F "files=@samples/火车票02.pdf"
```

**JSON**（n8n / 前端友好）：

```bash
curl -X POST http://127.0.0.1:8091/extract/batch \
  -H "Content-Type: application/json" \
  -d '{
    "files": [
      {"filename": "火车票01.pdf", "base64": "..."},
      {"filename": "火车票02.pdf", "base64": "..."}
    ]
  }'
```

返回 `List[ExtractResponse]`，每个元素对应一个文件。单个文件解析失败时，对应项的 `error` 字段会包含错误信息，其余文件不受影响。

**限制**：

- 最多 50 个文件
- 单个文件最大 10 MB
- 总大小最大 500 MB

### 错误响应

| 状态码   | 说明                                       |
| ----- | ---------------------------------------- |
| 400   | 缺少 `file` / `file_path` / `file_base64` / `files` |
| 422   | 内容无法解析（单文件）                             |
| 500   | 内部错误                                     |

## 解析器

| 模块                              | 匹配类型                       |
| ------------------------------- | -------------------------- |
| `src/fapiao_cn/parsers/invoice.py`      | 新电子发票（标准双栏、京东、水费、通行费变体）     |
| `src/fapiao_cn/parsers/old_invoice.py`  | 旧版电子发票（含密码区、校验码）            |
| `src/fapiao_cn/parsers/merged_code.py`  | 合并代码/成品油（坐标解析人名）            |
| `src/fapiao_cn/parsers/airline_ticket.py` | 航空运输电子客票行程单                 |
| `src/fapiao_cn/parsers/train_ticket.py`  | 铁路电子客票                     |
| `src/fapiao_cn/parsers/non_tax_receipt.py` | 中央非税收入统一票据（专利年费等）          |
| `src/fapiao_cn/parsers/xml_invoice.py`   | XML 全电发票                   |

## 测试

```bash
fapiao_cn test                            # 通过控制台脚本
bash scripts/cli.sh test             # 通过 shell 封装
python -m pytest tests/ -v           # 直接 pytest
```

测试覆盖 `samples/` 下 30 个样本（24 PDF + 6 XML），涵盖所有已支持的票种和变体。

## 离线部署（无网络目标机）

源机器先构建自包含的 portable 包（含预下载的离线 wheel + `parse_vevn/`），拷贝到目标机解压即跑。

### 源机器（在线）

```bash
# 1. 创建 in-tree venv，装 fapiao_cn（editable）
bash scripts/setup.sh

# 2. 跑通测试
fapiao_cn test

# 3. 打 portable 包（含 wheel 预下载 + 离线模拟安装 + 打包 + 清理）
bash scripts/build-portable.sh

# 4. （可选）本地验证 portable 包
python build-scripts/verify_portable.py
```

`build-portable.sh` 内部会：

1. 把所有运行时依赖 + fapiao_cn 自身 wheel 下载到 `./wheels/`（临时）
2. 创建一次性 venv，用 `pip install --no-index --find-links=./wheels/ -e .` 模拟离线安装
3. 在该离线 venv 中跑 `/health` smoke test
4. 打 tarball `fapiao_cn-portable.tar.gz`（包含源码 + `samples/` + `parse_vevn/` + `scripts/install_portable.sh`）
5. 删除 `./wheels/` 与模拟 venv（`--keep-wheels` 可保留）

产物：仓库根 `fapiao_cn-portable.tar.gz`（约 40 MiB，含 venv）。

### 目标机（无网络）

**前提**：目标机 **Linux x86_64** + **Python 3.10+**（与源机器一致）。

传输这 3 个文件到目标机同目录：

```
fapiao_cn-portable.tar.gz
scripts/install_portable.sh
scripts/fapiao_cn.service.template   # 仅 systemd 模式需要
scripts/uninstall_systemd.sh     # 仅 systemd 模式需要
```

然后启动安装脚本（**交互式**会问 systemd 还是 nohup）：

```bash
bash scripts/install_portable.sh [DEST_DIR]         # 默认 /opt/fapiao_cn
# 或非交互：
bash scripts/install_portable.sh --systemd [DEST_DIR]
bash scripts/install_portable.sh --nohup   [DEST_DIR]
```

`install_portable.sh` 内部：

1. 解压 tarball 到 `DEST_DIR`
2. 自检 `./parse_vevn/bin/python -c "import fapiao_cn"` —— 若 venv 中无 fapiao_cn 则报错终止
3. 选择启动器（systemd / nohup）：
   - **systemd**：需 root；自动创建 `fapiao_cn` 系统用户、写入 `/etc/systemd/system/fapiao_cn.service`、`enable --now`；日志走 journald
   - **nohup**：后台启动，写 `.uvicorn.pid`、日志到 `logs/server.log`

#### systemd 模式详情

适合长期运行的生产机：开机自启、崩溃自动重启、`Restart=always`、`StandardOutput=journal`、最小权限（`NoNewPrivileges`、`ProtectSystem=strict`、`PrivateTmp`）。

```bash
# 查看状态
systemctl status fapiao_cn
# 看实时日志journalctl -u fapiao_cn -f
# 改端口：编辑 /opt/fapiao_cn/fapiao_cn.env（FC_PORT=18091），systemctl restart fapiao_cn
# 卸载 systemd 注册（保留 deployment 目录）：
sudo bash scripts/uninstall_systemd.sh [/opt/fapiao_cn]
```

#### nohup 模式详情

适合临时测试、容器内、或不想动 systemd 的环境：

```bash
cd /opt/fapiao_cn
# 停止
kill $(cat .uvicorn.pid)
# 改端口
kill $(cat .uvicorn.pid) 2>/dev/null
PORT=18091 nohup ./parse_vevn/bin/python -m uvicorn fapiao_cn.server:app \
  --host 0.0.0.0 --port 18091 > logs/server.log 2>&1 &
echo $! > .uvicorn.pid
```

#### 验证

```bash
curl -fsS http://localhost:8091/health
# 期望：{"status":"ok"}

curl -fsS http://localhost:8091/extract \
  -H "Content-Type: application/json" \
  -d '{"file_path":"/opt/fapiao_cn/samples/购_材料_01.pdf"}'
```

### 仅 Python 包使用（不部署 HTTP 服务）

```bash
pip install fapiao_cn
python -c "
from fapiao_cn import parse_pdf, settings
print('host:', settings.host, 'port:', settings.port)
resp = parse_pdf(open('samples/购_材料_01.pdf', 'rb').read(), filename='购_材料_01.pdf')
print(resp.seller_name, resp.total_amount)
"
```
## Docker 部署

```bash
# 构建 + 启动（后台）
docker compose up -d

# 验证
curl -fsS http://localhost:8091/health
curl -fsS http://localhost:8091/health/full | jq

# 启用 Prometheus 监控（可选 profile）
docker compose --profile monitoring up -d
# 浏览器访问 http://localhost:9090

# 配置国税查验（在 .env 里）
echo "FC_VERIFY_ENABLED=true" >> .env
echo "FC_VERIFY_ENDPOINT=http://your-proxy/verify" >> .env
docker compose restart
```

镜像特性：
- 多阶段构建（`python:3.12-slim`），最终镜像 < 250 MB
- 非 root 运行（`fapiao_cn` 用户）
- 内置 `HEALTHCHECK`（30s 间隔，`/health` 端点）
- `/app/logs` 通过 volume 挂载到宿主机
