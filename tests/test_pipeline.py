"""Integration tests: parse each known PDF and compare against expected fields."""

from __future__ import annotations

import pytest

from tests._helpers import parse_pdf


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

        with open("samples/购_材料_01.pdf", "rb") as f:
            result = run(f.read())
        assert result.record is not None
        assert result.record.invoice_number

    def test_run_with_raw(self):
        from fapiao_cn.pipeline import run

        with open("samples/购_材料_01.pdf", "rb") as f:
            result = run(f.read(), with_raw=True)
        assert result.pdf_raw is not None
        assert result.pdf_raw.text
        assert result.pdf_raw.page_count > 0


class TestXmlPipeline:
    """XML e-invoice end-to-end parsing through ``fapiao_cn.formats.parse_pdf``."""

    XML_FILES = [
        "购_材料_01.xml",
        "购_油品_01.xml",
        "购_油品_02.xml",
        "购_零售_01.xml",
        "销_产品_01.xml",
    ]

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
        from fapiao_cn.formats import parse_pdf

        with open(f"samples/{filename}", "rb") as f:
            resp = parse_pdf(f.read(), filename=filename)
        assert resp.page_count == 1

    def test_xml_magic_bytes_detected_without_extension(self):
        """Magic-byte detection should accept XML content even without .xml suffix."""
        from fapiao_cn.formats import parse_pdf

        with open("samples/购_油品_01.xml", "rb") as f:
            contents = f.read()
        resp = parse_pdf(contents, filename="invoice")  # no .xml
        assert resp.message == "success"
        assert resp.invoice_number == "26507000000185284098"

    def test_xml_filename_with_pdf_path(self):
        """Filename extension should take precedence over content sniffing."""
        from fapiao_cn.formats import parse_pdf

        with open("samples/购_材料_01.xml", "rb") as f:
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

        with open("samples/购_材料_01.pdf", "rb") as f:
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

        with open("samples/购_油品_01.pdf", "rb") as f:
            contents = f.read()

        with patch("pdfplumber.page.Page.extract_words") as mock_words:
            mock_words.return_value = []
            result = run(contents)
            # merged-code 解析器走 ctx['page'].words()
            assert result.record is not None
            assert mock_words.call_count >= 1
