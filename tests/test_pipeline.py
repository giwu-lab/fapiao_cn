"""Integration tests: parse each known PDF and compare against expected fields."""

from __future__ import annotations

import pytest

from tests._helpers import ee_feature_enabled, parse_pdf


class TestKnownInvoices:
    """Parametrized over all PDFs in samples/ with known ground truth."""

    def test_expected_fields_match(self, filename: str, expected: dict):
        record = parse_pdf(filename)
        for field, expected_val in expected.items():
            actual = getattr(record, field)
            assert (
                actual == expected_val
            ), f"[{filename}] {field}: expected {expected_val!r}, got {actual!r}"

    def test_seller_name_does_not_contain_column_headers(self, filename: str, expected: dict):
        """Seller name must not leak table-column text."""
        record = parse_pdf(filename)
        assert "规格型号" not in record.seller_name

    def test_buyer_is_known_company(self, filename: str, expected: dict):
        if not expected.get("buyer_name"):
            pytest.skip("No expected buyer_name")
        record = parse_pdf(filename)
        assert record.buyer_name
        assert "售" not in record.buyer_name


@pytest.mark.skipif(
    not ee_feature_enabled("customs"),
    reason="企业版能力：需要安装 fapiao_cn_ee 并提供有效许可证",
)
class TestKnownCustomsDeclarations:
    """Parametrized over every 海关出口货物报关单 PDF in ``samples/报关单/``
    with stable expected fields."""

    def test_expected_fields_match(
        self, customs_filename: str, customs_expected: dict
    ):
        from tests._helpers import parse_customs_pdf

        rec = parse_customs_pdf(customs_filename)
        for field, expected_val in customs_expected.items():
            actual = getattr(rec, field)
            assert actual == expected_val, (
                f"[{customs_filename}] {field}: expected {expected_val!r}, got {actual!r}"
            )

    def test_customs_no_is_18_digits(
        self, customs_filename: str, customs_expected: dict
    ):
        from tests._helpers import parse_customs_pdf

        if not customs_expected.get("customs_no"):
            pytest.skip("No customs_no expected")
        rec = parse_customs_pdf(customs_filename)
        assert rec.customs_no.isdigit()
        assert len(rec.customs_no) == 18

    def test_items_have_hs_code(
        self, customs_filename: str, customs_expected: dict
    ):
        from tests._helpers import parse_customs_pdf

        if not customs_expected.get("item_count"):
            pytest.skip("No items expected")
        rec = parse_customs_pdf(customs_filename)
        for it in rec.items:
            assert it.hs_code and len(it.hs_code) == 10, (
                f"[{customs_filename}] bad hs_code: {it.hs_code!r}"
            )

    def test_release_notice_does_not_pollute(
        self, customs_filename: str, customs_expected: dict
    ):
        """Merged-document 报关单 + 放行条 pages parse the 报关单 page
        only — the 放行条 page must not contribute declarations to the
        output."""
        from tests._helpers import parse_customs_pdf

        rec = parse_customs_pdf(customs_filename)
        # ``customs_no`` must always be present (not '').
        assert rec.customs_no, f"{customs_filename}: empty customs_no"
        # The 报关单 title is on the parsed page only.
        assert rec.declaration_variant in ("操作联", "出口退税联")


