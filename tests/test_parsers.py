"""Unit tests for individual parser functions."""

from __future__ import annotations

import pytest

from fapiao_cn.parsers.invoice import (
    _extract_amounts,
    _extract_invoice_number,
    _extract_invoice_type,
    _extract_issue_date,
    _extract_names_and_tax_ids,
)
from fapiao_cn.parsers.merged_code import (
    _extract_buyer_from_cipher_lines,
    _extract_seller_after_total,
)
from fapiao_cn.utils import extract_people, looks_like_company_name, looks_like_tax_id
from tests._helpers import ee_feature_enabled


class TestLooksLikeCompanyName:
    def test_company_keyword(self):
        assert looks_like_company_name("重庆市旺成科技股份有限公司")
        assert looks_like_company_name("重庆京东创盟信息技术有限公司")

    def test_too_short(self):
        assert not looks_like_company_name("abc")
        assert not looks_like_company_name("")

    def test_no_keyword(self):
        assert not looks_like_company_name("随便一段文字")


class TestExtractInvoiceType:
    def test_normal(self):
        text = "电子发票（增值税专用发票）"
        assert _extract_invoice_type(text) == "电子发票（增值税专用发票）"

    def test_common(self):
        text = "电子发票（普通发票）"
        assert _extract_invoice_type(text) == "电子发票（普通发票）"

    def test_with_spaces(self):
        text = "电 子 发 票（增 值 税 专 用 发 票）"
        assert _extract_invoice_type(text) == "电子发票（增值税专用发票）"

    def test_empty(self):
        assert _extract_invoice_type("") == ""


class TestExtractInvoiceNumber:
    def test_standard(self):
        text = "发票代码：255020000001\n发票号码：29241041"
        code, no = _extract_invoice_number(text)
        assert code == "255020000001"
        assert no == "29241041"

    def test_embedded_in_receipt_monitor(self):
        text = "统一发票监制 26502000001100907556"
        code, no = _extract_invoice_number(text)
        assert code == ""
        assert no == "26502000001100907556"

    def test_empty(self):
        assert _extract_invoice_number("") == ("", "")


class TestExtractIssueDate:
    def test_standard(self):
        text = "开票日期：2025年11月27日"
        assert _extract_issue_date(text) == "2025-11-27"

    def test_after_tax_bureau(self):
        text = "国家税务总局 2026年06月08日"
        assert _extract_issue_date(text) == "2026-06-08"

    def test_with_spaces(self):
        text = "开票日期  :  2025  年  11  月  27  日"
        assert _extract_issue_date(text) == "2025-11-27"


class TestExtractNamesAndTaxIds:
    def test_standard_two_lines(self):
        text = (
            "名 称 : 重庆市旺成科技股份有限公司\n"
            "纳税人识别号 : 91500106622015389N\n"
            "名 称 : 维克多汽车技术（上海）有限公司\n"
            "纳税人识别号 : 91310000692939379C\n"
        )
        buyer, buyer_tax, seller, seller_tax = _extract_names_and_tax_ids(text)
        assert buyer == "重庆市旺成科技股份有限公司"
        assert buyer_tax == "91500106622015389N"
        assert seller == "维克多汽车技术（上海）有限公司"
        assert seller_tax == "91310000692939379C"

    def test_two_column_same_line_with_colon(self):
        text = (
            "购 销\n"
            "买 名 称 : 重庆市旺成科技股份有限公司 售 名 称 : 重庆京东创盟信息技术有限公司\n"
            "信 统一社会信用代码/纳税人识别号： 统一社会信用代码/纳税人识别号：\n"
            "91500106622015389N 91310000692939379C\n"
        )
        buyer, buyer_tax, seller, seller_tax = _extract_names_and_tax_ids(text)
        assert buyer == "重庆市旺成科技股份有限公司"
        assert seller == "重庆京东创盟信息技术有限公司"

    def test_two_column_with_value_on_subsequent_line(self):
        text = (
            "购 销\n"
            "买 名称： 售 名称：\n"
            "信 息 ...\n"
            "项目名称 规格型号 单 价\n"
            "*水*水费 100 10\n"
            "重庆市旺成科技股份有限公司 重庆水资源产业股份有限公司西城自来水分公司\n"
            "91500106622015389N 91500107MA61QQQ4XP\n"
        )
        buyer, buyer_tax, seller, seller_tax = _extract_names_and_tax_ids(text)
        assert buyer == "重庆市旺成科技股份有限公司"
        assert buyer_tax == "91500106622015389N"
        assert seller == "重庆水资源产业股份有限公司西城自来水分公司"
        assert seller_tax == "91500107MA61QQQ4XP"

    def test_personal_dekai_merged_row(self):
        """个人代开 variant: column header bytes are split across rows;
        the actual buyer/seller names sit on a single physical line
        under the tax-id header, with both tax ids on the next line."""

        text = (
            "购 销\n"
            "买 名称： 售 名称：\n"
            "方 方\n"
            "信 信 统一社会信用代码/纳税人识别号： 统一社会信用代码/纳税人识别号：\n"
            "息 息\n"
            "项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额\n"
            "重庆市旺成科技股份有限公司 颜国明\n"
            "91500106622015389N 92500106MA5XUBM0XC\n"
        )
        buyer, buyer_tax, seller, seller_tax = _extract_names_and_tax_ids(text)
        assert buyer == "重庆市旺成科技股份有限公司"
        assert buyer_tax == "91500106622015389N"
        assert seller == "颜国明"
        assert seller_tax == "92500106MA5XUBM0XC"

    def test_missing_tax_id_backfilled_from_cache(self, monkeypatch):
        """购_餐饮_03.pdf style: company name is printed but the
        统一社会信用代码/纳税人识别号 value is omitted.  When the
        (name -> tax_id) cache has been seeded with that company, the
        parser must back-fill the missing slot."""

        from fapiao_cn.parsers import invoice as inv_mod

        # Reset cache to a known state.
        inv_mod.clear_company_tax_id_cache()
        try:
            inv_mod.record_company_tax_id(
                "重庆市旺成科技股份有限公司", "91500106622015389N"
            )

            text = (
                "电子发票（普通发票）\n"
                "购 销\n"
                "买 名称:重庆市旺成科技股份有限公司 售 名称:杭州宜家家居有限公司上海分公司\n"
                "方 方\n"
                "统一社会信用代码/纳税人识别号: 统一社会信用代码/纳税人识别号:\n"
                "信 信\n"
                "息 息\n"
                "项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额\n"
                "*餐饮服务*餐饮服务 件 1 37.73 37.73 6% 2.26\n"
                "合 计 37.73 2.26\n"
                "价税合计(大写) 叁拾玖圆玖角玖分 （小写） 39.99\n"
                "收款人:徐洁敏; 复核人:陈晨;\n"
                "开票人:汪兴平\n"
            )
            buyer, buyer_tax, seller, seller_tax = _extract_names_and_tax_ids(text)
            assert buyer == "重庆市旺成科技股份有限公司"
            assert buyer_tax == "91500106622015389N"  # back-filled from cache
            assert seller == "杭州宜家家居有限公司上海分公司"
            assert seller_tax == ""  # never seen, no cache entry
        finally:
            inv_mod.clear_company_tax_id_cache()

    def test_successful_extraction_populates_cache(self):
        """Resolving a (name, tax_id) pair should register it in the
        cache so subsequent lookups succeed even without a fresh
        successful extraction."""

        from fapiao_cn.parsers import invoice as inv_mod

        inv_mod.clear_company_tax_id_cache()
        try:
            text = (
                "名 称 : 重庆市旺成科技股份有限公司\n"
                "纳税人识别号 : 91500106622015389N\n"
                "名 称 : 维克多汽车技术（上海）有限公司\n"
                "纳税人识别号 : 91310000692939379C\n"
            )
            _extract_names_and_tax_ids(text)
            assert (
                inv_mod.lookup_company_tax_id("重庆市旺成科技股份有限公司")
                == "91500106622015389N"
            )
            assert (
                inv_mod.lookup_company_tax_id("维克多汽车技术（上海）有限公司")
                == "91310000692939379C"
            )
            assert inv_mod.lookup_company_tax_id("未知的公司") == ""
        finally:
            inv_mod.clear_company_tax_id_cache()


