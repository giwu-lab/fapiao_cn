"""fapiao_cn — electronic invoice field extractor (PDF + XML).

Public API:
    parse_pdf(contents, filename="") -> ExtractResponse
    InvoiceRecord, PdfRaw, ExtractResponse        — data models
    Settings, settings                            — configuration

The CLI surface lives in :mod:`fapiao_cn.cli` (console script ``fapiao_cn``) and
:mod:`fapiao_cn.server` (console script ``fapiao_cn-server``).
"""

from __future__ import annotations

from .config import Settings, settings
from .formats import parse_pdf
from .schemas import ExtractResponse, InvoiceRecord, PdfRaw

__all__ = [
    "parse_pdf",
    "InvoiceRecord",
    "PdfRaw",
    "ExtractResponse",
    "Settings",
    "settings",
]