"""Unit tests for ``fapiao_cn.expense_category``."""

from __future__ import annotations

import pytest

from fapiao_cn.expense_category import (
    CATEGORIES,
    COURIER,
    DINING,
    ELECTRICITY,
    FUEL,
    GAS,
    LODGING,
    OTHER,
    TELECOM,
    TRANSPORTATION,
    UNCLASSIFIED,
    WATER,
    classify_expense,
)
from fapiao_cn.schemas import InvoiceItem, InvoiceRecord


class TestClassifyL1TicketLevel:
    """L1 票种级命中：parser_name 命中 TICKET_LEVEL_MAP."""

    def test_airline_ticket(self):
        rec = InvoiceRecord()
        cat, label, src = classify_expense(rec, parser_name="airline")
        assert (cat, label, src) == (TRANSPORTATION, "交通", "ticket")

    def test_train_ticket(self):
        rec = InvoiceRecord()
        cat, label, src = classify_expense(rec, parser_name="train")
        assert (cat, label, src) == (TRANSPORTATION, "交通", "ticket")

    def test_merged_code_is_fuel(self):
        rec = InvoiceRecord()
        cat, label, src = classify_expense(rec, parser_name="merged_code")
        # 成品油变体 → 成品油分类，源为票种
        assert (cat, label, src) == (FUEL, "成品油", "ticket")

    def test_non_tax_receipt_is_other_but_source_ticket(self):
        rec = InvoiceRecord()
        cat, label, src = classify_expense(rec, parser_name="non_tax_receipt")
        # 中央非税 → other 但保留 ticket 源标识
        assert (cat, label, src) == (OTHER, "其他", "ticket")

    def test_customs_declaration_never_classified(self):
        """报关单不属于报销场景：无论卖方 / 明细是否命中关键词，三字段恒为空。"""
        rec = InvoiceRecord(seller_name="某滴滴出行科技有限公司",
                            items=[InvoiceItem(category="交通运输服务", name="客运")])
        cat, label, src = classify_expense(rec, parser_name="customs_declaration")
        assert (cat, label, src) == ("", "", "")

    def test_unknown_parser_falls_through(self):
        rec = InvoiceRecord(seller_name="滴滴出行")
        cat, label, src = classify_expense(rec, parser_name="unknown_parser_xyz")
        # 未知 parser_name：不在 TICKET_LEVEL_MAP、也不是 customs_declaration，
        # fallthrough 到 L2 卖方关键词命中。
        assert cat == TRANSPORTATION
        assert src == "seller"


class TestClassifyL2Seller:
    """L2 卖方关键词命中."""

    @pytest.mark.parametrize(
        "seller_name, expected",
        [
            ("重庆滴滴出行科技有限公司", (TRANSPORTATION, "交通")),
            ("杭州嘀嗒出行信息技术有限公司", (TRANSPORTATION, "交通")),
            ("首汽约车北京分公司", (TRANSPORTATION, "交通")),
            ("北京如家酒店管理有限公司", (LODGING, "住宿")),
            ("汉庭星空（上海）酒店管理有限公司", (LODGING, "住宿")),
            ("锦江国际酒店管理有限公司", (LODGING, "住宿")),
            ("重庆福烤餐饮服务有限公司", (DINING, "餐饮")),
            ("海底捞火锅股份有限公司", (DINING, "餐饮")),
            ("浙江顺丰速运有限公司", (COURIER, "快递")),
            ("中通快递股份有限公司", (COURIER, "快递")),
            ("中国联合网络通信有限公司重庆市分公司", (TELECOM, "通信")),
            ("中国电信股份有限公司重庆分公司", (TELECOM, "通信")),
            ("中国移动通信集团重庆有限公司", (TELECOM, "通信")),
            ("重庆水资源产业股份有限公司西城自来水分公司", (WATER, "水费")),
            ("国网重庆市电力公司", (ELECTRICITY, "电费")),
            ("重庆燃气（集团）有限责任公司", (GAS, "燃气")),
            ("中石化重庆石油分公司", (FUEL, "成品油")),
            ("重庆龙 禹石油有限公司富源加油站", (FUEL, "成品油")),
        ],
    )
    def test_seller_keyword_hit(self, seller_name, expected):
        rec = InvoiceRecord(seller_name=seller_name)
        cat, label, src = classify_expense(rec, parser_name="new")
        assert (cat, label) == expected
        assert src == "seller"

    def test_seller_priority_over_item(self):
        """L2 卖方优先于 L3 明细：当明细里有「住宿费」但卖方是「滴滴出行」，
        仍应归 transportation."""

        rec = InvoiceRecord(
            seller_name="重庆滴滴出行科技有限公司",
            items=[InvoiceItem(category="住宿费", name="酒店住宿")],
        )
        cat, _, src = classify_expense(rec, parser_name="new")
        assert cat == TRANSPORTATION
        assert src == "seller"