class TestExtractAmounts:
    def test_standard(self):
        text = (
            "合 计 ¥124018.66 ¥16122.43\n"
            "价税合计（大写） 壹拾肆万零壹佰肆拾壹圆零玖分 （小写） ¥140141.09"
        )
        sub, tax, total, cn = _extract_amounts(text)
        assert sub == "124018.66"
        assert tax == "16122.43"
        assert total == "140141.09"
        assert cn == "壹拾肆万零壹佰肆拾壹圆零玖分"

    def test_amounts_before_heji(self):
        text = (
            "¥27.69 ¥0.83\n"
            "合 计\n"
            "价税合计（大写） 贰拾捌圆伍角贰分 （小写） ¥28.52"
        )
        sub, tax, total, cn = _extract_amounts(text)
        assert sub == "27.69"
        assert tax == "0.83"
        assert total == "28.52"
        assert cn == "贰拾捌圆伍角贰分"

    def test_zero_amount(self):
        text = (
            "合 计 ¥0.00 ¥0.00\n"
            "价税合计（大写） 零圆整 （小写） ¥0.00"
        )
        sub, tax, total, cn = _extract_amounts(text)
        assert sub == "0.00"
        assert tax == "0.00"
        assert total == "0.00"
        assert cn == "零圆整"

    def test_amounts_with_negative_line_items(self):
        """购_滴滴_01: line items include a negative refund row.  The
        ``合 计`` row reports the net amounts (156.80 - 30.87 = 125.93)
        and ``价税合计`` shows the grand total (129.70).  Make sure the
        fallback window picker does not mistakenly include a negative
        price from the table when ``合 计`` is followed by nothing."""

        text = (
            "*交通运输服务*客运服 156.80 1 156.80 3% 4.70\n"
            "*交通运输服务*客运服 -30.87 3% -0.93\n"
            "合 计 ¥125.93 ¥3.77\n"
            "出行人 有效身份证件号 出行日期 出发地 到达地\n"
            "价税合计（大写） 壹佰贰拾玖圆柒角整 （小写）¥129.70\n"
        )
        sub, tax, total, cn = _extract_amounts(text)
        assert sub == "125.93"
        assert tax == "3.77"
        assert total == "129.70"
        assert cn == "壹佰贰拾玖圆柒角整"

    def test_total_with_single_decimal_digit(self):
        """购_滴滴_02: total is printed as ``¥ 46.3`` without trailing
        zero.  Must normalise to ``46.30`` and still pair with the
        preceding 大写."""

        text = (
            "合 计 ¥ 44.95 ¥ 1.35\n"
            "出行人 有效身份证件号 出行日期 出发地 到达地\n"
            "价税合计(大写) ⨂ 肆拾陆元叁角 (小写) ¥ 46.3\n"
        )
        sub, tax, total, cn = _extract_amounts(text)
        assert sub == "44.95"
        assert tax == "1.35"
        assert total == "46.30"
        assert cn == "肆拾陆元叁角"

    def test_total_upper_strips_noise_glyph(self):
        """Daxie capture includes a stray ``⨂`` glyph (watermark).  The
        ``_AMOUNT_CN_RE`` post-filter must strip it and return only the
        valid Chinese-amount characters."""

        text = "价税合计(大写) ⨂ 壹佰元整 (小写) ¥100"
        _, _, total, cn = _extract_amounts(text)
        assert total == "100.00"
        assert cn == "壹佰元整"

    def test_tax_free_heji_with_wildcard(self):
        """购_通信_01.pdf style: 合计行只有金额 ``¥358.00`` 和 ``*``，
        没有税额数字。``tax_amount`` 必须为 ``"0.00"``，而不是从
        窗口里抓一个数字填充。"""

        text = (
            "*电信服务*通信服务费 无 次 1 358 358.00 * *\n"
            "合 计 ¥358.00 *\n"
            "价税合计（大写） 叁佰伍拾捌圆整 （小写）¥358.00"
        )
        sub, tax, total, _ = _extract_amounts(text)
        assert sub == "358.00"
        assert tax == "0.00"
        assert total == "358.00"

    def test_fallback_does_not_double_assign_single_price(self):
        """当窗口内只有 1 个带币种前缀的数字时（合计行被换行或格式
        打散），``tax_amount`` 必须留空，而不是把同一金额复制。"""

        text = (
            "项目名称 单价 金额 税率 税额\n"
            "*服务*服务费 次 1 100.00 100.00\n"
            "合 计\n"
            "¥100.00\n"
            "价税合计（大写） 壹佰元整 （小写）¥100.00"
        )
        sub, tax, total, _ = _extract_amounts(text)
        assert sub == "100.00"
        assert tax == ""
        assert total == "100.00"


