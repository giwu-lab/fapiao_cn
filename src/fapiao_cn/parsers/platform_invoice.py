"""平台下载版电子发票解析器（标签块在上、值块在下）。

版式特征
--------
部分发票平台（下载页导出的 PDF）把票面拆成两段写进内容流：第一段是
**标签块**（``发票号码：`` / ``开票日期：`` / ``名称：`` / 统一社会信用代码…），
第二段才是**值块**（号码 / 日期 / 名称 / 税号 / 明细 / 合计 / 大写金额）。

两段在票面上其实是**同行相邻**的，但 ``pdfplumber`` 的文本层顺序就是内容流
顺序，于是 ``发票号码：`` 后面是空的，号码却排在文本末尾 —— 依赖「标签: 值」
同行的标准解析器会把号码、日期、金额全部丢掉，只剩下从别处兜底捞到的
买方 / 卖方名称。

实测样本（``个人代开_01`` / ``购_自来水_01`` / 志诚）都是这个模板。前两份
带「统一发票监制章」，号码与日期还能被兜底正则捞到；不带监制章的那份
（发票号码 / 开票日期 / 金额 / 大写金额全部丢失）就是本模块要解决的问题。

做法
----
按**词坐标**重建阅读顺序：把词按 ``(top, x0)`` 排序、按行距聚成行，
逐行拼回文本，再交给 :mod:`fapiao_cn.parsers.invoice` 里已验证的取值函数。
坐标来自 ``ctx["page"].words()``（生产链路）或 ``ctx["words"]``（测试链路）。

重建后的文本还要剔除「购买方信息 / 销售方信息」的**竖排表头字** —— 它们在
坐标上与买方 / 卖方名称同一行，混进文本会把 ``名称：`` 的取值切错。

版式识别
--------
:func:`looks_like_platform_layout` 用**结构特征**而不是关键字识别：
``发票号码 / 发票代码 / 开票日期 / 开票人`` 里至少有两个以「标签 + 冒号」
独占一行（后面没有值）。普通票面这些标签都带值同行，所以不会误判。
"""

from __future__ import annotations

import re
import statistics
from typing import Dict, List, Sequence, Set, Tuple

from fapiao_cn.parsers.invoice import parse_new_electronic_invoice
from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import normalize_invoice_text

# 「标签 + 冒号独占一行」—— 平台下载版最稳定的结构特征。
_EMPTY_LABEL_RE = re.compile(
    r"^\s*(发\s*票\s*号\s*码|发\s*票\s*代\s*码|开\s*票\s*日\s*期|开\s*票\s*人)\s*[:：]\s*$",
    re.MULTILINE,
)

# 「购买方信息 / 销售方信息」的竖排表头字：每个字独占一格，纵向排成一列。
_VERTICAL_HEADER_CHARS = frozenset("购买销售方信息")

# 竖排表头字的 x 位置容差（pt）—— 同一列的字 x0 可能差零点几个 pt。
_X_BUCKET_TOLERANCE = 1.5

# 参与「重建结果是否更好」统计的字段。
_SCORED_FIELDS: Tuple[str, ...] = (
    "invoice_type",
    "invoice_number",
    "issue_date",
    "buyer_name",
    "buyer_tax_id",
    "seller_name",
    "seller_tax_id",
    "amount_without_tax",
    "tax_amount",
    "total_amount",
    "total_amount_upper",
    "drawer",
)


def looks_like_platform_layout(text: str) -> bool:
    """判断文本是否来自「标签块在上、值块在下」的平台下载版式。

    判据：正文是电子发票，且 ``发票号码 / 发票代码 / 开票日期 / 开票人``
    里至少**两个**以「标签 + 冒号」独占一行（冒号后面没有值）。普通票面
    这些标签都带值同行，因此不会误判。

    要数的是**不同**标签：同一张票面里 ``发票号码：`` 出现两次也只算一种。

    文本先过一遍 :func:`~fapiao_cn.utils.normalize_invoice_text`：平台下载版
    的「电子发票」标题里可能带康熙部首 ``⼦``，不归一化的话
    ``"电子发票" in text`` 会判空 —— 调用方传的是原始文本还是清洗后的
    文本都可能，检测器不能依赖其中之一。
    """

    if not text:
        return False
    text = normalize_invoice_text(text)
    if "电子发票" not in text:
        return False
    labels = {
        re.sub(r"\s+", "", m.group(1)) for m in _EMPTY_LABEL_RE.finditer(text)
    }
    return len(labels) >= 2