@pytest.mark.skipif(
    not ee_feature_enabled("customs"),
    reason="企业版能力：需要安装 fapiao_cn_ee 并提供有效许可证",
)
class TestCustomsItemFieldsFromSamples:
    """真实样本的明细字段断言 —— 每一条都对着票面逐列核对过。"""

    def _items(self, name: str):
        from tests._helpers import parse_customs_pdf

        rec = parse_customs_pdf(f"报关单/{name}")
        assert rec.items, name
        return rec.items

    def test_refund_item_fields(self):
        """出口退税联：标签列 + 目的国 + 币制。"""
        items = self._items("L20260602000010_0429.pdf")
        it = items[0]
        assert it.item_no == "1"
        assert it.hs_code == "8483109090"
        assert it.name_and_spec == "初级输出轴|将发动机的动力传递给车轮，使汽车产生驱动力|无"
        assert it.legal_qty == "210个"
        assert it.second_qty == "325.5千克"
        assert it.declared_qty == "210个"
        assert it.unit_price == "8.97"
        assert it.total_price == "1883.7"
        assert it.currency_code == "USD"
        assert it.currency_name == "美元"
        assert it.final_destination_code == "502"
        assert it.final_destination_name == "美国"

    def test_operational_item_fields(self):
        """操作联：原产国 / 最终目的国 / 境内货源地 / 征免。"""
        items = self._items("L20260626000009_BGD_SNKO020260509798.pdf")
        it = items[0]
        assert it.hs_code == "8483900090"
        assert it.name_and_spec.startswith("离合器外罩|")
        assert it.legal_qty == "4333千克"
        assert it.declared_qty == "5000个"  # 5.71 × 5000 = 28550
        assert it.unit_price == "5.71"
        assert it.total_price == "28550"
        assert it.currency_name == "美元"
        assert it.origin_country_code == "CHN"
        assert it.origin_country_name == "中国"
        assert it.final_destination_code == "IDN"
        assert it.final_destination_name == "印度尼西亚"
        assert it.source_region_code == "50069"
        assert it.source_region_name == "沙坪坝区"
        assert it.tax_status == "照章征税"

    def test_merged_document_item_fields(self):
        """报关单 + 放行条合并件：明细只来自报关单那一页。"""
        items = self._items("L20260528000009_21708433876_报关单放行条合并文档(1).pdf")
        assert len(items) == 1
        it = items[0]
        assert it.hs_code == "7318151001"
        assert it.name_and_spec == "圆柱螺栓|钢|1177-1324Mpa兆帕|无品牌|无型号|6.4±0.1mm"
        assert it.legal_qty == "236.8千克"
        assert it.declared_qty == "7400个"  # 0.2626 × 7400 = 1943.24
        assert it.total_price == "1943.24"

    def test_no_item_swallows_form_footer(self):
        """最后一条明细不能把表尾（签字 / 审单 / 打印日期）吞进规格型号。"""
        for name in (
            "L20260602000010_0429.pdf",
            "L20260625000008_0522.pdf",
            "L20260626000009_BGD_SNKO020260509798.pdf",
            "L20260901000001_SHSL60355800_bgd.pdf",
        ):
            for it in self._items(name):
                for bad in ("Page ", "打印日期", "税费征收情况", "录入员", "报关人员"):
                    assert bad not in it.name_and_spec, (name, bad, it.name_and_spec)

    def test_item_price_times_qty_matches_total(self):
        """成交数量 × 单价 必须等于总价（数量归位错了就会对不上）。"""
        for name in (
            "L20260602000010_0429.pdf",
            "L20260625000008_0522.pdf",
            "L20260626000009_BGD_SNKO020260509798.pdf",
            "L20260901000001_SHSL60355800_bgd.pdf",
        ):
            for it in self._items(name):
                if not (it.unit_price and it.total_price and it.declared_qty):
                    continue
                digits = "".join(ch for ch in it.declared_qty if ch.isdigit() or ch == ".")
                if not digits:
                    continue
                price = float(it.unit_price)
                total = float(it.total_price)
                assert abs(price * float(digits) - total) <= max(0.01, total * 0.001), (
                    f"{name} item {it.item_no}: {price} × {digits} ≠ {total}"
                )


class TestPlatformLayoutRouting:
    """平台下载版（票面标签块在上、值块在下）走 platform 解析器。

    其它票面必须保持原样 —— 版式识别靠结构特征，不能误伤。
    """

    # 同一个平台模板会出现在不同目录里（带 / 不带「统一发票监制章」），
    # 所以这里用显式白名单，而不是按目录判断。
    PLATFORM_SAMPLES = {
        "新电子发票-平台下载/志诚_26322000008364632971.pdf",
        "新电子发票-平台下载/个人代开_01.pdf",
        "新电子发票-平台下载/购_自来水_01.pdf",
    }

    def test_only_platform_samples_use_platform_parser(self):
        import glob
        from pathlib import Path

        import pdfplumber

        from fapiao_cn.parsers import dispatch
        from fapiao_cn.pipeline import _clean_text
        from tests._helpers import TEST_DIR

        if not TEST_DIR.exists():
            pytest.skip("samples/ not available")

        files = sorted(glob.glob(str(TEST_DIR / "**" / "*.pdf"), recursive=True))
        assert files, "samples/ is empty"
        seen = set()
        for path in files:
            with pdfplumber.open(path) as pdf:
                text = _clean_text(pdf.pages[0].extract_text() or "")
            name, _ = dispatch(text)
            rel = Path(path).relative_to(TEST_DIR).as_posix()
            if rel in self.PLATFORM_SAMPLES:
                assert name == "platform", rel
                seen.add(rel)
            else:
                assert name != "platform", rel
        assert seen == self.PLATFORM_SAMPLES