class TestExtractPeople:
    def test_inline(self):
        text = "收款人: 张三 复核: 李四 开票人: 王五"
        payee, reviewer, drawer = extract_people(text)
        assert payee == "张三"
        assert reviewer == "李四"
        assert drawer == "王五"

    def test_standard_line(self):
        text = "收款人：蓝桂涛\n复核：王秋华\n开票人：高玉玲"
        payee, reviewer, drawer = extract_people(text)
        assert payee == "蓝桂涛"
        assert reviewer == "王秋华"
        assert drawer == "高玉玲"

    def test_empty(self):
        assert extract_people("") == ("", "", "")


class TestMergedCodeHelpers:
    def test_looks_like_tax_id(self):
        assert looks_like_tax_id("91500106622015389N")
        assert looks_like_tax_id("91310000692939379C")
        assert not looks_like_tax_id("")
        assert not looks_like_tax_id("short")

    def test_extract_buyer_from_cipher_lines(self):
        lines = [
            "header",
            "重庆市旺成科技股份有限公司 >63*4<706-041<+/4149-2<76<>",
            "91500106622015389N >63*4<...",
        ]
        name, tax = _extract_buyer_from_cipher_lines(lines)
        assert name == "重庆市旺成科技股份有限公司"
        assert tax == "91500106622015389N"

    def test_extract_buyer_when_cipher_mixed_with_digits(self):
        """购_油品_03.pdf style: the cipher text starts with digits and
        ``+/-`` chars right after the company name, so a naive
        ``split(">")`` would still leave cipher characters trailing the
        company name and fail ``looks_like_company_name``."""

        lines = [
            "header",
            "重庆市旺成科技股份有限公司 1+64558-0/0<</>2<4<<8147//4",
            "91500106622015389N 629<+2103/4+791<4+//68273>*",
            "72+3++8648-1>-23<564-66+8**",
            ">-9<70->2+723/4<6+0*464->>7",
        ]
        name, tax = _extract_buyer_from_cipher_lines(lines)
        assert name == "重庆市旺成科技股份有限公司"
        assert tax == "91500106622015389N"

    def test_extract_company_prefix_three_strategies(self):
        from fapiao_cn.parsers.merged_code import _extract_company_prefix

        # Strategy 1: split by cipher/space, first company-name token wins.
        assert (
            _extract_company_prefix(
                "重庆市旺成科技股份有限公司 >63*4<706-041<+/4149-2<76<>"
            )
            == "重庆市旺成科技股份有限公司"
        )
        # Strategy 1 also handles the digit-leading cipher variant from
        # 购_油品_03.
        assert (
            _extract_company_prefix(
                "重庆市旺成科技股份有限公司 1+64558-0/0<</>2<4<<8147//4"
            )
            == "重庆市旺成科技股份有限公司"
        )
        # Strategy 3 fallback: when there is no space and the entire
        # prefix before the first cipher char passes
        # ``looks_like_company_name``, return as-is.
        assert (
            _extract_company_prefix("重庆京东创盟信息技术有限公司<1+2-3/4>")
            == "重庆京东创盟信息技术有限公司"
        )

    def test_extract_seller_after_total(self):
        lines = [
            "叁佰贰拾壹圆整 ¥321.00",
            "重庆中油红綦能源发展有限公司",
            "91500222MA5XDW8PX6",
        ]
        name, tax = _extract_seller_after_total(lines)
        assert name == "重庆中油红綦能源发展有限公司"
        assert tax == "91500222MA5XDW8PX6"


class TestNonTaxReceipt:
    def _text(self) -> str:
        return (
            "中央非税收入统一票据（电子）\n"
            "票据代码:00010126 票据号码:0012090205\n"
            "交款人统一社会信用代码:91500106622015389N 校验码:2b5d4a\n"
            "交款人:重庆市旺成科技股份有限公司 开票日期:2026-06-29\n"
            "项目编码 项目名称 单位 数量 标准 金额（元） 备注\n"
            "056990124030发明专利第3年年费 元 0.15 900.00 135.00\n"
            "金额合计（大写）壹 佰叁拾伍元整 （小写）135.00\n"
            "申请号:2024103711884_缴费日期:2026-06-26_缴费方式:网上支付_订单号:202606269021113479\n"
            "其\n"
            "他\n"
            "信\n"
            "息\n"
            "收款单位（章）：国 家知识产权局专利局 复核人： 王舒 收款人：胡瀛\n"
        )

    def test_parse_central_non_tax_receipt(self):
        from fapiao_cn.parsers.non_tax_receipt import parse_central_non_tax_receipt

        rec = parse_central_non_tax_receipt(self._text(), {})
        assert rec.invoice_type == "中央非税收入统一票据（电子）"
        assert rec.invoice_number == "0012090205"
        assert rec.issue_date == "2026-06-29"
        assert rec.buyer_name == "重庆市旺成科技股份有限公司"
        assert rec.buyer_tax_id == "91500106622015389N"
        # "国 家知识产权局专利局" should merge whitespace.
        assert rec.seller_name == "国家知识产权局专利局"
        assert rec.seller_tax_id == ""
        assert rec.amount_without_tax == "135.00"
        assert rec.tax_amount == ""
        assert rec.total_amount == "135.00"
        assert rec.total_amount_upper == "壹佰叁拾伍元整"
        assert rec.payee == "胡瀛"
        assert rec.reviewer == "王舒"
        assert rec.drawer == ""

    def test_missing_seller_collapses_whitespace(self):
        from fapiao_cn.parsers.non_tax_receipt import _clean_seller_name

        assert _clean_seller_name("  国 家知识产权局专利局  ") == "国家知识产权局专利局"


class TestUtils:
    """Shared helpers (utils.py)."""

    def test_truncate_short_unchanged(self):
        from fapiao_cn.utils import truncate

        assert truncate("hello") == "hello"
        assert truncate("hello", limit=10) == "hello"

    def test_truncate_long_clipped(self):
        from fapiao_cn.utils import truncate

        long_s = "x" * 500
        out = truncate(long_s, limit=50)
        assert len(out) == 50
        assert out.endswith("…")

    def test_truncate_non_string(self):
        from fapiao_cn.utils import truncate

        assert truncate(Exception("a" * 1000), limit=30).endswith("…")


class TestCompanyNameKeywords:
    """H15/H40: expanded company-name keyword set + negative filtering."""

    @pytest.mark.parametrize(
        "name",
        [
            "重庆市旺成科技股份有限公司",
            "京东大药房重庆有限公司",
            "某某集团有限公司",
            "中国人民财产保险股份有限公司",
            "工商银行",
            "北京协和医院",
            "喜来登酒店",
        ],
    )
    def test_company_keywords_match(self, name):
        from fapiao_cn.utils import looks_like_company_name

        assert looks_like_company_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "信息部",
            "财务部",
            "营销部",
            "采购部",
            "数据中心",
            "客户服务中心",
        ],
    )
    def test_generic_units_rejected(self, name):
        from fapiao_cn.utils import looks_like_company_name

        assert not looks_like_company_name(name)