def _ctx_words(ctx: dict) -> List[Dict]:
    """取出当前页的词坐标列表。

    生产链路（``pipeline.run``）给的是 ``ctx["page"]``（PageView，words
    懒加载）；测试链路（``tests._helpers.parse_pdf``）直接给 ``ctx["words"]``。
    两条都支持，解析器才能被单元测试直接驱动。
    """

    words = ctx.get("words")
    if words:
        return list(words)
    page = ctx.get("page")
    if page is not None:
        getter = getattr(page, "words", None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:  # noqa: BLE001 - 坐标提取失败不该拖垮整单解析
                return []
    return []


def _row_tolerance(words: Sequence[Dict]) -> float:
    """同一行的词在纵向上允许的偏移（pt）。

    取词高中位数的一个比例：9pt 字号 → 3.6pt。上下限 2~6pt，既能容忍
    标签与值的基线差（票面上常见 1~3pt），又不会把相邻两行并成一行。
    """

    heights: List[float] = []
    for w in words:
        height = w.get("height")
        if not height:
            height = float(w.get("bottom", 0)) - float(w.get("top", 0))
        if height and height > 0:
            heights.append(float(height))
    if not heights:
        return 3.0
    return max(2.0, min(6.0, 0.4 * statistics.median(heights)))


def _vertical_header_indices(words: Sequence[Dict]) -> Set[int]:
    """找出「购买方信息 / 销售方信息」这类竖排表头字的下标。

    竖排表头的特征是**同一个 x 位置上叠着多个单字**（每个字占一格往下
    排）。这些字在坐标上与买方 / 卖方名称同一行，混进文本会把「名称：」
    的取值切错（卖方名称会被截成「售」），所以要剔除。
    """

    buckets: List[List[Tuple[float, int]]] = []
    for idx, w in enumerate(words):
        text = w.get("text", "")
        if len(text) != 1 or text not in _VERTICAL_HEADER_CHARS:
            continue
        x0 = float(w.get("x0", 0.0))
        for bucket in buckets:
            if abs(bucket[0][0] - x0) <= _X_BUCKET_TOLERANCE:
                bucket.append((x0, idx))
                break
        else:
            buckets.append([(x0, idx)])
    drop: Set[int] = set()
    for bucket in buckets:
        if len(bucket) >= 2:
            drop.update(idx for _, idx in bucket)
    return drop


def _group_rows(words: Sequence[Dict], tolerance: float) -> List[List[Dict]]:
    """按纵向间距把词聚成行，行内按 x 从左到右排好。

    一个词并入当前行需要同时满足两个条件：与**上一个词**的纵向差在容差内
    （相邻即同行），且与**行首**的纵向差也在容差内（防止一行被无限拉长）。
    """

    ordered = sorted(words, key=lambda w: (float(w["top"]), float(w["x0"])))
    rows: List[List[Dict]] = []
    current: List[Dict] = []
    previous_top = 0.0
    for w in ordered:
        top = float(w["top"])
        if (
            current
            and (top - previous_top) <= tolerance
            and (top - float(current[0]["top"])) <= tolerance
        ):
            current.append(w)
        else:
            if current:
                rows.append(current)
            current = [w]
        previous_top = top
    if current:
        rows.append(current)
    for row in rows:
        row.sort(key=lambda w: float(w["x0"]))
    return rows


def _visual_text(words: Sequence[Dict]) -> str:
    """按词坐标重建「视觉阅读顺序」的文本。

    竖排水印（如平台盖的「下载次数：1」）的 ``upright`` 为 ``False``，
    直接排除 —— 它不是票面内容，只是平台标记。
    """

    if not words:
        return ""
    upright = [w for w in words if w.get("upright", True)]
    if not upright:
        return ""
    drop = _vertical_header_indices(upright)
    kept = [w for i, w in enumerate(upright) if i not in drop]
    if not kept:
        return ""
    rows = _group_rows(kept, _row_tolerance(kept))
    lines = [" ".join(str(w.get("text", "")) for w in row) for row in rows]
    return normalize_invoice_text("\n".join(lines))


def _score(record: InvoiceRecord) -> int:
    """记录里命中了多少个关键字段 —— 用来比较两种解析结果的好坏。"""

    return sum(1 for field in _SCORED_FIELDS if getattr(record, field, ""))


def parse_platform_invoice(text: str, ctx: dict) -> InvoiceRecord:
    """解析平台下载版电子发票。

    先按词坐标重建阅读顺序再取值；如果重建后命中的字段反而比原始文本少
    （说明这份 PDF 不是平台版式、或坐标不可用），就退回原始文本的结果，
    保证**永远不会比原来更差**。
    """

    baseline = parse_new_electronic_invoice(text, ctx)
    visual = _visual_text(_ctx_words(ctx))
    if not visual:
        return baseline
    rebuilt = parse_new_electronic_invoice(visual, ctx)
    return rebuilt if _score(rebuilt) >= _score(baseline) else baseline


__all__ = [
    "looks_like_platform_layout",
    "parse_platform_invoice",
]