class TestEdgeCases:
    def test_empty_text(self):
        from fapiao_cn.parsers.invoice import parse_new_electronic_invoice

        rec = parse_new_electronic_invoice("", {"page_index": 0, "parser_name": "new"})
        assert rec.invoice_type == ""
        assert rec.invoice_number == ""
        assert rec.buyer_name == ""

    def test_text_without_invoice(self):
        from fapiao_cn.parsers.invoice import parse_new_electronic_invoice

        rec = parse_new_electronic_invoice(
            "some random text without invoice fields",
            {"page_index": 0, "parser_name": "new"},
        )
        assert rec.invoice_type == ""


class TestPipelineRun:
    def test_run_returns_parse_result(self):
        from fapiao_cn.pipeline import run

        with open("samples/新电子发票-标准/购_材料_01.pdf", "rb") as f:
            result = run(f.read())
        assert result.record is not None
        assert result.record.invoice_number

    def test_run_with_raw(self):
        from fapiao_cn.pipeline import run

        with open("samples/新电子发票-标准/购_材料_01.pdf", "rb") as f:
            result = run(f.read(), with_raw=True)
        assert result.pdf_raw is not None
        assert result.pdf_raw.text
        assert result.pdf_raw.page_count > 0


class TestXmlPipeline:
    """XML e-invoice end-to-end parsing through ``fapiao_cn.formats.parse_pdf``."""

    XML_FILES = [
        "全电发票-XML/购_材料_01.xml",
        "全电发票-XML/购_油品_01.xml",
        "全电发票-XML/购_油品_02.xml",
        "全电发票-XML/购_零售_01.xml",
        "全电发票-XML/销_产品_01.xml",
    ]