class TestInvoiceNumberAnchoring:
    """H25: invoice_no from 统一发票监制 line must anchor."""

    def test_anchored_no_match_across_lines(self):
        from fapiao_cn.parsers.invoice import _INVOICE_NO_EMBEDDED_RE

        text = "统一发票监制章\n93369231"
        # Without anchor, this could spuriously match.
        assert not _INVOICE_NO_EMBEDDED_RE.search(text)

    def test_anchored_match_on_same_line(self):
        from fapiao_cn.parsers.invoice import _INVOICE_NO_EMBEDDED_RE

        text = "统一发票监制章 933692318830000"
        m = _INVOICE_NO_EMBEDDED_RE.search(text)
        assert m is not None
        assert m.group(1) == "933692318830000"


class TestNameNegativePhrases:
    """H21: company names containing 名称含义/查重 should not be split."""

    def test_negative_phrase_kept_whole(self):
        from fapiao_cn.parsers.invoice import _extract_names_and_tax_ids

        text = (
            "名 称：深圳市名称含义信息技术有限公司\n"
            "统一社会信用代码/纳税人识别号：91440300MA5G12345X\n"
        )
        buyer_name, _, _, _ = _extract_names_and_tax_ids(text)
        assert buyer_name == "深圳市名称含义信息技术有限公司"


class TestAmountCnPicksActualTotal:
    """H26: 备注 row must not steal the total amount."""

    def test_actual_amount_wins_over_remark(self):
        from fapiao_cn.parsers.invoice import _extract_amounts

        # The 备注 row contains "壹角贰分" but the 价税合计 line
        # carries the proper "壹佰零壹圆壹角贰分".  We expect the long
        # one (or at least the one nearest the label).
        text = (
            "价税合计（大写）壹佰零壹圆壹角贰分 （小写）¥101.12\n"
            "备注：含税费壹角贰分"
        )
        _, _, _, total_cn = _extract_amounts(text)
        assert total_cn == "壹佰零壹圆壹角贰分"


class TestTrainTicketTaxIdLength:
    """H30: 铁路电子客票 tax id 长度扩展到 15-22 位。"""

    def test_short_tax_id_matches(self):
        from fapiao_cn.parsers.train_ticket import _BUYER_RE

        text = "购买方名称:张三 统一社会信用代码:91500000709375352T"
        m = _BUYER_RE.search(text)
        assert m is not None
        assert m.group(2) == "91500000709375352T"


class TestParserRegistry:
    """H7: /formats 描述必须与 parser 注册表同步。"""

    def test_format_descriptions_match_registry(self):
        from fapiao_cn.parsers import get_format_descriptions

        desc = get_format_descriptions()
        # All keys must be non-empty strings.
        for k, v in desc.items():
            assert isinstance(v, str) and v, k
        # All keys must be non-empty strings.
        for k, v in desc.items():
            assert isinstance(v, str) and v, k

    def test_default_parser_described(self):
        from fapiao_cn.parsers import get_format_descriptions

        desc = get_format_descriptions()
        assert "new" in desc
        assert "京东" in desc["new"] or "增值税" in desc["new"]


