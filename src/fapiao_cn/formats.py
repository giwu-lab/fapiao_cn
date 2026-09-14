"""Unified parse entry point.

Auto-detects XML vs PDF by filename extension and magic bytes, then
dispatches to the appropriate parser.  All outputs share the same
``ExtractResponse`` shape regardless of the underlying format.

When ``Settings.cache_size > 0`` (the default) the function consults an
LRU cache keyed on ``sha256(contents) + filename + with_raw``.  Repeat
uploads of the same file return the cached response instantly.
"""

from __future__ import annotations

from typing import Optional

from pdfplumber.utils.exceptions import PdfminerException

from fapiao_cn.cache import get_cache
from fapiao_cn.config import settings
from fapiao_cn.parsers.xml_invoice import parse_xml_invoice
from fapiao_cn.pipeline import run
from fapiao_cn.schemas import ExtractResponse, InvoiceRecord


def _detect_format(contents: bytes, filename: str) -> str:
    """Identify document format: ``"xml"`` or ``"pdf"`` (default)."""

    if filename and filename.lower().endswith(".xml"):
        return "xml"
    if contents[:5] == b"<?xml":
        return "xml"
    return "pdf"


def _parse_xml(contents: bytes, filename: str) -> ExtractResponse:
    record: Optional[InvoiceRecord] = parse_xml_invoice(contents)
    if record is None:
        return ExtractResponse(
            file=filename,
            error="xml_parse_failed: malformed or unsupported XML",
        )
    return ExtractResponse(
        file=filename,
        page_count=record.page_count,
        invoice_type=record.invoice_type,
        invoice_number=record.invoice_number,
        issue_date=record.issue_date,
        buyer_name=record.buyer_name,
        buyer_tax_id=record.buyer_tax_id,
        seller_name=record.seller_name,
        seller_tax_id=record.seller_tax_id,
        amount_without_tax=record.amount_without_tax,
        tax_amount=record.tax_amount,
        total_amount=record.total_amount,
        total_amount_upper=record.total_amount_upper,
        payee=record.payee,
        reviewer=record.reviewer,
        drawer=record.drawer,
    )


def _build_response(record: InvoiceRecord, filename: str, pdf_raw, warnings) -> ExtractResponse:
    return ExtractResponse(
        file=filename,
        page_count=record.page_count,
        invoice_type=record.invoice_type,
        invoice_number=record.invoice_number,
        issue_date=record.issue_date,
        buyer_name=record.buyer_name,
        buyer_tax_id=record.buyer_tax_id,
        seller_name=record.seller_name,
        seller_tax_id=record.seller_tax_id,
        amount_without_tax=record.amount_without_tax,
        tax_amount=record.tax_amount,
        total_amount=record.total_amount,
        total_amount_upper=record.total_amount_upper,
        payee=record.payee,
        reviewer=record.reviewer,
        drawer=record.drawer,
        pdf_raw=pdf_raw,
        warnings=warnings,
    )


def parse_pdf(contents: bytes, filename: str = "", with_raw: bool = False) -> ExtractResponse:
    if not contents:
        return ExtractResponse(file=filename, error="empty_file")
    max_bytes = settings.max_file_mb * 1024 * 1024
    if len(contents) > max_bytes:
        return ExtractResponse(
            file=filename,
            error=f"file_too_large: {len(contents)} > {max_bytes} bytes",
        )

    # Cache lookup (LRU; honours settings.cache_size).
    cache = get_cache()
    cached = cache.get(contents, filename, with_raw)
    if cached is not None:
        return cached  # type: ignore[return-value]

    if _detect_format(contents, filename) == "xml":
        resp = _parse_xml(contents, filename)
    else:
        try:
            result = run(contents, with_raw=with_raw)
        except (ValueError, KeyError, AttributeError, PdfminerException) as exc:
            resp = ExtractResponse(
                file=filename,
                error=f"parse_failed: {exc}",
            )
        else:
            resp = _build_response(result.record, filename, result.pdf_raw, result.warnings)

    # Only cache successful responses — error responses are not stable.
    if not resp.error:
        cache.put(contents, filename, with_raw, resp)
    return resp