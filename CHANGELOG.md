# Changelog

All notable changes to **fapiao_cn** are documented here.  The format
follows [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.3.1] - 2026-10-31

### Removed
- **撤回 1.3.0 引入的电子银行承兑汇票解析功能的「单文件端点」**（企业版）。

  - 移除 `POST /acceptance`（单文件）—— 两版都**不再有**这个路径，请求得到
    404，而不是 403；
  - 错误提示随之改口：误投 `/invoice` 时现在指向 `POST /acceptance/batch`。

  > **批量端点保留**：`POST /acceptance/batch`、`GET /formats/acceptance`、
  > 能力位 `acceptance`、响应契约 `AcceptanceBill` / `AcceptanceEndorsement`、
  > 企业版发行包内的承兑汇票解析器 —— **全部保留，行为不变**。需要解析单张
  > 票据时用批量端点提交一份即可。

  > ⚠️ **1.3.0 已发布过**：GitHub 上的 `v1.3.0` 标签与 Release 资产里仍然带
  > `POST /acceptance` 单文件端点。本版（1.3.1）才是撤下该端点的版本 ——
  > **请用 1.3.1，不要用 1.3.0**。

### Changed
- 插件契约维持 1.3.0 的形态：`FapiaoPlugin.sync_parsers()` 入参是**当前授予的
  能力集**（`frozenset[Feature]`），`register_acceptance_route()` 方法保留 ——
  但它现在只注册批量端点。
- 企业版 `POST /acceptance/batch` 的单项错误提示：用户输入问题（不是承兑票 /
  读不出页面）不再标成 `internal_error`，改成 `error: <原因>`。

### Fixed
- 解析器注册/撤销按能力位逐个判断（`acceptance` 与 `customs` 互相独立），
  未授权的安装不会通过 `/formats` 泄漏电子承兑能力。

## [1.2.0] - 2026-10-30

> 2026-10-28 首发过一版 1.2.0（平台下载版电子发票解析器）。本版在其基础上补入
> **批量端点 URL 入参**与**端点更名 `/invoice`**，重新打包发布。

### Changed
- **端点更名**（BREAKING）：`POST /extract` → **`POST /invoice`**，
  `POST /extract/batch` → **`POST /invoice/batch`**。

  旧路径**已不存在** —— 请求 `/extract` 或 `/extract/batch` 得到 404。
  迁移只要把 URL 里的 `/extract` 一段换成 `/invoice`：**请求体、鉴权 scope
  （`extract`）、响应结构一律不变**。

  路径没动的一批：`/raw`、`/verify`、`/customs/batch`、`/extract-and-verify`。

  能力名 `Feature.EXTRACT` 与鉴权 scope `extract` **不是路径**，刻意保持原样 ——
  它们进了许可证的 `features` 数组与客户自己配的 key scope，改名会让现有许可证
  和 key 集体失效。

  两版的路由表仍然一致：开源版的 `/invoice/batch` 是 403 占位路由，企业版是真实
  实现，路径完全相同（见 `fapiao_cn.server._register_batch_unavailable` 与
  `fapiao_cn_ee.batch.register_batch_route`）。

- 文本规整逻辑（康熙部首还原、3+ 连续重复 CJK 折叠）从 `pipeline._clean_text`
  下沉到 `utils.normalize_invoice_text` —— 两条链路共用同一套规整，同时避开
  `pipeline` ↔ `parsers` 的循环导入。行为不变。

### Added
- **批量端点支持 URL 入参**：`/invoice/batch` 与 `/customs/batch` 的每个文件现在
  都能用**远端 URL** 取，与单文件端点 `/invoice` 对齐。

  - JSON：`{"files": [{"filename": ..., "url": "https://..."}]}`；`files[]` 里
    每项还可以用 `file_path` 或 `base64`（三种来源互斥，优先级
    `url` → `file_path` → `base64`，与单文件端点一致）。
  - multipart：重复的 `urls`（或 `url`）字段给远端地址；`files` 字段直接写
    http(s) 地址也认（`curl -F "files=https://..."`）。
  - 顶层 `{"url": ...}` / `{"file_path": ...}` / `{"file_base64": ...}` 等价于
    一份文件的批量，键名与 `/invoice` 一致。

  URL 抓取走的是和 `/invoice` **同一套防护**：云元数据地址
  （`169.254.169.254` / `fd00:ec2::254`）一律拒绝、响应体按 `FC_MAX_FILE_MB`
  截断、30 秒超时、只允许 http/https。批量里的多个 URL **并发抓取**
  （并发上限 8，避免 50 个大文件同时驻留内存），但报错仍按输入顺序确定性地
  指出是第几项出的问题。

  文件数闸门（`FC_MAX_BATCH_FILES`）提到了抓取**之前** —— 超限时一个 URL 都
  不会去下载。

- **平台下载版电子发票解析器**（`parsers/platform_invoice.py`）：支持票面
  **标签块在上、值块在下**的平台导出 PDF。

### Fixed
- **批量 JSON 顶层的 `url` 不再被静默忽略**：过去 `{"files": [...], "url": "..."}`
  返回 200，但顶层那份文件**根本没进来** —— 调用方会以为已经处理过了。现在顶层
  与 `files` 数组同时给会明确返回 400（二选一）。

- **批量 multipart 里 `files` 字段给非 URL 文本不再被静默丢弃**：过去只会看到
  一句含糊的 "No files provided"，现在明确告诉调用方该用 `urls` / `file_paths`。

- **非法 URL 不再把请求打成 500**：坏主机名、坏 IPv6 字面量、IDNA 失败这类异常
  现在按「抓取失败」处理（单文件端点同样受益）。

- **从 URL 派生的文件名会做百分号解码**：中文名不再返回
  `%E7%81%AB%E8%BD%A6...` 这种编码串；URL 里的 fragment 也不再混进文件名。

