"""发票消费类别分类。

把 ``InvoiceRecord`` 归类到 8 个常用类目（交通 / 住宿 / 餐饮 / 快递 /
通信 / 水费 / 电费 / 燃气 / 成品油），加 ``other``（票种级命中，中央非税
行政收费）和 ``unclassified``（L1–L4 全未命中时的独立兜底，**不与
``other`` 混用**），方便企业报销系统按类目做预算统计与凭证归档。

分类层级（命中即返回，**严格优先级**）：

    L1 票种      parser_name 在 TICKET_LEVEL_MAP 里直接查
    L2 卖方      seller_name 命中 SELLER_RULES 任一关键词
    L3 明细      items[].category / items[].name 命中 ITEM_RULES
    L4 税收编码  items[].tax_classification_code 前缀命中 CODE_PREFIX_RULES
    兜底          → expense_category = "unclassified" / "未归类" / source = ""
                     调用方应当独立处理（不能按 "other" 兜底）：
                     - 想看是否被分类，看 ``category != "unclassified"``
                     - 想看是否识别为 "其他" 类目，看 ``category == "other"``

    空字符串三字段 ( "", "", "" ) 用于：
    - 报关单显式跳过（不属于企业报销场景）
    - ``FC_EXPENSE_CATEGORY_ENABLED=false`` 关闭开关

同层多规则同时命中时按 ``priority`` 数字从大到小取第一；priority 相同时
按规则在表中顺序取第一。

**不**做运行时 JSON 覆盖开关（见 README 「配置」段 — 仅
``FC_EXPENSE_CATEGORY_ENABLED`` 控制总开关）。新增类目请直接改本文件
并提交 PR。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from fapiao_cn.schemas import InvoiceItem, InvoiceRecord

# ── 类目常量 ────────────────────────────────────────────────────────
#: 枚举字符串值（API 响应里 ``expense_category`` 字段取值）。
TRANSPORTATION = "transportation"
LODGING = "lodging"
DINING = "dining"
COURIER = "courier"
TELECOM = "telecom"
WATER = "water"
ELECTRICITY = "electricity"
GAS = "gas"
FUEL = "fuel"
OTHER = "other"
UNCLASSIFIED = "unclassified"

#: 中文显示标签（API 响应里 ``expense_category_label`` 字段取值）。
#: ``unclassified`` 翻译为「未归类」，**与 ``other``（其他）独立**——
#: 前者表示"我们没有规则命中它"，后者表示"我们识别为"其他"类目"。
CATEGORIES: "dict[str, str]" = {
    TRANSPORTATION: "交通",
    LODGING: "住宿",
    DINING: "餐饮",
    COURIER: "快递",
    TELECOM: "通信",
    WATER: "水费",
    ELECTRICITY: "电费",
    GAS: "燃气",
    FUEL: "成品油",
    OTHER: "其他",
    UNCLASSIFIED: "未归类",
}


# ── L1: 票种 → 类别 ─────────────────────────────────────────────────
#: 票种（解析器 dispatch 返回值）→ (category, label).
#: ``non_tax_receipt``（中央非税）落 ``other``（``source="ticket"``），
#: 让调用方知道这是票种级命中（行政事业性收费），便于单独统计。
TICKET_LEVEL_MAP: "dict[str, Tuple[str, str]]" = {
    "airline": (TRANSPORTATION, CATEGORIES[TRANSPORTATION]),
    "train": (TRANSPORTATION, CATEGORIES[TRANSPORTATION]),
    "merged_code": (FUEL, CATEGORIES[FUEL]),  # 成品油变体
    "non_tax_receipt": (OTHER, CATEGORIES[OTHER]),
}


# ── L2/L3 规则数据类 ────────────────────────────────────────────────
@dataclass(frozen=True)
class KeywordRule:
    """类目关键词命中规则。

    ``keywords`` 是归一化后的小写字符串片段；任一命中即认为整条规则触发。
    ``priority`` 字典同层冲突时的优先级，数字越大越优先。
    """

    category: str
    label: str
    keywords: Tuple[str, ...]
    priority: int = 0


def _rules() -> "dict[str, List[KeywordRule]]":
    """返回所有关键词规则（按类目分组，便于按类目遍历）。"""

    return {
        TRANSPORTATION: [
            # 卖方：网约车 / 出行 / 航空售票 / 铁路售票
            KeywordRule(TRANSPORTATION, CATEGORIES[TRANSPORTATION],
                        ("滴滴", "嘀嗒", "曹操", "t3出行", "首汽约车", "高德打车",
                         "携华", "享道", "欧了", "风韵出行", "妥妥E行",
                         "出行", "航空售票", "12306", "铁友", "携程", "去哪儿",
                         "航信", "航空服务", "铁路客票", "客运站", "出租汽车"),
                        priority=10),
            # 明细：交通运输 / 客运 / 网约车 / 打车
            KeywordRule(TRANSPORTATION, CATEGORIES[TRANSPORTATION],
                        ("交通运输服务", "运输服务", "客运服务", "客运服", "网约车",
                         "打车", "出租车", "航空运输", "水路运输", "铁路运输",
                         "公共电汽车客运", "城市轨道交通", "客运站", "订票费"),
                        priority=5),
        ],
        LODGING: [
            # 卖方：酒店 / 民宿
            KeywordRule(LODGING, CATEGORIES[LODGING],
                        ("酒店", "宾馆", "招待所", "民宿", "公寓", "客栈",
                         "如家", "汉庭", "锦江", "首旅", "桔子", "亚朵", "全季",
                         "格林", "七天", "速 8", "速八", "莫泰", "维也纳"),
                        priority=10),
            # 明细：住宿服务
            KeywordRule(LODGING, CATEGORIES[LODGING],
                        ("住宿服务", "住宿费", "房费", "住宿", "客房"),
                        priority=5),
        ],
        DINING: [
            # 卖方：餐饮 / 团餐 / 外卖平台
            KeywordRule(DINING, CATEGORIES[DINING],
                        ("餐饮", "餐厅", "饭店", "酒家", "酒楼", "美食",
                         "食堂", "快餐", "团餐", "海鲜", "火锅", "烧烤",
                         "海底捞", "西贝", "外婆家", "麦当劳", "肯德基", "星巴克",
                         "美团", "饿了么", "大众点评"),
                        priority=10),
            # 明细：餐饮服务 / 食品
            KeywordRule(DINING, CATEGORIES[DINING],
                        ("餐饮服务", "餐饮费", "餐费", "盒饭", "团餐", "外卖",
                         "食品"),
                        priority=5),
        ],
        COURIER: [
            # 卖方：快递 / 物流
            KeywordRule(COURIER, CATEGORIES[COURIER],
                        ("顺丰", "京东物流", "中通", "圆通", "申通", "韵达",
                         "百世", "邮政", "德邦", "极兔", "菜鸟", "丰巢", "速递",
                         "快递", "物流"),
                        priority=10),
            # 明细：快递 / 邮寄 / 收派
            KeywordRule(COURIER, CATEGORIES[COURIER],
                        ("快递服务", "收派服务费", "收派服务", "寄递服务",
                         "快递费", "邮寄"),
                        priority=5),
        ],
        TELECOM: [
            # 卖方：三大运营商 + 中国广电
            # 注：联通的全称是「中国联合网络通信」，必须独立命中关键词
            # （「联通」二字在全称里被「网络通信」拆开，不直接出现）。
            KeywordRule(TELECOM, CATEGORIES[TELECOM],
                        ("中国电信", "中国移动", "中国联通", "中国广电",
                         "联合网络通信", "联通", "电信", "移动通信", "移动通信"),
                        priority=10),
            # 明细：电信 / 话费 / 流量
            KeywordRule(TELECOM, CATEGORIES[TELECOM],
                        ("电信服务", "通信服务", "通信服务费", "话费",
                         "流量费", "宽带", "网络服务", "信息服务"),
                        priority=5),
        ],
        WATER: [
            KeywordRule(WATER, CATEGORIES[WATER],
                        ("自来水", "供水", "水资源", "水利"),
                        priority=10),
            KeywordRule(WATER, CATEGORIES[WATER],
                        ("水费", "自来水费", "供水费"),
                        priority=5),
        ],
        ELECTRICITY: [
            KeywordRule(ELECTRICITY, CATEGORIES[ELECTRICITY],
                        ("供电", "电力", "电网", "国网", "南方电网", "发电"),
                        priority=10),
            KeywordRule(ELECTRICITY, CATEGORIES[ELECTRICITY],
                        ("电费", "电力费"),
                        priority=5),
        ],
        GAS: [
            # 注：fuel 规则里的「中国石油」「中国石化」全资公司不能被
            # 本规则的「天然气」误命中 —— 见 fuel 注释里的 priority=11 解释。
            KeywordRule(GAS, CATEGORIES[GAS],
                        ("燃气", "煤气", "昆仑能源", "港华燃气",
                         "华润燃气", "新奥燃气"),
                        priority=10),
            KeywordRule(GAS, CATEGORIES[GAS],
                        ("燃气费", "天然气费", "煤气费"),
                        priority=5),
        ],
        FUEL: [
            # 卖方：加油站 / 油企 —— 全资央企名（"中国石化" / "中国石油" /
            # "中国海油"）必须以 priority=11 跑赢 gas 规则里的「天然气」二字
            # （中石油全称「中国石油天然气股份有限公司」包含「天然气」）。
            KeywordRule(FUEL, CATEGORIES[FUEL],
                        ("中国石化", "中国石油", "中国海油",
                         "加油站", "中石化", "中石油", "中海油", "龙禹石油",
                         "成品油", "石化"),
                        priority=11),
            # 明细：成品油 / 汽油 / 柴油
            KeywordRule(FUEL, CATEGORIES[FUEL],
                        ("成品油", "汽油", "柴油", "煤油", "燃料油",
                         "润滑油"),
                        priority=5),
        ],
    }


# ── L4: 税收分类编码前缀映射 ────────────────────────────────────────
#: 国家税务总局 19 位税收分类编码（前 7 位通常足够区分大类）。
#: 完整编码见《商品和服务税收分类与编码》表。本表只覆盖 8 大类里
#: L2/L3 没覆盖到的部分，例如油品批发（304099900）这种用卖方规则抓不到的。
CODE_PREFIX_RULES: "Tuple[Tuple[str, str, str], ...]" = (
    # (prefix, category, label)
    ("3040101", TRANSPORTATION, CATEGORIES[TRANSPORTATION]),  # 陆路运输服务
    ("3040102", TRANSPORTATION, CATEGORIES[TRANSPORTATION]),  # 水路运输服务
    ("3040103", TRANSPORTATION, CATEGORIES[TRANSPORTATION]),  # 航空运输服务
    ("3040104", TRANSPORTATION, CATEGORIES[TRANSPORTATION]),  # 管道运输服务
    ("3040201", DINING, CATEGORIES[DINING]),                  # 餐饮服务
    ("3040301", LODGING, CATEGORIES[LODGING]),                # 住宿服务
    ("3040501", COURIER, CATEGORIES[COURIER]),                # 快递服务
    ("3040502", COURIER, CATEGORIES[COURIER]),                # 邮政寄递服务
    ("3060101", TELECOM, CATEGORIES[TELECOM]),                # 基础电信服务
    ("3060102", TELECOM, CATEGORIES[TELECOM]),                # 增值电信服务
    ("3070201", WATER, CATEGORIES[WATER]),                    # 自来水
    ("3070202", ELECTRICITY, CATEGORIES[ELECTRICITY]),        # 电力
    ("3070204", GAS, CATEGORIES[GAS]),                        # 燃气
    ("3050101", FUEL, CATEGORIES[FUEL]),                      # 汽油
    ("3050102", FUEL, CATEGORIES[FUEL]),                      # 柴油
)


# ── 内部工具 ────────────────────────────────────────────────────────
def _hit_keywords(text: str, keywords: Iterable[str]) -> bool:
    """任一关键词在 ``text``（已小写、空格折叠）中出现即返回 True。"""

    if not text:
        return False
    norm = text.lower().replace(" ", "")
    for kw in keywords:
        if kw.replace(" ", "").lower() in norm:
            return True
    return False


def _flat_rules(
    rules: "dict[str, List[KeywordRule]]",
) -> List[Tuple[KeywordRule, int]]:
    """把按类目分组的规则表摊平成一条带**全局序号**的列表。

    全局序号是跨类目的 tie-breaker：模块文档承诺「priority 相同时按规则在
    表中顺序取第一」，而"表中顺序"是所有类目按声明顺序拼接出来的。只用类目
    内下标会让「先出现的类目里排在后面的规则」反过来赢过「后出现的类目里
    排在最前面的规则」，与文档不符。
    """

    flat: List[Tuple[KeywordRule, int]] = []
    for cat_rules in rules.values():
        for rule in cat_rules:
            flat.append((rule, len(flat)))
    return flat


def _match_code_prefix(
    items: List[InvoiceItem],
) -> Optional[Tuple[str, str, str]]:
    """任一明细的 19 位税收分类编码前缀命中 → 返回类目。"""

    for it in items:
        code = (it.tax_classification_code or "").strip()
        if len(code) < 7:
            continue
        for prefix, cat, lbl in CODE_PREFIX_RULES:
            if code.startswith(prefix):
                return (cat, lbl, "code")
    return None


# ── 主入口 ──────────────────────────────────────────────────────────
def classify_expense(
    record: InvoiceRecord,
    *,
    parser_name: str = "",
    enabled: bool = True,
) -> Tuple[str, str, str]:
    """对 ``InvoiceRecord`` 做消费类别分类，返回 ``(category, label, source)``。

    - ``enabled=False`` 时直接返回 ``("", "", "")``（FC_EXPENSE_CATEGORY_ENABLED 关闭）。
    - ``parser_name == "customs_declaration"`` 时显式跳过，返回 ``("", "", "")``。
    - ``parser_name`` 不为空时优先按 TICKET_LEVEL_MAP 命中。
    - 命中优先级：L1 票种 → L2 卖方关键词 → L3 商品明细关键词 → L4 税收编码前缀 → 兜底。

    返回的 ``source`` 取值之一：

    - ``"ticket"`` —— L1 票种命中
    - ``"seller"`` —— L2 卖方关键词命中
    - ``"item"``   —— L3 商品明细关键词命中
    - ``"code"``   —— L4 19 位税收分类编码命中
    - ``""``       —— L1–L4 全未命中时的兜底（``category="unclassified"`` / ``label="未归类"``，
                     **与 ``other`` 独立**）；或报关单显式跳过、``enabled=False`` 时的取值
                     （这两条 ``category`` / ``label`` 是空字符串，调用方结合上下文区分）
    """

    if not enabled:
        return ("", "", "")

    # L1: 报关单不参与消费分类（不属于报销场景）。
    # 即便卖方名称碰巧命中 L2 关键词、或者明细里有餐饮服务之类，
    # 海关出口货物报关单都不应被归到交通 / 餐饮 / 其他云类。
    if parser_name == "customs_declaration":
        return ("", "", "")

    # L1: 票种
    if parser_name:
        ticket = TICKET_LEVEL_MAP.get(parser_name)
        if ticket is not None:
            cat, lbl = ticket
            return (cat, lbl, "ticket")

    rules = _rules()

    # L2: 卖方关键词（按 priority 跨类目比较）
    seller_text = record.seller_name or ""
    best: Optional[Tuple[int, int, KeywordRule]] = None
    for rule, gidx in _flat_rules(rules):
        if not _hit_keywords(seller_text, rule.keywords):
            continue
        # 同 priority 时按规则在表中顺序取第一（全局序号 gidx 越小越优先）。
        rank = (rule.priority, -gidx)
        if best is None or rank > best[:2]:
            best = (rule.priority, -gidx, rule)
    if best is not None:
        _, _, rule = best
        return (rule.category, rule.label, "seller")

    # L3: 商品明细关键词（XML 全电发票填充了 items[]，PDF 通常为空）
    items = record.items or []
    if items:
        # 把所有明细的 category + name 拼成一个待匹配字符串
        item_text_parts: List[str] = []
        for it in items:
            if it.category:
                item_text_parts.append(it.category)
            if it.name:
                item_text_parts.append(it.name)
        item_text = " ".join(item_text_parts)
        if item_text:
            best = None
            for rule, gidx in _flat_rules(rules):
                if not _hit_keywords(item_text, rule.keywords):
                    continue
                rank = (rule.priority, -gidx)
                if best is None or rank > best[:2]:
                    best = (rule.priority, -gidx, rule)
            if best is not None:
                _, _, rule = best
                return (rule.category, rule.label, "item")

    # L4: 税收分类编码前缀
    if items:
        code_hit = _match_code_prefix(items)
        if code_hit is not None:
            return code_hit

    # 兜底：未命中任何规则时显式返回 "unclassified" / "未归类"，
    # 与 ``other``（票种级命中行政收费）独立——调用方按 category 是否为
    # ``unclassified`` 判断"是否有规则命中"，而不是按是否为空字符串。
    return (UNCLASSIFIED, CATEGORIES[UNCLASSIFIED], "")


__all__ = [
    "CATEGORIES",
    "TICKET_LEVEL_MAP",
    "CODE_PREFIX_RULES",
    "classify_expense",
    "TRANSPORTATION",
    "LODGING",
    "DINING",
    "COURIER",
    "TELECOM",
    "WATER",
    "ELECTRICITY",
    "GAS",
    "FUEL",
    "OTHER",
    "UNCLASSIFIED",
]