class TestXmlParser:
    """Unit tests for ``fapiao_cn.parsers.xml_invoice``."""

    def test_xml_items_extracted(self):
        """Every ``<IssuItemInformation>`` becomes one ``items[]`` entry."""
        from decimal import Decimal

        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        with open("samples/全电发票-XML/购_材料_01.xml", "rb") as f:
            rec = parse_xml_invoice(f.read())
        assert rec is not None
        assert len(rec.items) == 4
        # item_no is generated (XML carries no row number) and must be 1..N.
        assert [i.item_no for i in rec.items] == [1, 2, 3, 4]

        first = rec.items[0]
        assert first.name == "从动齿轮（精车坯）"
        assert first.category == "齿轮"  # split from the ``*齿轮*`` prefix
        assert first.spec == "23111-2SB-0033-00"
        assert first.unit == "件"
        assert first.quantity == "3335"
        assert first.unit_price == "19.447"
        assert first.amount == "64855.75"
        assert first.tax_rate == "0.13"
        assert first.tax_amount == "8431.25"
        assert first.total_amount == "73287.00"
        assert first.tax_classification_code == "1090124020000000000"
        assert first.is_reversal is False

        # The line items must reconcile with the header totals.
        assert sum(Decimal(i.amount or 0) for i in rec.items) == Decimal(
            rec.amount_without_tax
        )
        assert sum(Decimal(i.tax_amount or 0) for i in rec.items) == Decimal(
            rec.tax_amount
        )
        assert sum(Decimal(i.total_amount or 0) for i in rec.items) == Decimal(
            rec.total_amount
        )

    def test_xml_items_red_reversal_row(self):
        """红字冲销行 keeps its negative amounts and is flagged."""
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        with open("samples/全电发票-XML/购_材料_01.xml", "rb") as f:
            rec = parse_xml_invoice(f.read())
        reversal = [i for i in rec.items if i.is_reversal]
        assert len(reversal) == 1
        rev = reversal[0]
        assert rev.amount == "-2026.01"
        assert rev.tax_amount == "-263.38"
        assert rev.total_amount == "-2289.39"
        # The quantity/price quartet is absent on reversal rows and must not
        # drop the row.
        assert rev.quantity == "" and rev.unit_price == ""
        assert rev.tax_classification_code == "1090124020000000000"

    def test_xml_items_multi_page_invoice(self):
        """A 38-line invoice yields 38 items (cross-page accumulation)."""
        from decimal import Decimal

        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        with open("samples/全电发票-XML/销_产品_01.xml", "rb") as f:
            rec = parse_xml_invoice(f.read())
        assert len(rec.items) == 38
        assert sum(Decimal(i.amount or 0) for i in rec.items) == Decimal(
            rec.amount_without_tax
        )

    def test_xml_items_all_samples_reconcile(self):
        """Regression: every XML sample's items sum to the header amount.

        This is the guard against a *silently truncated* item list -- rows can
        be missing while each returned row still looks well-formed.
        """
        from decimal import Decimal
        from pathlib import Path

        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        sample_dir = Path("samples/全电发票-XML")
        files = sorted(sample_dir.glob("*.xml"))
        assert files, "no XML samples found"
        for path in files:
            rec = parse_xml_invoice(path.read_bytes())
            assert rec is not None, path.name
            assert rec.items, f"{path.name} produced no items"
            total = sum(Decimal(i.amount or 0) for i in rec.items)
            assert total == Decimal(rec.amount_without_tax), (
                f"{path.name}: items sum {total} != header {rec.amount_without_tax}"
            )

    def test_pdf_and_ofd_items_are_empty(self):
        """Only the XML parser fills items; PDF and OFD stay empty."""
        from fapiao_cn.formats import parse_pdf
        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        with open("samples/新电子发票-标准/购_材料_01.pdf", "rb") as f:
            rec = parse_pdf(f.read())
        assert rec is not None
        assert rec.items == []

        with open("samples/全电发票-OFD/烤匠电子发票_332.60元_龚健武.ofd", "rb") as f:
            rec = parse_ofd_invoice(f.read())
        assert rec is not None
        assert rec.items == [], "OFD line items are out of scope for now"

    def test_parse_xml_bytes(self):
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        with open("samples/全电发票-XML/购_材料_01.xml", "rb") as f:
            rec = parse_xml_invoice(f.read())
        assert rec is not None
        assert rec.invoice_type == "电子发票（增值税专用发票）"
        assert rec.invoice_number == "25502000000129241041"
        assert rec.seller_name == "重庆铜睿机械有限公司"
        assert rec.total_amount == "140141.09"

    def test_parse_xml_vat_special(self):
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        with open("samples/全电发票-XML/销_产品_01.xml", "rb") as f:
            rec = parse_xml_invoice(f.read())
        assert rec.invoice_type == "电子发票（增值税专用发票）"
        assert rec.buyer_name == "新大洲本田摩托（苏州）有限公司"
        assert rec.total_amount == "6034961.05"

    def test_parse_xml_common(self):
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        with open("samples/全电发票-XML/购_油品_01.xml", "rb") as f:
            rec = parse_xml_invoice(f.read())
        assert rec.invoice_type == "电子发票（普通发票）"
        assert rec.payee == ""
        assert rec.reviewer == ""
        assert rec.drawer == "邹玮"

    def test_parse_xml_page_count(self):
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        with open("samples/全电发票-XML/购_零售_01.xml", "rb") as f:
            rec = parse_xml_invoice(f.read())
        assert rec.page_count == 1

    def test_parse_xml_malformed(self):
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        assert parse_xml_invoice(b"<<not valid xml>>") is None
        assert parse_xml_invoice(b"") is None

    def test_parse_xml_missing_einvoice_data(self):
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        assert parse_xml_invoice(b"<Root><Other/></Root>") is None

    def test_parse_xml_xxe_external_entity_blocked(self):
        """H14: external entity declarations must not be expanded."""
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        xxe = (
            b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
            b'<EInvoice><EInvoiceData>'
            b'<SellerInformation><SellerName>&xxe;</SellerName></SellerInformation>'
            b'</EInvoiceData></EInvoice>'
        )
        # defusedxml raises DefusedXmlDtdsForbidden / EntitiesForbidden; we
        # swallow and return None.
        assert parse_xml_invoice(xxe) is None

    def test_parse_xml_billion_laughs_blocked(self):
        """H14: exponential entity expansion must be blocked."""
        from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

        bl = (
            b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;">]>\n'
            b'<EInvoice><EInvoiceData>'
            b'<SellerInformation><SellerName>&lol3;</SellerName></SellerInformation>'
            b'</EInvoiceData></EInvoice>'
        )
        assert parse_xml_invoice(bl) is None


class TestXmlFormatDate:
    def test_iso_date(self):
        from fapiao_cn.parsers.xml_invoice import _format_date

        assert _format_date("2025-11-21") == "2025-11-21"

    def test_iso_date_with_time(self):
        from fapiao_cn.parsers.xml_invoice import _format_date

        assert _format_date("2026-06-02 09:38:41") == "2026-06-02"

    def test_zero_padded_month(self):
        from fapiao_cn.parsers.xml_invoice import _format_date

        assert _format_date("2026-5-5") == "2026-05-05"

    def test_empty(self):
        from fapiao_cn.parsers.xml_invoice import _format_date

        assert _format_date("") == ""

    def test_invalid_format_unchanged(self):
        from fapiao_cn.parsers.xml_invoice import _format_date

        assert _format_date("not-a-date") == "not-a-date"


class TestXmlDetectInvoiceType:
    def test_special_vat(self):
        import xml.etree.ElementTree as ET

        from fapiao_cn.parsers.xml_invoice import _detect_invoice_type

        root = ET.fromstring("""<EInvoice>
          <Header><InherentLabel>
            <EInvoiceType><LabelName>电子发票</LabelName></EInvoiceType>
            <GeneralOrSpecialVAT><LabelName>增值税专用发票</LabelName></GeneralOrSpecialVAT>
          </InherentLabel></Header>
        </EInvoice>""")
        assert _detect_invoice_type(root) == "电子发票（增值税专用发票）"

    def test_common(self):
        import xml.etree.ElementTree as ET

        from fapiao_cn.parsers.xml_invoice import _detect_invoice_type

        root = ET.fromstring("""<EInvoice>
          <Header><InherentLabel>
            <EInvoiceType><LabelName>电子发票</LabelName></EInvoiceType>
            <GeneralOrSpecialVAT><LabelName>普通发票</LabelName></GeneralOrSpecialVAT>
          </InherentLabel></Header>
        </EInvoice>""")
        assert _detect_invoice_type(root) == "电子发票（普通发票）"

    def test_missing_inherent_label(self):
        import xml.etree.ElementTree as ET

        from fapiao_cn.parsers.xml_invoice import _detect_invoice_type

        root = ET.fromstring("<EInvoice><Header/></EInvoice>")
        assert _detect_invoice_type(root) == ""