- **平台下载版电子发票不再丢关键字段**：部分发票平台（下载页导出的
  PDF）把票面拆成两段写进 PDF 内容流 —— 第一段是**标签块**
  （`发票号码：` / `开票日期：` / `名称：` / 统一社会信用代码…），第二段才是
  **值块**（号码 / 日期 / 名称 / 税号 / 明细 / 合计 / 大写金额）。两段在
  票面上其实**同行相邻**，但 PDF 文本层的顺序就是内容流顺序，于是
  `发票号码：` 后面是空的，号码却排在文本末尾。

  依赖「标签: 值」同行的标准解析器因此把这类票的**发票号码、开票日期、
  不含税金额、税额、价税合计、大写金额全部丢掉**，`drawer` 还会误抓成
  平台盖的「下载次数」。

  新增 `parsers/platform_invoice.py`：按**词坐标**重建阅读顺序（词按
  `(top, x0)` 排序、按行距聚成行，逐行拼回文本），再交给 `parsers/invoice.py`
  里已验证的取值函数；同时剔除「购买方信息 / 销售方信息」的**竖排表头字**
  —— 它们与买方 / 卖方名称同行，混进文本会把卖方名称截成「售」。

  版式识别用**结构特征**而不是关键字：`发票号码 / 发票代码 / 开票日期 /
  开票人` 里至少有两个以「标签 + 冒号」独占一行（冒号后面没有值）。普通
  票面这些标签都带值同行，因此不会误判。若重建结果反而比原始文本少命中
  字段，会自动退回原始文本 —— 永远不会比原来更差。

  同一模板的既有样本（`个人代开_01` / `购_自来水_01`，带「统一发票监制章」
  因而号码日期还能被兜底正则捞到）现在也统一走这条路径，取值不变。

- **版式识别不再依赖调用方先清洗文本**：平台下载版的「电子发票」标题在 PDF
  里写的是康熙部首 ``⼦``。检测器此前直接判断 `"电子发票" in text`，调用方
  传原始文本时（如 `scripts/classify_samples.py`）会漏判。现在检测器自己
  先做一遍文本归一化。

- **补平台下载版的回归测试**：新增 `tests/test_platform_invoice.py`（17 个
  用例，**不依赖 `samples/`**，版面几何用合成词坐标描述，公开仓库也能跑），
  覆盖版式识别、阅读顺序重建、竖排表头剔除、竖排水印排除、康熙部首还原，
  以及「重建结果更差时退回原始文本」这条安全网。


## [1.1.0] - 2026-10-25

### Changed
- **报关单端点改为批量**（BREAKING）：`POST /extract/customs`（单文件、返回对象）
  改为 **`POST /customs/batch`**（多文件、返回数组）。

  - 入参与 `/extract/batch` 对齐：multipart 里**重复的 `files`** 字段，或 JSON
    `{"files": [{"filename": ..., "base64": ...}]}`。
  - 限额复用同一套开关：最多 50 个（`FC_MAX_BATCH_FILES`）、单文件 10 MB
    （`FC_MAX_FILE_MB`）、总 500 MB（`FC_MAX_BATCH_TOTAL_MB`）；并发
    `FC_BATCH_WORKERS`（默认 4），与 `/extract/batch` **共用同一个线程池**。
  - 返回 `CustomsDeclarationHeader` 数组，顺序与上传顺序一致；单个文件解析失败
    落进该项的 `message`，不影响整批。
  - **迁移**：URL 换成 `/customs/batch`，字段名 `file` → `files`，响应从对象
    改成数组（取 `[0]`）。

- **报关单误发不再静默吞字段**（FIX）：报关单 PDF 发到 `/extract` 或
  `/extract/batch` 时，过去会返回一堆空字段而 `message` 仍是 `success`
  —— 调用方察觉不到数据已经丢了。根因是发票管线 `_aggregate()` 用
  `getattr(record, field, "")` 把报关单记录压成 `InvoiceRecord`，报关单没有的
  字段全部静默变成空串。

  现在企业版的报关单解析器是**守门包装**：记录类型不是 `InvoiceRecord` 就抛
  `ValueError`，`/extract` 因此返回 **422**，`message` 里写明「请走
  `/customs/batch`」；`/extract/batch` 则落进对应项的 `message`。

  这条修复完全在企业版包内完成 —— 开源版的 `pipeline` / `formats` 一行未改。

### Added
- `fapiao_cn_ee/pool.py`：`/extract/batch` 与 `/customs/batch` 共用的线程池
  （此前两个批量端点各开各的，同时被调用时线程数会翻倍）。
- 企业版仓库补上 ruff / mypy 配置 —— 此前缺失，`ruff check` 跑的是默认规则
  而不是与开源版一致的 `E,F,I`。

### Fixed
- 开源版 `ruff check src tests` 的一处既有 E501（报关单样本的中文票面原文，
  折行会破坏解析器依赖的版面结构）—— 改为 per-file-ignores。

- **报关单明细字段归位**（FIX，企业版包内）：此前报关单的**商品明细**只有
  `item_no` / `hs_code` / `total_price` 几个字段是对的，其余字段要么空着、
  要么整行都塞进 `name_and_spec` —— 包括品名后面的 `第二数量/第二单位`
  这类表内标签，甚至把表格尾部的签字、审单、打印日期一起吞进品名。

  根因是明细表解析只用了一套「按 token 位置猜」的规则，但**两种版式的
  行结构完全不同**：

  - **出口退税联** 在行内带明确表内标签（`法定数量/法定单位`、
    `第二数量/第二单位`、`申报数量/申报单位`），而旧实现只在**续行**里
    找标签 —— 标签实际在**表头行**，于是法定数量永远解析不出来；
  - **操作联** 压根没有表内标签，只能按列的位置还原；旧实现把整行连
    标签一起当成品名；
  - 续行的正则会一路吃到行尾，把 `第二数量/第二单位 325.5千克 (502) 美元`
    整段吞进 `name_and_spec`；
  - 最后一条明细还会把表尾（签字 / 审单 / `Page 1 of 1 打印日期`）一并吞掉。

  现在按版式分派解析：**出口退税联**按表内标签切分，**操作联**以境内货源地
  `(50069)沙坪坝区` 为锚点从右往左定位各列（某一列为空也不会整体错位）；
  「数量及单位」格里可能有 2~3 个值（`880千克` / `220000个`），用
  「单价 × 数量 = 总价」认出哪个才是成交数量。

  修复后每条明细能正确给出：品名与规格型号、法定数量、第二数量、
  申报/成交数量、单价、总价、币制代码与名称、原产国、最终目的国、
  境内货源地、征免。规格型号在**折行处直接接续**（票面自带的 `|` 是数据、
  折行符不是），并剥掉 `0|2|` 这类件号前缀。