class TestClassifyL3Items:
    """L3 明细关键词命中（PDF 通常 items=[]，XML 全电发票才有明细）."""

    @pytest.mark.parametrize(
        "items, expected",
        [
            # 交通
            ([InvoiceItem(category="交通运输服务", name="客运服务费")], TRANSPORTATION),
            ([InvoiceItem(category="运输服务", name="客运")], TRANSPORTATION),
            # 住宿
            ([InvoiceItem(category="住宿服务", name="住宿费")], LODGING),
            # 餐饮
            ([InvoiceItem(category="餐饮服务", name="餐饮费")], DINING),
            ([InvoiceItem(category="食品", name="盒饭")], DINING),
            # 快递
            ([InvoiceItem(category="物流辅助服务", name="收派服务费")], COURIER),
            # 通信
            ([InvoiceItem(category="电信服务", name="通信服务费")], TELECOM),
            # 水电燃气
            ([InvoiceItem(category="自来水", name="水费")], WATER),
            ([InvoiceItem(category="电力", name="电费")], ELECTRICITY),
            ([InvoiceItem(category="燃气", name="燃气费")], GAS),
            # 成品油
            ([InvoiceItem(category="汽油", name="92号车用汽油")], FUEL),
            ([InvoiceItem(category="成品油", name="柴油")], FUEL),
        ],
    )
    def test_item_keyword_hit(self, items, expected):
        rec = InvoiceRecord(items=items)
        cat, _, src = classify_expense(rec, parser_name="new")
        assert cat == expected
        assert src == "item"

    def test_item_category_used_when_name_empty(self):
        rec = InvoiceRecord(items=[InvoiceItem(category="交通运输服务", name="")])
        cat, _, src = classify_expense(rec, parser_name="new")
        assert (cat == TRANSPORTATION) and (src == "item")


class TestClassifyL4Code:
    """L4 19 位税收分类编码前缀命中."""

    def test_transport_code(self):
        rec = InvoiceRecord(items=[InvoiceItem(tax_classification_code="3040101000000000000")])
        cat, _, src = classify_expense(rec, parser_name="new")
        assert (cat == TRANSPORTATION) and (src == "code")

    def test_dining_code(self):
        rec = InvoiceRecord(items=[InvoiceItem(tax_classification_code="3040201000000000000")])
        cat, _, src = classify_expense(rec, parser_name="new")
        assert (cat == DINING) and (src == "code")

    def test_lodging_code(self):
        rec = InvoiceRecord(items=[InvoiceItem(tax_classification_code="3040301000000000000")])
        cat, _, src = classify_expense(rec, parser_name="new")
        assert (cat == LODGING) and (src == "code")

    def test_courier_code(self):
        rec = InvoiceRecord(items=[InvoiceItem(tax_classification_code="3040501000000000000")])
        cat, _, src = classify_expense(rec, parser_name="new")
        assert (cat == COURIER) and (src == "code")

    def test_fuel_code(self):
        rec = InvoiceRecord(items=[InvoiceItem(tax_classification_code="3050101000000000000")])
        cat, _, src = classify_expense(rec, parser_name="new")
        assert (cat == FUEL) and (src == "code")

    def test_short_code_no_hit(self):
        rec = InvoiceRecord(items=[InvoiceItem(tax_classification_code="3040")])
        cat, _, src = classify_expense(rec, parser_name="new")
        # 编码短于 7 位跳过 L4，应该 fallthrough 到 unclassified 兜底
        assert cat == UNCLASSIFIED
        assert src == ""