class TestOfdParser:
    """Unit tests for ``fapiao_cn.parsers.ofd_invoice``."""

    def test_parse_ofd_vat_special(self):
        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        path = (
            "samples/全电发票-OFD/"
            "dzfp_25502000000126087334_重庆市旺成科技股份有限公司_20251204095208.ofd"
        )
        with open(path, "rb") as f:
            rec = parse_ofd_invoice(f.read())
        assert rec is not None
        # The CustomTag-driven path matches the paired XML invoice record.
        # ``invoice_type`` is recovered from the template-layer (Tpls/)
        # banner because the CustomTag dictionary doesn't carry it.
        assert rec.invoice_type == "电子发票（增值税专用发票）"
        assert rec.invoice_number == "25502000000126087334"
        assert rec.issue_date == "2025-11-21"
        assert rec.buyer_name == "新大洲本田摩托（苏州）有限公司"
        assert rec.buyer_tax_id == "91320585MA1T6M1R5F"
        assert rec.seller_name == "重庆市旺成科技股份有限公司"
        assert rec.seller_tax_id == "91500106622015389N"
        assert rec.amount_without_tax == "5340673.44"
        assert rec.tax_amount == "694287.61"
        assert rec.total_amount == "6034961.05"
        assert rec.drawer == "周妍伶"

    def test_parse_ofd_layout_fallback(self):
        """烤鱼店样张没有 CustomTag.xml;走布局兜底分支。"""
        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        with open("samples/全电发票-OFD/烤匠电子发票_332.60元_龚健武.ofd", "rb") as f:
            rec = parse_ofd_invoice(f.read())
        assert rec is not None
        assert rec.invoice_type == "电子发票（普通发票）"
        assert rec.invoice_number == "26507000000257250590"
        assert rec.issue_date == "2026-09-26"
        assert rec.buyer_name == "龚健武（个人）"
        assert rec.seller_name == "重庆福烤餐饮服务有限公司"
        assert rec.seller_tax_id == "91500103MADHB6TU2B"
        assert rec.amount_without_tax == "313.77"
        assert rec.tax_amount == "18.83"
        assert rec.total_amount == "332.60"
        assert rec.drawer == "苟艳"

    def test_parse_ofd_garbage_bytes(self):
        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        assert parse_ofd_invoice(b"") is None
        assert parse_ofd_invoice(b"<<not a zip>>") is None
        assert parse_ofd_invoice(b"PK\x03\x04not-a-real-zip") is None

    def test_parse_ofd_missing_document_xml(self):
        """A ZIP without ``Doc_0/Document.xml`` should be rejected cleanly."""
        import io
        import zipfile

        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", "no document here")
        assert parse_ofd_invoice(buf.getvalue()) is None

    def test_parse_ofd_zip_bomb_too_many_entries(self):
        """Defend against central-directory flooding."""
        import io
        import zipfile

        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i in range(70):
                z.writestr(f"file_{i}.txt", "x")
        assert parse_ofd_invoice(buf.getvalue()) is None

    def test_parse_ofd_huge_member_rejected(self):
        """A 12 MiB single member must be rejected before decompression."""
        import io
        import zipfile

        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        buf = io.BytesIO()
        # Stash 12 MiB of zeros so getinfo().file_size > _MAX_ENTRY_SIZE.
        huge = b"\0" * (12 * 1024 * 1024)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr("Doc_0/Document.xml", huge)
        assert parse_ofd_invoice(buf.getvalue()) is None

    def test_parse_ofd_no_invoice_signal(self):
        """An OFD-shaped archive with no TaxInfo/CustomTag falls back to
        layout scanning; if neither strategy finds an invoice number the
        record comes back with empty fields but still as a non-None
        InvoiceRecord (or None depending on the heuristic).  Both are
        acceptable; we just check it doesn't crash.
        """

        import io
        import zipfile

        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        # Build an OFD-shaped zip with garbage Content.xml; parser should
        # return None (no invoice signal).
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(
                "OFD.xml",
                '<?xml version="1.0"?><ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016"/>',
            )
            z.writestr(
                "Doc_0/Document.xml",
                '<?xml version="1.0"?>'
                '<ofd:Document xmlns:ofd="http://www.ofdspec.org/2016">'
                '<ofd:Pages><ofd:Page ID="3" BaseLoc="Pages/Page_0/Content.xml"/></ofd:Pages>'
                '<ofd:CommonData><ofd:MaxUnitID>1</ofd:MaxUnitID></ofd:CommonData>'
                '</ofd:Document>',
            )
            z.writestr(
                "Doc_0/Pages/Page_0/Content.xml",
                '<?xml version="1.0"?>'
                '<ofd:Page xmlns:ofd="http://www.ofdspec.org/2016"><ofd:Content/>'
                "</ofd:Page>",
            )
        rec = parse_ofd_invoice(buf.getvalue())
        # No invoice signal ⇒ either None or an empty record.  Both are
        # acceptable; we just don't want to crash.
        assert rec is None or rec.invoice_number == ""