- **整数总价不再漏读**（FIX，企业版包内）：金额正则原先要求必须带小数点，
  于是**整数总价**（`1686`）既没被当成总价，又被错当成单价 —— 6 页退税联
  样本有 3 条是整数总价，整单金额合计因此算错（`49195.76` → 正确的
  `57438.00`）。金额合计已按票面逐行核对。

- **币制拆成代码与名称**（FIX，企业版包内）：`currency_code` 原先恒为空
  （注释说「代码在规格行上」，其实那里是目的国代码），`currency_name` 里
  塞的是代码。现在代码进 `currency_code`（`USD`）、中文名进 `currency_name`
  （`美元`）；表头 `currency` 改为优先取代码。操作联此前还会把**征免**
  误当成币制（`照章征税`），现在正确显示 `美元`。

- **补明细字段的回归测试**：此前报关单明细字段是**零覆盖** —— 只断言了
  `hs_code` 和 `total_price`，正是这组缺失让上面三个问题溜了过去。
  现在新增 10 个用例：按版式断言每一列的取值、品名不含表内标签与表尾、
  以及一条通用不变量「**成交数量 × 单价 = 总价**」。

## [1.0.0] - 2026-10-25

首次**对外正式发布**。本节汇总此前内部迭代的全部内容 —— 对外只承认这一次
发布，内部版本号不作为独立发布暴露。

项目始于 2026-09-08 的内部版本（6 种票种 + XML + systemd + 便携 tarball，
211 条测试通过），此前从未公开发布。

### Added

- **发行版拆分（CE / EE）与许可证体系**: 报关单解析与批量提取下沉为**独立的
  发行包** `fapiao_cn_ee`（企业版），开源版 `fapiao_cn` 不再包含这部分实现。

  - 报关单解析器（`parsers/customs_declaration.py`，950 行）已移出开源版，
    开源版的 dispatch 注册表里没有报关单规则 —— `/formats` 不再列出它，
    `/extract` 也不会把报关单 PDF 派给它。
  - 新增 `src/fapiao_cn/editions.py`：`Edition` / `Feature` / `Entitlements`
    三层能力模型，以及 `require_feature()` 依赖。
  - 新增 `src/fapiao_cn/plugins.py`：CE ↔ EE 的插件契约（`FapiaoPlugin`），
    EE 包通过 `importlib.util.find_spec("fapiao_cn_ee")` 被发现。
  - 新增 `GET /capabilities`（公开）：报告当前发行版与可用能力，前端据此
    显示/隐藏企业版入口。
  - 企业版能力由**许可证**授权（Ed25519 签名）：缺失 / 过期 / 签名不合法
    时**自动降级成开源版行为**（端点返回 403，服务本身照常工作），
    `SIGHUP` 可热重载 —— 续期不用重启容器。
  - **刻意没有 `FC_EDITION` 之类的版本开关**：开源版用户改一个环境变量就能
    解锁企业版的话，整套商业边界就作废了。版本只由「装了哪个发行包 +
    许可证是否有效」决定。

  商业边界落在**发行包**上：CE 的 wheel 与镜像里没有 `fapiao_cn_ee` 的
  任何字节码（Dockerfile 按 `ARG EDITION` 剔除企业版 wheel）。

- **企业版仓库独立为私有库**: 企业版实现（`fapiao_cn_ee` 包、签发工具
  `scripts/sign_license.py`、构建脚本、授权手册 `AUTHORIZATION.md`）已从
  开源仓库拆出，落在私有仓库 `fapiao_cn_ee`；本仓库只保留 CE 代码与两边
  对接用的插件契约。签发私钥始终在仓库外保管，不入库。

- **`/extract/customs` 与 `/extract/batch` 的门禁**: 未授权时返回结构化 403
  （`error=feature_not_available`），而不是 404 —— 两个版本的路由表保持
  一致，客户端不会因为版本差异崩溃。鉴权优先于版本判定：无 key 的请求先
  得到 401，不会先撞到 403（否则等于向未鉴权方泄漏「这是企业版功能」）。

- **消费类别分类（`expense_category`）**: `/extract` 响应新增三个字段
  —— `expense_category`（枚举值）、`expense_category_label`（中文显示）、
  `expense_category_source`（命中层级）。把发票归到交通 / 住宿 / 餐饮 / 快递 /
  通信 / 水费 / 电费 / 燃气 / 成品油 / 其他 10 个类目，便于按类目做预算统计、
  凭证归档与报销路由。

  命中层级严格优先级：**L1 票种 → L2 卖方关键词 → L3 商品明细 → L4 19 位税收
  分类编码前缀 → 兜底**。同层冲突按规则 `priority` 数字从大到小取第一。

  - `other`（其他）与 `unclassified`（未归类）**语义独立**：前者是「票种级显式
    归类」（中央非税收入统一票据），后者是「没有规则命中」。
  - 报关单（`/extract/customs`）不属于报销场景，分类逻辑显式跳过，三字段恒为空。
  - 总开关 `FC_EXPENSE_CATEGORY_ENABLED`（默认 `true`）；关闭时三字段恒为空字符串，
    且不消耗额外计算。

  新增 `src/fapiao_cn/expense_category.py`（规则表）与 `tests/test_classify.py`、
  `tests/test_extract_endpoint.py`（端到端字段贯穿断言）。

- **商品明细 `items[]`**: `/extract` 响应新增 `items` 数组，仅 **XML 全电发票**填充；
  PDF / OFD 恒为 `[]`。每个 `<IssuItemInformation>` 一行，含 `name` /
  `category`（从 `*类别*` 前缀拆分）/ `spec` / `unit` / `quantity` / `unit_price` /
  `amount` / `tax_rate` / `tax_amount` / `total_amount` /
  `tax_classification_code`（19 位税收分类编码，退税申报必填）/ `is_reversal`
  （红字冲销行标记）。金额一律保持字符串原值，负数与带单位数量不被破坏。
  `item_no` 为解析器生成的 1..N 序号（XML 不带行号）。

- **明细自洽校验**: 解析后比对「明细金额和 / 税额和 / 价税合计和」与表头，
  不一致时写 warning 日志。这是发现**明细被静默截断**的唯一手段——缺失的行
  不会报错，每一行返回出来都看起来完全正常。