class TestOfdPipeline:
    """OFD 数电票 end-to-end parsing through ``fapiao_cn.formats.parse_pdf``."""

    OFD_FILES = [
        "全电发票-OFD/dzfp_25502000000126087334_重庆市旺成科技股份有限公司_20251204095208.ofd",
        "全电发票-OFD/烤匠电子发票_332.60元_龚健武.ofd",
    ]

    def test_ofd_files_have_expected_fields(self, filename: str, expected: dict):
        from fapiao_cn.formats import parse_pdf

        with open(f"samples/{filename}", "rb") as f:
            resp = parse_pdf(f.read(), filename=filename)
        assert resp.message == "success", f"{filename}: {resp.message}"
        for field, expected_val in expected.items():
            actual = getattr(resp, field)
            assert actual == expected_val, (
                f"[{filename}] {field}: expected {expected_val!r}, got {actual!r}"
            )

    @pytest.mark.parametrize(
        "filename,expected_pages",
        [
            ("全电发票-OFD/烤匠电子发票_332.60元_龚健武.ofd", 1),
            (
                "全电发票-OFD/dzfp_25502000000126087334_重庆市旺成科技股份有限公司_20251204095208.ofd",
                2,
            ),
        ],
    )
    def test_ofd_page_count_matches_document(
        self, filename: str, expected_pages: int
    ):
        from fapiao_cn.formats import parse_pdf

        with open(f"samples/{filename}", "rb") as f:
            resp = parse_pdf(f.read(), filename=filename)
        assert resp.page_count == expected_pages

    def test_xml_files_have_expected_fields(self, filename: str, expected: dict):
        from fapiao_cn.formats import parse_pdf

        with open(f"samples/{filename}", "rb") as f:
            resp = parse_pdf(f.read(), filename=filename)
        assert resp.message == "success", f"{filename}: {resp.message}"
        for field, expected_val in expected.items():
            actual = getattr(resp, field)
            assert actual == expected_val, (
                f"[{filename}] {field}: expected {expected_val!r}, got {actual!r}"
            )

    def test_xml_page_count_is_one(self, filename: str, expected: dict):
        """XML 数电票是逻辑单页;PDF/OFD 各自有专属测试,这里跳过."""
        from fapiao_cn.formats import parse_pdf

        if not filename.endswith(".xml"):
            pytest.skip("PDF/OFD page counts vary; see dedicated tests")
        with open(f"samples/{filename}", "rb") as f:
            resp = parse_pdf(f.read(), filename=filename)
        assert resp.page_count == 1

    def test_xml_magic_bytes_detected_without_extension(self):
        """Magic-byte detection should accept XML content even without .xml suffix."""
        from fapiao_cn.formats import parse_pdf

        with open("samples/全电发票-XML/购_油品_01.xml", "rb") as f:
            contents = f.read()
        resp = parse_pdf(contents, filename="invoice")  # no .xml
        assert resp.message == "success"
        assert resp.invoice_number == "26507000000185284098"

    def test_xml_filename_with_pdf_path(self):
        """Filename extension should take precedence over content sniffing."""
        from fapiao_cn.formats import parse_pdf

        with open("samples/全电发票-XML/购_材料_01.xml", "rb") as f:
            contents = f.read()
        resp = parse_pdf(contents, filename="some.pdf")
        # filename says .pdf but content is XML — content wins (magic bytes).
        assert resp.message == "success"
        assert resp.invoice_number == "25502000000129241041"

    def test_xml_malformed_returns_error(self):
        from fapiao_cn.formats import parse_pdf

        resp = parse_pdf(b"<<not valid xml>>", filename="bad.xml")
        assert resp.message.startswith("error:")
        assert "xml_parse_failed" in resp.message

    def test_xml_empty_file_error(self):
        from fapiao_cn.formats import parse_pdf

        resp = parse_pdf(b"", filename="empty.xml")
        assert resp.message == "error: empty_file"


class TestLazyWords:
    """D3: ``PageView.words()`` must defer ``extract_words()`` to the parser."""

    def test_page_view_words_is_lazy(self):
        from unittest.mock import MagicMock

        from fapiao_cn.pipeline import PageView

        mock_page = MagicMock()
        mock_page.extract_words.return_value = [
            {"text": "购", "x0": 0, "top": 0, "x1": 10, "bottom": 12},
            {"text": "买", "x0": 10, "top": 0, "x1": 20, "bottom": 12},
        ]

        view = PageView(mock_page, "购买方")

        # extract_words() must NOT have been called yet
        mock_page.extract_words.assert_not_called()

        words = view.words()
        mock_page.extract_words.assert_called_once()
        assert len(words) == 2

        # Subsequent calls return the cached list (no extra extraction)
        again = view.words()
        mock_page.extract_words.assert_called_once()
        assert again is words

    def test_pipeline_only_extracts_words_when_needed(self):
        """For an invoice whose parser does not touch .words(), the
        underlying ``page.extract_words()`` is never invoked."""
        from unittest.mock import patch

        from fapiao_cn.pipeline import run

        with open("samples/新电子发票-标准/购_材料_01.pdf", "rb") as f:
            contents = f.read()

        with patch("pdfplumber.page.Page.extract_words") as mock_words:
            result = run(contents)
            assert result.record.invoice_number
            # 新发票解析器完全不用 words，extract_words 应该 0 次
            assert mock_words.call_count == 0

    def test_merged_code_parser_extracts_words(self):
        """The merged-code parser DOES need coordinates and must
        trigger ``extract_words()`` on each page."""
        from unittest.mock import patch

        from fapiao_cn.pipeline import run

        with open("samples/成品油合并代码/购_油品_01.pdf", "rb") as f:
            contents = f.read()

        with patch("pdfplumber.page.Page.extract_words") as mock_words:
            mock_words.return_value = []
            result = run(contents)
            # merged-code 解析器走 ctx['page'].words()
            assert result.record is not None
            assert mock_words.call_count >= 1
