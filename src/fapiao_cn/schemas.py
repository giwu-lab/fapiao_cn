"""Pydantic schemas for the einvoice API.

Field names form the stable wire contract across PDF / XML / batch
inputs; downstream consumers parse one shape regardless of source.
"""

from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, Field

# Pattern used to normalise whitespace inside extracted field values.
_WHITESPACE_RE = re.compile(r"\s+")


class InvoiceRecord(BaseModel):
    file: str = ""
    page_count: int = 0
    invoice_type: str = ""
    invoice_number: str = ""
    issue_date: str = ""
    buyer_name: str = ""
    buyer_tax_id: str = ""
    seller_name: str = ""
    seller_tax_id: str = ""
    amount_without_tax: str = ""
    tax_amount: str = ""
    total_amount: str = ""
    total_amount_upper: str = ""
    payee: str = ""
    reviewer: str = ""
    drawer: str = ""

    def stripped(self) -> "InvoiceRecord":
        """Return a copy with all whitespace removed from string fields."""

        # ``page_count`` is an int and must round-trip verbatim; the
        # rest are strings that get their whitespace stripped.
        data = self.model_dump()
        for k, v in data.items():
            if isinstance(v, str):
                data[k] = _WHITESPACE_RE.sub("", v)
        return InvoiceRecord(**data)


class PdfRaw(BaseModel):
    text: str = ""
    lines: List[str] = Field(default_factory=list)
    page_count: int = 0


class ExtractResponse(BaseModel):
    file: str = ""
    page_count: int = 0
    invoice_type: str = ""
    invoice_number: str = ""
    issue_date: str = ""
    buyer_name: str = ""
    buyer_tax_id: str = ""
    seller_name: str = ""
    seller_tax_id: str = ""
    amount_without_tax: str = ""
    tax_amount: str = ""
    total_amount: str = ""
    total_amount_upper: str = ""
    payee: str = ""
    reviewer: str = ""
    drawer: str = ""
    pdf_raw: Optional[PdfRaw] = None
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None