- **Customs declaration**: 海关出口货物报关单专用解析器（操作联 / 出口退税联）。
  新增 `CustomsDeclarationHeader` / `CustomsDeclarationItem` schema；端点
  `POST /extract/customs` 与 `GET /formats/customs`；多页续打（如 6 页退税联
  24 个商品）通过 `ctx["full_text"]` 跨页聚合；合并文档（报关单 + 放行条）
  副页守卫自动忽略放行条页。Covered by `tests/test_parsers.py::TestCustomsDeclaration`、
  `tests/test_pipeline.py::TestKnownCustomsDeclarations` 与
  `tests/test_api.py::TestExtractCustoms` 共 33 个用例。

- **Docker 完全离线构建**: Dockerfile 改为全程 `pip install --no-index
  --find-links=/wheels`，构建依赖（hatchling）与运行时依赖全部由主机侧
  `scripts/prepare_wheels.sh` 预下载到 `./wheels/`。绕开 BuildKit sandbox
  在受限网络环境下的 DNS 隔离问题（`Temporary failure in name resolution`）。
  `.dockerignore` 不再忽略 `wheels/`。

- **`scripts/start-win.ps1`**: Windows 专用启停脚本（`start` / `-Stop` / `-Status`）。
  绕开 `fapiao_cn` CLI 在 Windows 上的两个缺陷——`cli.py::_alive()` 用
  `os.kill(pid, 0)` 探活会抛 `OSError: [Errno 22]`（Windows 无信号 0），导致
  `status` 误报 stale、`stop` 只删 PID 文件却不杀进程；以及 venv 的
  `Scripts\python.exe` 会再 fork 出真正持有 socket 的子进程，
  `Start-Process -PassThru` 拿到的是 wrapper PID。脚本改用 TCP 监听表判断存活
  与反查真实监听 PID，启动前预检解释器能否 import 依赖，端口被非本项目进程
  占用时拒绝启动与终止。

- **`POST /raw`** — 专用端点，只返回 PDF 原始文本（`text` / `lines` /
  `page_count`）。XML 输入以 `400 raw_unsupported: pdf_only` 拒绝。用于在
  解析流水线之外单独调试 PDF。
- **`PdfRaw.message` 字段**（默认 `"success"`），`/raw` 端点因此能像 `/extract`
  一样用 `message` 字段承载 422 错误。

- **API 鉴权** — 受保护端点（除 `/health`）需要 API Key
  - Bearer Token / `X-API-Key` 双形式，按 `key_id` 维度限速
  - Simple 模式：`FC_API_KEYS="k1,k2,k3"` 逗号分隔，全作用域
  - JSON 模式：`FC_API_KEYS_FILE=...` 支持 `label` / `scopes` / `rpm` /
    `burst` 精细配置
  - 作用域：`extract` / `verify` / `admin`（admin 为超集）
  - 错误响应符合 RFC 6750：`WWW-Authenticate: Bearer realm="fapiao_cn", error="..."`
  - 审计日志 `logs/audit.log`（10 MiB × 5 自动轮转）
  - Dev 兜底：未配 key 时启动 WARN 仍放行（迁移友好）
  - 双轨兼容开关 `FC_API_LEGACY_ALLOW`（仅迁移期使用）
  - 25 条专项测试 (`tests/test_auth.py`)

- **Docker** (`Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`,
  `deploy/prometheus.yml`)：多阶段构建，非 root 运行，HEALTHCHECK 内置，slim 镜像。
- **`/health/full` readiness probe** (E3)：报告 pdfplumber / disk / parsers /
  uptime / cache 状态；失败时返回 503。
- **`/verify` 国家税务总局查验代理** (C3)：通过 `FC_VERIFY_ENABLED` /
  `FC_VERIFY_ENDPOINT` / `FC_VERIFY_API_KEY` 开关与配置。
- **`/extract-and-verify` 一站式端点**：提取 + 查验（默认 disable 查验时退化为
  普通提取）。
- **LRU 解析结果缓存** (D2) `src/fapiao_cn/cache.py`：按 `sha256(contents) +
  filename + with_raw` key，配置 `FC_CACHE_SIZE`（默认 128，0 关闭）。
- **Batch 并行解析** (D1)：`/extract/batch` 改为 `ThreadPoolExecutor` 并行，配置
  `FC_BATCH_WORKERS`（默认 4），保持输入顺序。
- **惰性 `extract_words()`** (D3)：`PageView.words()` 首次访问时提取，后续复用
  缓存；普通发票解析器不再付 layout 代价。
- **GitHub Actions CI** (G3) `.github/workflows/ci.yml`：多 Python 版本
  （3.10/3.11/3.12）跑 ruff + pytest + mypy advisory + Docker smoke。
- **mypy 类型检查** (G4)：`pyproject.toml` 接入 `strict_optional` /
  `warn_unused_ignores` 等规则。

### Renamed

- Project: `fpapi` → `fapiao_cn` (包名、控制台脚本、systemd unit、Docker image、
  env prefix 全部改名)。
  - 包导入: `import fpapi` → `import fapiao_cn`
  - CLI scripts: `fpapi` / `fpapi-server` → `fapiao_cn` / `fapiao_cn-server`
  - systemd service: `fapiao_cn.service`（unit 模板由 `scripts/install_portable.sh`
    渲染）
  - Docker image: `fapiao_cn:1.0.0`
  - 环境变量前缀: `PDF_` → `FC_`（所有 `FC_HOST` / `FC_PORT` / `FC_CACHE_SIZE` 等）
  - PID 文件: `.uvicorn.pid` → `.fapiao_cn.pid`
  - 默认安装路径: `/opt/fpapi` → `/opt/fapiao_cn`

### Changed

- **`fapiao_cn dev` 改为后台模式**: 原先是前台阻塞运行（`uvicorn.run`），
  现在与 `fapiao_cn start` 一致——后台启动 + PID 文件 + 端口占用预检 +
  `start_new_session` 脱离调用方 shell，日志写入 `logs/server-dev.log`。
  `stop` / `restart` / `status` 对 `start` 与 `dev` 两种模式行为一致。