@pytest.mark.skipif(
    not ee_feature_enabled("customs"),
    reason="企业版能力：需要安装 fapiao_cn_ee 并提供有效许可证",
)
class TestCustomsDeclaration:
    """Unit tests for ``fapiao_cn_ee.customs_declaration``."""

    OPERATIONAL_TEXT = """
中华人民共和国海关出口货物报关单 *222920260002109647*
预录入编号：222920260002109647 海关编号：222920260002109647
境内发货人 （91500106660851636U） 出境关别 (2225) 出口日期 申报日期 备案号
重庆旺成贸易有限公司 外港海关 20260530 20260527
境外收货人 （） 运输方式 (2) 运输工具名称及航次号 提运单号
PT TVS MOTOR COMPANY INDONESIA 水路运输 HOCHIMINH VOYAGER/2605S SNKO020260509798
生产销售单位（91500106660851636U） 监管方式 (0110) 征免性质 (101) 许可证号
重庆旺成贸易有限公司 一般贸易 一般征税
合同协议号 贸易国（地区）(IDN) 运抵国（地区）(IDN) 指运港 (IDN081) 离境口岸 (310701)
印度尼西亚 印度尼西亚 雅加达（印度尼西亚） 外高桥
包装种类 (23) 件数 毛重（千克） 净重（千克） 成交方式(3) 运费 保费 杂费
木制或竹藤等植物性材料制盒/箱 16 8649 7833 FOB
项号 商品编号 商品名称及规格型号 数量及单位 单价/总价/币制 原产国(地区) 最终目的国(地区) 境内货源地 征免
1 8483900090 离合器外罩 4333千克 5.71 中国 印度尼西亚 (50069)沙坪坝区 照章征税
1|1|安装于摩托车发动机内 28550 (CHN) (IDN) (1) 5000个 美元
2 8483409090 从动齿轮 5000个 5.42 中国 印度尼西亚 (50069)沙坪坝区 照章征税
1|1|安装于摩托车发动机内 3500千克 27100 (CHN) (IDN) (1) 5000个 美元
"""

    REFUND_TEXT = """
JG09
报关单证明第 1 次打印 出口退税联
主页 中华人民共和国海关出口货物报关单
预录入编号：E20260001753221591 海关编号：223120260001921706
出口口岸(2248) 备案号 出口日期 申报日期
洋山港区 20260513 20260512
境内收发货人(5006960544) 运输方式 运输工具名称 提运单号
重庆旺成贸易有限公司 水路运输 HYUNDAI FORWARD W237316692
生产销售单位 贸易方式(0110) 征免性质 结汇方式
重庆旺成贸易有限公司 一般贸易
许可证号 运抵国（地区）(502) 指运港（地区）(502) 境内货源地(50069)
美国 美国 沙坪坝区
批准文号 成交方式 运费 保费 杂费
FOB /0/ /0/ /0/
合同协议号 件数 包装种类 毛重（千克） 净重（千克）
WCTEAM26042901 3 其它 858.83 749.94
集装箱号 随附单据
YMLU5151410
标记唛码及备注
港:ST PAUL
商品序号 商品编号 商品名称、规格型号 数量/单位 数量及单位 目的国(地区) 单价 总价 币制 征免
1 8483109090 初级输出轴 法定数量/法定单位 210个 美国 8.97 1883.7 USD
0|0|将发动机的动力传 第二数量/第二单位 325.5千克 (502) 美元
递给车轮，使汽车产生
申报数量/申报单位 210个
2 8708409990 联接盘 法定数量/法定单位 270个 美国 3.31 893.7 USD
0|0|适用于POLARIS品 第二数量/第二单位 113.4千克 (502) 美元
申报数量/申报单位 270个
Page 1 of 1 打印日期：2026-05-26
"""

    NON_DECLARATION_TEXT = """
通关无纸化出口放行通知书
北京巡宇国际货运代理有限公司
海关编号：222920260002109647
出口日期 20260530
"""

    def _parse(self, text: str):
        from fapiao_cn_ee.customs_declaration import parse_customs_declaration

        return parse_customs_declaration(
            text,
            {"page_index": 0, "parser_name": "customs_declaration", "full_text": text},
        )

    def test_operational_variant(self):
        rec = self._parse(self.OPERATIONAL_TEXT)
        assert rec.declaration_variant == "操作联"
        assert rec.direction == "出口"
        assert rec.customs_no == "222920260002109647"
        assert rec.pre_entry_no == "222920260002109647"
        assert rec.shipper_name == "重庆旺成贸易有限公司"
        assert rec.shipper_tax_id == "91500106660851636U"
        assert rec.producer_name == "重庆旺成贸易有限公司"
        assert rec.producer_tax_id == "91500106660851636U"
        assert rec.customs_office_name == "外港海关"
        assert rec.customs_office_code == "2225"
        assert rec.supervision_mode_name == "一般贸易"
        assert rec.supervision_mode_code == "0110"
        assert rec.tax_exemption_name == "一般征税"
        assert rec.tax_exemption_code == "101"
        assert rec.export_date == "2026-05-30"
        assert rec.declaration_date == "2026-05-30"
        assert rec.bill_of_lading_no == "SNKO020260509798"

    def test_refund_variant(self):
        rec = self._parse(self.REFUND_TEXT)
        assert rec.declaration_variant == "出口退税联"
        assert rec.direction == "出口"
        assert rec.customs_no == "223120260001921706"
        assert rec.pre_entry_no == "E20260001753221591"
        assert rec.print_date == "2026-05-26"
        assert rec.shipper_name == "重庆旺成贸易有限公司"
        assert rec.shipper_tax_id == "5006960544"
        assert rec.producer_name == "重庆旺成贸易有限公司"
        assert rec.supervision_mode_code == "0110"
        assert rec.contract_no == "WCTEAM26042901"
        assert rec.packages_count == "3"
        assert rec.gross_weight_kg == "858.83"
        assert rec.net_weight_kg == "749.94"
        assert rec.container_no == "YMLU5151410"

    def test_items_extracted(self):
        rec = self._parse(self.REFUND_TEXT)
        assert rec.item_count == 2
        # Item 1 total price
        assert rec.items[0].hs_code == "8483109090"
        assert rec.items[0].total_price == "1883.7"
        assert rec.items[0].currency_name == "USD"
        assert rec.items[1].hs_code == "8708409990"
        assert rec.items[1].total_price == "893.7"
        # Cumulative total (USD 原值)
        assert rec.total_amount == "2777.40"
        assert rec.currency == "USD"

    def test_non_declaration_page_returns_empty(self):
        """Merged-document 放行条 pages must NOT pollute the record."""
        rec = self._parse(self.NON_DECLARATION_TEXT)
        assert rec.declaration_variant == ""
        assert rec.customs_no == ""
        assert rec.shipper_name == ""
        assert rec.total_amount == ""

    def test_release_notice_truncated_in_full_text(self):
        """When ``full_text`` spans a 报关单 + 放行条, item extraction
        must stop at the 放行条 boundary."""

        full_text = self.OPERATIONAL_TEXT + "\n" + self.NON_DECLARATION_TEXT
        from fapiao_cn_ee.customs_declaration import parse_customs_declaration

        rec = parse_customs_declaration(
            self.OPERATIONAL_TEXT,
            {"page_index": 0, "full_text": full_text, "page_count": 2},
        )
        # Items should come only from the 报关单 portion.
        assert all(
            "摩托车发动机" in (it.name_and_spec or "") or it.hs_code for it in rec.items
        )