class TestClassifyFallback:
    """兜底：未命中时显式归 ``unclassified``（与 ``other`` 独立）；报关单 / 关闭开关保持空。"""

    def test_empty_record(self):
        rec = InvoiceRecord()
        cat, label, src = classify_expense(rec, parser_name="new")
        assert (cat, label, src) == (UNCLASSIFIED, CATEGORIES[UNCLASSIFIED], "")

    def test_unrelated_seller(self):
        rec = InvoiceRecord(seller_name="某不知名公司", items=[InvoiceItem(name="普通商品")])
        cat, _, src = classify_expense(rec, parser_name="new")
        # 卖方不命中任何关键词、items 也不命中 → 兜底归 unclassified
        assert cat == UNCLASSIFIED
        assert src == ""

    def test_industrial_ecommerce_seller_falls_to_unclassified(self):
        """沭阳俊迪电子商务有限公司这种工业品电商：不在 L2 任何关键词里，
        但解析成功且属于报销场景，必须显式归 ``unclassified``（独立兜底，
        不能与 ``other`` 混用）。"""

        rec = InvoiceRecord(seller_name="沭阳俊迪电子商务有限公司")
        cat, label, src = classify_expense(rec, parser_name="new")
        assert cat == UNCLASSIFIED
        assert label == CATEGORIES[UNCLASSIFIED]
        assert src == ""
        # 与 other 互斥：明明没命中任何规则，绝不能落到 other
        assert cat != OTHER

    def test_other_and_unclassified_are_distinct(self):
        """``other`` 是 L1 票种命中（中央非税），``unclassified`` 是 L1–L4 全未命中。
        两类不能混用：调用方应能区分"识别为 other 类目"和"未命中"两种语义。"""

        # 走 L1 票种命中 other
        rec = InvoiceRecord()
        cat, _, src = classify_expense(rec, parser_name="non_tax_receipt")
        assert cat == OTHER
        assert src == "ticket"

        # 走 L1–L4 全未命中兜底 unclassified
        rec2 = InvoiceRecord(seller_name="某不知名公司")
        cat2, _, src2 = classify_expense(rec2, parser_name="new")
        assert cat2 == UNCLASSIFIED
        assert src2 == ""

    def test_disabled_flag(self):
        """``enabled=False`` 不是兜底未命中，是关闭开关——保持空字符串。"""

        rec = InvoiceRecord(seller_name="滴滴出行")
        cat, _, src = classify_expense(rec, parser_name="new", enabled=False)
        assert (cat, src) == ("", "")


class TestCategoriesMapping:
    """CATEGORIES 字典完整性."""

    def test_all_keys_have_labels(self):
        for cat, label in CATEGORIES.items():
            assert isinstance(cat, str)
            assert isinstance(label, str)
            assert label  # 非空

    def test_categories_count(self):
        # 8 大类（交通/住宿/餐饮/快递/通信/水费/电费/燃气/成品油）+ other（票种级命中）
        # + unclassified（兜底未命中）= 11 项。
        assert len(CATEGORIES) == 11
        assert OTHER in CATEGORIES
        assert UNCLASSIFIED in CATEGORIES
        assert CATEGORIES[UNCLASSIFIED] == "未归类"