- **`fapiao_cn stop` 改为同步终止**: 先向整个进程组发 `SIGTERM`（覆盖 uvicorn
  的 reloader 父进程 + worker 子进程），轮询等待进程真正退出（超时升级
  `SIGKILL`），再等待监听端口释放，最后才删除 PID 文件。之前 `stop` 立即返回，
  紧接着的 `start` 会撞上尚未释放的端口（`EADDRINUSE`）。
  `_alive()` 额外读取 `/proc/PID/status` 识别僵尸进程，避免把已死未回收的
  进程判为「存活」。

- **单文件上限统一为 `FC_MAX_FILE_MB`，默认 10 MB**（BREAKING 配置项）:
  `/extract/batch` 原先有独立的 `FC_MAX_BATCH_SINGLE_MB`（默认 10 MB），
  而 `/extract` 用 `FC_MAX_FILE_MB`（默认 32 MB），导致同一份 10–32 MB 的文件
  走单文件入口能过、走批量入口却被整批 400。现在两个端点共用
  `FC_MAX_FILE_MB`，**默认值同时下调为 10 MB**。

  逐端点的净影响：

  | 端点 | 变更前 | 变更后 |
  | --- | --- | --- |
  | `/extract` | 32 MB | 10 MB（**收紧**） |
  | `/extract/batch` | 10 MB | 10 MB（不变，仅换用共用开关） |

  **迁移**: 之前能接收 10–32 MB 文件的调用方需要显式调大
  `FC_MAX_FILE_MB`（`FC_MAX_FILE_MB=32` 可恢复原行为）；仍在环境变量里残留的
  `FC_MAX_BATCH_SINGLE_MB` 会被 `extra="ignore"` 静默忽略、再无任何作用，
  请从 `docker-compose.yml` / `.env` 中移除。

  状态码语义保持不变：批量入口超限仍在闸门处整批 400，单文件端点由
  `parse_pdf` 兜底返回 422（`message=error: file_too_large`）。

- `samples_dir_exists` 不再参与就绪判定：`samples/` 是 `.gitignore` 掉的
  开发/CI 素材，镜像不 COPY、容器内本就不存在，按它判定会让生产探针必然 503。
- 新增环境变量 `FC_APP_ROOT`（默认空，本地一般无需设置）。

- PID file moved from `.fapiao_cn.pid` (project root) to `logs/fapiao_cn.pid` —
  keeps all runtime artefacts under the logs directory. `.gitignore`,
  `.dockerignore`, `scripts/install_portable.sh`, and
  `build-scripts/package_portable.py` updated accordingly.
- `/extract`, `/extract-and-verify`, `/raw` HTTP 422 responses now return the full
  `ExtractResponse` / `PdfRaw` body with the complete error string in `message`
  (including the `error: ` prefix). Previously the error string was stripped of
  the prefix and stuffed into FastAPI's `{"detail": ...}` envelope, which
  contradicted the documented client contract. 400 / 500 / 502 / 503 behaviour
  unchanged — those still use `{"detail": ...}` since they correspond to
  request-structure errors or proxy errors, not parse errors.
- LRU cache key simplified to `sha256(contents)[:32] : filename` (no more
  `with_raw` dimension). `ParseResultCache.get` / `put` signatures updated.
- `formats.detect_format` is now public (renamed from `_detect_format`) so the
  `/raw` endpoint can reuse the format-sniffing logic.

- `server.py` 重写为包含 `/health/full`、`/verify`、`/extract-and-verify`。
- `pipeline.py` 引入 `PageView` 类（lazy words），API 内部传递 `ctx["page"]`。
- `formats.py` 接入 LRU 缓存（只缓存成功响应，错误不缓存）。
- `config.py` env prefix 改为 `FC_`，新增 `cache_size` / `batch_workers` /
  `verify_*` 字段。
- `cli.py` PID 文件名改 `.fapiao_cn.pid`，prog 改 `fapiao_cn`。
- `setup.sh` / `cli.sh` / `install_portable.sh` / `uninstall_systemd.sh` /
  `lint.sh` / `fapiao_cn.service.template` 全部以新名称渲染。
- `pyproject.toml` name 改为 `fapiao_cn`（本发布版本为 `1.0.0`）。
- `scripts/lint.sh` 加入 ruff + mypy + pytest 三件套。

### Fixed

- **Linux 上容器启动即崩溃**: `PermissionError: [Errno 13] Permission denied:
  '/app/logs/audit.log'` → `ValueError: Unable to configure handler
  'audit_file'`。

  `deploy/log_config.yaml` 的 `audit_file` 是 `RotatingFileHandler`，它在
  **构造时**（`delay` 默认 False）就 `open()` 文件；uvicorn 在
  `Config.__init__` → `configure_logging()` → `dictConfig()` 阶段创建
  handler，此时打不开文件就抛 `PermissionError` 并被包装成 `ValueError`
  —— 容器在启动阶段直接退出，compose 表现为无限重启。

  根因是属主：宿主把 `./logs` 挂到 `/app/logs` 时，那个目录在 Linux 上
  通常是 `root:root 0755`（compose 用 root 创建），而服务以非 root 用户
  `fapiao_cn`（uid 999）运行。旧版 entrypoint 里的 `chmod 777 /app/logs`
  救不了：容器以非 root 运行，对 root 拥有的目录 `chmod` 直接 `EPERM`
  被 `|| true` 吞掉；就算目录 chmod 成功，**已存在的 root:root 0644 的
  `audit.log` 本身**依然会被 `open(..., 'a')` 拒绝。

  现在 entrypoint 以 root 进入：先把日志目录**和里面已存在的日志文件**
  一起 `chown` 给应用用户，再以 `setpriv --reuid --regid --init-groups`
  降权回 `fapiao_cn` 启动 uvicorn（`exec` 让它仍是 PID 1，`docker stop`
  的 SIGTERM 语义不变）。降权手段不可用时（既无 `setpriv` 也无 `su`）
  明确失败退出，**绝不静默地以 root 跑服务**。

  另外：写不了日志目录时不再抛一句难懂的 `PermissionError`，而是先做
  预检，给出「`sudo chown -R 999:999 logs`」这样可直接执行的提示。