class TestExpenseCategorySamples:
    """消费类别分类：基于 samples/ 目录下的真实样本断言 ``expense_category``。

    期望值与样本文件命名约定（``购_滴滴_01`` / ``购_餐饮_XX`` / ``购_顺丰_01`` /
    ...）以及实际卖方/明细语义同步制定。每个 case 都会跑一次完整解析，校验
    ``expense_category`` / ``expense_category_source`` 两个字段。
    """

    @pytest.mark.parametrize(
        "sample_path, expected_cat, expected_source",
        [
            # ── L1 票种命中 ──
            ("航空运输电子客票/航_运输_01.pdf", "transportation", "ticket"),
            ("航空运输电子客票/航_运输_02.pdf", "transportation", "ticket"),
            ("铁路电子客票/火车票01.pdf", "transportation", "ticket"),
            ("铁路电子客票/火车票02.pdf", "transportation", "ticket"),
            ("成品油合并代码/购_油品_01.pdf", "fuel", "ticket"),
            ("成品油合并代码/购_油品_03.pdf", "fuel", "ticket"),
            ("中央非税收入统一票据/专利_01.pdf", "other", "ticket"),
            # ── L2 卖方命中（PDF 没 items，靠 seller 命中） ──
            ("新电子发票-标准/购_滴滴_01.pdf", "transportation", "seller"),
            ("新电子发票-标准/购_滴滴_02.pdf", "transportation", "seller"),
            ("新电子发票-标准/购_顺丰_01.pdf", "courier", "seller"),
            ("新电子发票-标准/购_通信_01.pdf", "telecom", "seller"),
            ("新电子发票-标准/购_自来水_01.pdf", "water", "seller"),
            ("新电子发票-标准/购_油品_02.pdf", "fuel", "seller"),
            ("新电子发票-标准/购_餐饮_01.pdf", "dining", "seller"),
            ("新电子发票-标准/购_餐饮_02.pdf", "dining", "seller"),
            # ── L2 卖方命中（XML 也能从 seller 命中） ──
            ("全电发票-XML/购_油品_01.xml", "fuel", "seller"),
            ("全电发票-XML/购_油品_02.xml", "fuel", "seller"),
            # ── L3 明细命中（XML 有 items；明细里的 *餐饮服务* 触发 dining） ──
            ("全电发票-XML/购_零售_01.xml", "dining", "item"),
        ],
    )
    def test_sample_classified_correctly(self, sample_path, expected_cat, expected_source):
        from pathlib import Path

        from fapiao_cn.config import settings

        settings.expense_category_enabled = True
        path = Path("samples") / sample_path
        assert path.exists(), f"sample not found: {path}"
        contents = path.read_bytes()

        if path.suffix == ".xml":
            from fapiao_cn.parsers.xml_invoice import parse_xml_invoice

            rec = parse_xml_invoice(contents)
        else:
            from fapiao_cn.pipeline import run

            result = run(contents)
            rec = result.record

        assert rec is not None, f"parse failed: {sample_path}"
        assert rec.expense_category == expected_cat, (
            f"{sample_path}: expected {expected_cat}, got {rec.expense_category} "
            f"(seller={rec.seller_name!r}, items={[i.category for i in rec.items]!r})"
        )
        assert rec.expense_category_source == expected_source, (
            f"{sample_path}: expected source={expected_source}, got {rec.expense_category_source}"
        )
        # label 应当与 category 对应（CATEGORIES 表里的值）
        from fapiao_cn.expense_category import CATEGORIES

        if rec.expense_category:
            assert rec.expense_category_label == CATEGORIES[rec.expense_category], sample_path

    def test_disabled_flag_yields_empty_category(self):
        """FC_EXPENSE_CATEGORY_ENABLED=false 时三字段必须为空字符串。"""
        from pathlib import Path

        from fapiao_cn.config import settings
        from fapiao_cn.pipeline import run

        path = Path("samples/新电子发票-标准/购_滴滴_01.pdf")
        # try/finally: 断言失败时也必须把全局开关复位，否则会以 False
        # 泄漏到后续用例（test_pipeline.py 排在 test_parsers.py 之后）。
        settings.expense_category_enabled = False
        try:
            rec = run(path.read_bytes()).record
            assert rec.expense_category == ""
            assert rec.expense_category_label == ""
            assert rec.expense_category_source == ""
        finally:
            settings.expense_category_enabled = True

    def test_with_expense_category_method_returns_new_instance(self):
        """InvoiceRecord.with_expense_category() 返回新实例（不可变风格）。"""
        from fapiao_cn.schemas import InvoiceRecord

        base = InvoiceRecord(seller_name="滴滴出行", invoice_number="X")
        updated = base.with_expense_category(parser_name="new")
        assert base.expense_category == ""  # 原始不变
        assert updated.expense_category == "transportation"  # 新实例填充
        assert updated.invoice_number == "X"  # 其他字段保留

    def test_ofd_invoice_is_classified(self):
        """OFD 全电票也必须产出消费类别（此前只有 PDF / XML 两条路径分类）。

        卖方是「重庆福烤餐饮服务有限公司」→ 餐饮 / seller；OFD 与 PDF / XML
        的差异不应该体现在类目字段上。
        """
        from pathlib import Path

        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        path = Path("samples/全电发票-OFD/烤匠电子发票_332.60元_龚健武.ofd")
        if not path.exists():
            pytest.skip("OFD sample not available")
        rec = parse_ofd_invoice(path.read_bytes())
        assert rec is not None
        assert rec.seller_name == "重庆福烤餐饮服务有限公司"
        assert rec.expense_category == "dining"
        assert rec.expense_category_label == "餐饮"
        assert rec.expense_category_source == "seller"

    def test_ofd_classification_respects_disabled_flag(self):
        """总开关关闭时 OFD 的三字段同样必须为空字符串。"""
        from pathlib import Path

        from fapiao_cn.config import settings
        from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice

        path = Path("samples/全电发票-OFD/烤匠电子发票_332.60元_龚健武.ofd")
        if not path.exists():
            pytest.skip("OFD sample not available")
        settings.expense_category_enabled = False
        try:
            rec = parse_ofd_invoice(path.read_bytes())
            assert rec is not None
            assert rec.expense_category == ""
            assert rec.expense_category_label == ""
            assert rec.expense_category_source == ""
        finally:
            settings.expense_category_enabled = True

    @pytest.mark.parametrize(
        "sample_path",
        [
            "报关单/L20260528000009_21708433876_报关单放行条合并文档(1).pdf",
            "报关单/L20260602000010_0429.pdf",
            "报关单/L20260625000008_0522.pdf",
            "报关单/L20260626000009_BGD_SNKO020260509798.pdf",
            "报关单/L20260901000001_SHSL60355800_bgd.pdf",
        ],
    )
    def test_customs_declaration_skips_classification(self, sample_path):
        """报关单不参与消费类别分类：即便卖方偶然含「运输」/「餐饮」等关键词，
        也不能落到具体消费类目。

        开源版没有报关单解析器，报关单 PDF 会落到默认解析器 → 未命中任何
        规则 → ``unclassified``（"没归类"，不是"归到了某个类目"）；
        企业版走报关单解析器 → 字段全空 → 三字段空字符串。两者都满足
        "不被分到具体消费类目"。
        """
        from pathlib import Path

        from fapiao_cn.config import settings
        from fapiao_cn.pipeline import run

        settings.expense_category_enabled = True
        path = Path("samples") / sample_path
        assert path.exists(), f"sample not found: {path}"
        result = run(path.read_bytes())
        rec = result.record
        assert rec.expense_category in ("", "unclassified"), sample_path
        assert rec.expense_category_label in ("", "未归类"), sample_path
        assert rec.expense_category_source == "", sample_path

    def test_customs_declaration_skipped_even_if_seller_has_keywords(self):
        """报关单即使在 L2 关键词命中场景下也必须跳过分类。"""
        from fapiao_cn.schemas import InvoiceRecord

        rec = InvoiceRecord(
            seller_name="某滴滴出行科技有限公司",
            invoice_number="CUSTOMS_X",
            items=[],
        )
        updated = rec.with_expense_category(parser_name="customs_declaration")
        assert updated.expense_category == ""
        assert updated.expense_category_source == ""