- **全新克隆 / CI 的 `docker build` 必然失败**: Dockerfile 改成完全离线构建后
  第 11 行 `COPY wheels /wheels` 依赖一个**被 .gitignore 排除**的目录，
  仓库里并不存在它 —— 干净检出（含 `.github/workflows/ci.yml` 的
  `docker build -t fapiao_cn:ci .`）一律在第 3 步报
  `failed to calculate checksum ... "/wheels": not found`。
  `docker compose up -d`（compose 里 `build: {context: .}`）同样受影响。

  现在 Dockerfile 会自动判断：`wheels/` 里有 `.whl` 就走离线
  （`--no-index --find-links=/wheels`，全程不联网），否则回退到构建期从
  镜像源在线下载。两条路产出同样的镜像，**都不需要改 Dockerfile**。
  模式在第一个 RUN 里判定一次并记进 `/etc/pip-mode` —— 后续步骤不能重新
  判断「`wheels/` 里有没有 `.whl`」，因为在线分支会把依赖下载进
  `wheels/`，之后这个条件就不再代表原始模式了。

  配套：`wheels/.gitkeep` 入库（`.gitignore` 从 `wheels/` 改为
  `wheels/*` + `!wheels/.gitkeep`），保证全新克隆里 `COPY wheels /wheels`
  依然成立；`.dockerignore` 里那条「Dockerfile 不再引用 wheels/」的错误
  注释已更正。

- **OFD 全电票从不被分类**: 消费类别分类此前只接在两条路径上
  （`pipeline.run` 的 PDF、`parsers/xml_invoice.py` 的 XML），
  `parsers/ofd_invoice.py` 从不调用 `with_expense_category()`，
  而 `formats._parse_ofd` 却照抄这三个字段 —— 于是 OFD 发票的
  `expense_category` 恒为空串，而空串与「报关单跳过」「开关关闭」
  这两种语义无法区分。现在 OFD 与 PDF / XML 行为一致。

- **`fapiao_cn stop` 等的不是真正占用的端口**: `start` / `dev` 可以用
  `--host` / `--port` 绑到与 `FC_PORT` / `settings.port` 不同的端口，
  但 `stop` 只能从环境变量猜端口，于是「等端口释放」这件事发生在错误的
  socket 上 —— `start --port 9000` 之后的 EADDRINUSE 竞态原样存在。

  现在 `start` / `dev` 把实际绑定的 `host:port` 落到
  `logs/fapiao_cn.endpoint`，`stop` / `restart` 优先读它；
  `stop` 也接受 `--host` / `--port` 覆盖。`restart` 会在**原端口**上重启。

- **`stop` 可能误杀无关进程组**: PID 文件是纯文本，可能比它指向的进程
  活得久；PID 被系统回收给别的程序后，`os.killpg` 会连带干掉一个毫不
  相干的进程组（改动前只是杀单个进程）。现在发信号前先读
  `/proc/<pid>/cmdline` 确认它确实是 uvicorn，否则拒绝发信号并返回 1。
  没有 `/proc` 的平台（Windows / macOS）保持原行为。

- **规则表 tie-break 与文档不符**: 跨类目比较时用的是**类目内**下标，
  而文档承诺「priority 相同时按规则在表中顺序取第一」（表中顺序是所有
  类目拼接出来的全局顺序）。当前规则表下两者结果一致，但只要有一个类目
  新增第二条同优先级规则，选中的规则就会偏离文档。改用全局序号。

- **`.env.example` 与代码脱节**: `FC_MAX_FILE_MB` 仍写着 32（新默认值是
  10，32 恰好是「恢复旧行为」的值）；`FC_MAX_BATCH_SINGLE_MB` 仍写着 10
  （该配置项已删除，残留值会被 `extra="ignore"` 静默忽略）；新增的
  `FC_EXPENSE_CATEGORY_ENABLED` 则完全没写。已同步。

- **`fapiao_cn dev --help` 仍写「foreground」**: `cmd_dev` 早已改成后台
  模式，docstring 和 README 都更新过，只有参数帮助文案漏了。

- **`deploy/log_config.yaml` 必须保持纯 ASCII**: uvicorn 用 `yaml.safe_load()` 读它，
  而文件是按**系统 locale 编码**打开的 —— 中文 Windows（GBK）上任何非 ASCII
  字节都会让配置加载失败，服务在起得来之前就死于
  `UnicodeDecodeError: 'gbk' codec can't decode byte 0x84`。
  本仓库的 `log_config.yaml` 之前只有英文注释；新增 `fapiao_cn` logger 时若
  顺手写成中文，会让 `fapiao_cn start` / `dev` 在 Windows 上完全起不来。
  文件里已加注说明，保持 ASCII。

- **容器日志丢失 `fapiao_cn.*` 的 INFO 消息**: `deploy/log_config.yaml` 只配了
  `uvicorn.error` / `uvicorn.access` / `fapiao_cn.audit` 三个 logger，
  `fapiao_cn.server` / `fapiao_cn.auth` 的 INFO 记录向上 propagate 到 root，
  而 root 没有任何 handler —— Python 的 lastResort 只处理 WARNING 及以上，
  于是所有 INFO 级别诊断被**静默丢弃**。

  实际影响：运维在容器日志里看不到「loaded N API keys from ...（structured mode）」
  这类启动确认，也看不到 SIGHUP 热加载后的「reloaded N API keys」，
  无法判断密钥库到底有没有加载成功。

  现在补上 `fapiao_cn` logger（level INFO → default handler → stderr）。
  `fapiao_cn.audit` 自带 handler 且 `propagate: false`，不会被重复输出到 stdout。

- **CLI 生命周期命令在 Windows 上崩溃**: `cmd_stop` / `cmd_restart` 新增的
  `_wait_pid_gone()` 直接调用 `os.killpg()` 与 `signal.SIGKILL`——两者都是
  POSIX 专有，在 Windows 上分别抛 `AttributeError`（`os` 没有 `killpg` 属性）
  和 `AttributeError`（`signal` 没有 `SIGKILL`），导致 `fapiao_cn stop`
  直接带栈退出、反而停不掉服务（改动前用的 `os.kill(pid, SIGTERM)` 在
  Windows 上是能正常终止进程的）。

  现在新增 `_signal_process()` 统一封装：优先用进程组信号，没有进程组的
  平台降级为单进程 `os.kill()`（Windows 上映射为 `TerminateProcess`）；
  `SIGKILL` 常量改用 `getattr(signal, "SIGKILL", signal.SIGTERM)` 兜底。
  POSIX 行为保持不变。

  依赖进程组语义的测试用例（`os.killpg` / 子进程 `SIGTERM` handler）
  在 Windows 上标记为跳过，Windows 的进程管理仍走 `scripts/start-win.ps1`。

- **`scripts/start-win.ps1` 启动后卡住不退出**: 原写法用
  `Start-Process -RedirectStandardOutput/Error` 启动 uvicorn。PowerShell 的
  `-WindowStyle Hidden` 是 `SW_HIDE` + **继承控制台**（不是 `CREATE_NO_WINDOW`），
  叠加 `CreateProcess` 的 `bInheritHandles=TRUE`，导致 uvicorn 一直持有调用方
  shell 的 stdout 管道写端。调用方 shell 早已退出却等不到 EOF，表现为「启动成功
  但命令卡到被超时掐断」，且日志里没有任何错误。
  改由 `cmd.exe` 承担 `>` / `2>` 重定向、Python 进程从 cmd 继承句柄，继承链与
  调用方彻底断开。日志输出不受影响。

- **`-Stop` 写 PID 文件会抛异常**: 运行中的 uvicorn 持有 `server.err.log` 的
  文件句柄时 `Set-Content` 写入被拒，导致整个 `-Stop` 失败。改用
  `-ErrorAction SilentlyContinue`。同时说明：日志文件名固定为
  `logs/server.{out,err}.log`，**同名旧实例存活时新实例无法启动**（cmd 打不开
  被占用的日志文件），需先 `-Stop`。

- **Windows 启动崩溃**: `server.py` 的 lifespan 无条件使用 `signal.SIGHUP`，
  而 Windows 没有该信号，`AttributeError` 在进入 `add_signal_handler` 之前抛出，
  原有的 `except (NotImplementedError, RuntimeError)` 接不住，导致 uvicorn 启动期
  直接退出。改为先探测信号是否存在（`getattr(signal, "SIGHUP", None)`）；
  POSIX 行为逐字不变。

- **H22 app root 少上一层**: `/health/full` 用
  `Path(__file__).resolve().parent.parent` 定位基准，实际落在 `src/` 而非仓库根，
  导致 `samples_dir_exists` 恒为 `False`、磁盘探针每次在 `src/logs/` 凭空建目录，
  就绪探针因此永远 503。新增 `_resolve_app_root()`：优先 `FC_APP_ROOT`（镜像内置
  `/app`），否则按源码布局 `parents[2]` 推导，保持 H22 要求的 cwd 无关。

- **H18 告警位语义反转**: `cache_low_hit_ratio` 是告警位（True = 触发告警 =
  不健康），而 `pdfplumber` / `disk_writable` 是通过型布尔（True = 正常）。
  原实现把两者混进同一个 `all()`，语义互相抵消，低命中率告警从来无法降级就绪
  状态（此前被恒为 False 的 `samples_dir_exists` 意外掩盖）。改为显式区分
  通过型检查 / 告警位 / 非判定项。

### Security

- **H1 LFI**: `/extract` `file_path` input now gated by `FC_ALLOWED_FILE_PATHS`
  (CSV directory whitelist). Symlinks and `../` escapes are blocked via
  `Path.resolve()` + `is_relative_to()`. Empty whitelist disables `file_path`
  entirely. (#1)
- **H2 SSRF**: `/extract` URL fetches now block cloud-metadata destinations
  (`169.254.169.254`, `fd00:ec2::254`). Other internal networks are delegated to
  Docker / K8s network policy per agreed risk model. Response body is streamed
  with a `max_file_mb` byte cap. (#2)
- **H3 OOM**: `file_base64` decoding now size-checked **before** allocation so a
  1 GB payload no longer forces a 1 GB allocation. (#3)
- **H14 XXE**: XML parser now uses `defusedxml.ElementTree` instead of the stdlib
  `xml.etree`, blocking external-entity and billion-laughs attacks. New runtime
  dependency: `defusedxml>=0.7.1`. (#14)
- **H10/H47 production guard**: `FC_REQUIRE_CONFIGURED_KEYS=true` (default) now
  causes uvicorn to fail fast at startup when no keys are configured, preventing
  accidental dev-fallback in production. Devs opt out with
  `FC_REQUIRE_CONFIGURED_KEYS=false`. (#10, #47)

### Reliability

- **H4**: Batch worker `_parse_one`, `/extract`, `/extract-and-verify`, `/verify`
  now log full tracebacks via `log.exception` instead of silently swallowing them. (#4)
- **H8**: docker-compose healthcheck interval restored to `30s` (was `3600s`). (#8)
- **H13**: New `utils.truncate(value, limit=200)` clamps error-message echoes;
  `PdfminerException` trailer text no longer leaks onto the wire. All
  error-producing endpoints (`/extract`, `/raw`, `/verify`, `/extract-and-verify`)
  routed through the helper. (#13)
- **H5**: `BaseHTTPMiddleware` replaced with a pure ASGI middleware that captures
  response status + headers via a custom `send` wrapper. Streaming / SSE /
  JSONResponse all flow through unchanged. (#5)
- **H6**: SIGHUP reload handler hot-reloads `KeyStore` (and LRU cache via H42) so
  operators can rotate `keys.json` without restarting the service. (#6)
- **H11**: `/verify` proxy now streams the upstream body via `aiter_bytes` and
  enforces a 10 MiB cap, so a malicious proxy cannot OOM the service. (#11)
- **H18**: `/health/full` now flags `cache_low_hit_ratio=True` when the LRU hit
  ratio drops below 5% over 50+ lookups (503 degraded). (#18)
- **H22**: `/health/full` resolves `logs/` and `samples/` relative to the installed
  package, not the process cwd. (#22)
- **H42**: New `cache.reload_if_settings_changed()` rebuilds the LRU singleton on
  SIGHUP when `FC_CACHE_SIZE` changes. (#42)

### CLI / Deployment

- **H20**: `fapiao_cn start` now probes `(host, port)` via `SO_REUSEADDR` and
  refuses to spawn uvicorn when the port is busy. (#20)
- **H23**: PID file is now written via `tempfile + os.replace()` with `0600` mode
  so concurrent readers cannot observe partial writes. (#23)
- **H28**: CLI docstring notes that production crash-restart relies on Docker /
  systemd / supervisord rather than an in-process supervisor. (#28)
- **H45**: Dockerfile builder stage now splits `pyproject.toml` from `src/` so a
  source-only change doesn't invalidate the (slow) pip download layer. (#45)
- **H46**: `fapiao_cn start` / `dev` now pin `cwd` to the package root so log paths
  and `/health/full` file lookups are deterministic. (#46)

### Configuration

- **H39**: KeyStore loading moved from lifespan into `create_app()` itself, so
  uvicorn fails fast at boot — not on the first request — when
  `FC_API_KEYS_FILE` is misconfigured. (#39)

### Parsers

- **H7**: `/formats` descriptions now sourced from the parser registry via
  `parsers.get_format_descriptions()` so adding a new parser surfaces
  documentation automatically. (#7)
- **H9/H34**: Documented that `_KNOWN_TAX_IDS` is process-local; multi-worker /
  multi-pod deployments don't share the cache. Marked as best-effort, not a
  contract (GDPR / PIPL advisory). (#9, #34)
- **H12**: `KeyRecord.verify_rpm` field + per-scope rate limiting so `/verify`
  has its own quota independent of `/extract`. New env:
  `FC_API_KEY_DEFAULT_VERIFY_RPM` (default 30). (#12)
- **H15/H40**: Expanded company-name keyword set (集团/银行/医院/学校/事务所/
  工作室/酒店/餐厅/商店) plus a negative list (部/办公室/办事处/中心) so
  department labels aren't mistaken for company names. (#15, #40)
- **H21**: New e-invoice parser now recognises `名称含义` / `名称查重` /
  `销售方名称` as parts of a name rather than as separators. (#21)
- **H25**: `_INVOICE_NO_EMBEDDED_RE` anchored with `(?=\s|$)` so it doesn't grab
  digits from a later line. (#25)
- **H26**: `_TOTAL_CN_RE` capture now stops at `备注` / `小写` so a 大写 amount in
  the 备注 row can no longer shadow the actual total. (#26)
- **H30**: `train_ticket` tax-id regex lengthened to 15-22 chars. (#30)

### Breaking

- `pdf_raw` field removed from `/extract`, `/extract/batch`, `/extract-and-verify`,
  and `parse_pdf()` response. Callers passing `?raw=true` on the regular endpoints
  will no longer receive a raw-text payload (the query parameter is silently
  ignored).

### Removed

- **`fapiao_cn gen-key`** — CLI 子命令删除（功能过于狭窄，与 `openssl rand` 等价）
  - 改用 `openssl rand -base64 24` + 手写 `.env` 即可
  - 删 `tests/test_cli.py`（4 条测试随之移除）
- **部署形态合并**：只保留 dev + Docker 两种部署方式
  - 删 `parse_vevn/`（in-tree venv，~180 MiB）
  - 删 `scripts/setup.sh` / `scripts/cli.sh` / `scripts/lint.sh`
  - 删 `scripts/install_portable.sh` / `scripts/uninstall_systemd.sh` /
    `scripts/build-portable.sh`
  - 删 `scripts/fapiao_cn.service.template` / `scripts/fapiao_cn.supervisor.conf`
  - 删 `build-scripts/`（`package_portable.py` / `verify_portable.py`）
  - 改 `README.md`：删 systemd / Supervisor / portable tarball 三段，TL;DR +
    快速开始改为 Docker / dev 双形态
  - 改 `pyproject.toml` / `.gitignore` / `.dockerignore`：去掉 `parse_vevn` 引用
  - 改 `src/fapiao_cn/cli.py` docstring：去掉 cli.sh 引用，明确只面向 dev
- 旧 PID 文件名 `.uvicorn.pid` 的硬编码引用（tarball 排除列表中保留兼容项以避免
  清理旧部署）

### Tests / Tooling

- **H32**: pytest `filterwarnings` extended to silence the
  `anyio.abc.BlockingPortal` deprecation. (#32)
- **H37**: `tests/conftest.py` now hard-fails if `FC_API_KEYS_FILE` is set,
  preventing accidental prod-key use. (#37)
- **H43**: README documents that the XML parser's field names are hard-coded
  against the current `EInvoice` schema; schema upgrades require manual sync. (#43)
- 新增 5 条 `items[]` 回归测试：字段映射与 `*类别*` 拆分、红字冲销行负值与缺失
  字段、38 行跨页累加、**全部 5 个 XML 样本金额自洽**、以及锁死「PDF / OFD 的
  `items` 必须为空」以防 OFD 日后被误开启。
- **`scripts/start-win.ps1` 启动问题的四组对照实验**（均为嵌套
  `powershell -File` 调用）：

  | 场景 | 结果 |
  | --- | --- |
  | sleeper + `Start-Process` 重定向 | 正常返回 |
  | uvicorn + `Start-Process` 重定向 | 正常返回 |
  | sleeper + 嵌套 `-File` | 正常返回 |
  | uvicorn + 嵌套 `-File` + 直接重定向 | **卡死**（原写法） |
  | uvicorn + 嵌套 `-File` + cmd 内部重定向 | 正常返回（修复后） |

  修复后：嵌套调用 **3.2 s** 返回（修复前卡满 180 s 超时），三个日志文件
  （`server.err.log` / `server.out.log` / `audit.log`）均正常写入，
  `/extract` 冒烟返回 `message=success` 且 `items` 4 条。
- Test count: **235 → 303 → 376 → 464** passing（CE 发行版；
  76 skipped / 2 xfailed）。带有效许可证时 **509** passing；
  企业版仓库另有 **23** 条许可证与插件测试。
- `mypy src/fapiao_cn`：29 文件零问题。`ruff check src tests`：全绿。

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

从旧名 `fpapi` 迁移：

```bash
# 旧 → 新
pip install -e .                            # 自动按新 name 装
fapiao_cn  start                            # was: fpapi start
FC_PORT=18091 fapiao_cn-server              # was: FPAPI_PORT=18091 fpapi-server
cat /etc/systemd/system/fapiao_cn.service   # was: fpapi.service
```
