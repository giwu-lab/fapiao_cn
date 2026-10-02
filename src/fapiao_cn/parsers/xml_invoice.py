"""XML e-invoice parser.

Parses fully-digitalised (全电发票) XML invoices following the
``EInvoice`` schema.  Field names mirror the existing ``InvoiceRecord``
contract so downstream consumers can treat PDF and XML outputs
identically.

Notes:

* Output is a typed ``InvoiceRecord`` rather than a loose ``dict``.
* ``page_count`` is set to 1 (XML invoices are a single logical page).
* The returned record is passed through ``.stripped()`` for whitespace
  consistency with the PDF pipeline.
* XML parsing goes through ``defusedxml`` to neutralise XXE /
  billion-laughs style attacks (H14).
"""

from __future__ import annotations

from typing import Optional, Union

import defusedxml.ElementTree as DET

from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import normalize_issue_date


def parse_xml_invoice(xml_input: Union[bytes, str]) -> Optional[InvoiceRecord]:
    """Parse an XML e-invoice and return an ``InvoiceRecord``.

    ``xml_input`` may be:
    * ``bytes``  -- raw XML content
    * ``str``    -- either raw XML text (starting with ``<`` / ``<?xml``)
                    or a filesystem path

    Returns ``None`` if the document is not a well-formed ``EInvoice``
    or contains a malicious DOCTYPE / entity declaration.
    """

    try:
        if isinstance(xml_input, bytes):
            root = DET.fromstring(xml_input.decode("utf-8"))
        elif isinstance(xml_input, str):
            if xml_input.lstrip().startswith("<"):
                root = DET.fromstring(xml_input)
            else:
                root = DET.parse(xml_input).getroot()
        else:
            return None
    except Exception:
        return None

    return extract_invoice_from_root(root)


def _text(parent, *tags: str) -> str:
    """Return the first non-empty text of ``tags`` searched under ``parent``."""

    if parent is None:
        return ""
    for tag in tags:
        elem = parent.find(tag)
        if elem is not None and elem.text:
            t = elem.text.strip()
            if t:
                return t
    return ""


def _label_name(parent, *tags: str) -> str:
    """Read nested ``<Tag><LabelName>...</LabelName></Tag>`` structure."""

    if parent is None:
        return ""
    for tag in tags:
        elem = parent.find(f"{tag}/LabelName")
        if elem is not None and elem.text:
            t = elem.text.strip()
            if t:
                return t
    return ""


def _detect_invoice_type(root: DET.Element) -> str:
    """Derive ``invoice_type`` from ``Header/InherentLabel`` LabelName tags.

    Combines ``EInvoiceType/LabelName`` (typically "电子发票") with
    ``GeneralOrSpecialVAT/LabelName`` ("普通发票" or "增值税专用发票")
    into ``"电子发票（{vat}）"``.
    """

    label = root.find(".//Header/InherentLabel")
    if label is None:
        return ""
    e_type = _label_name(label, "EInvoiceType")
    vat_type = _label_name(label, "GeneralOrSpecialVAT")
    if not e_type:
        return ""
    if vat_type and vat_type != "电子发票":
        return f"{e_type}（{vat_type}）"
    return e_type


def _format_date(date_str: str) -> str:
    """Normalise a date string to ISO ``YYYY-MM-DD``.

    Accepts ``YYYY-M-D`` (with optional trailing time, e.g.
    ``2026-06-02 09:38:41``).  Returns the raw value when no date
    pattern is recognised so callers can decide what to do.
    """

    if not date_str:
        return ""
    return normalize_issue_date(date_str)


def extract_invoice_from_root(root: DET.Element) -> Optional[InvoiceRecord]:
    """Extract ``InvoiceRecord`` fields from a parsed ``EInvoice`` XML root.

    Public entry point so the OFD parser (which unpacks an OFD zip and
    reads the embedded ``CustomTag.xml`` as a normal XML doc) can reuse
    the same field-extraction logic.  Both standalone XML 数电发票 and
    the XML embedded inside OFD share the same ``EInvoice`` schema per
    国税总局 数电发票 spec; H43 ("XML schema sync responsibility")
    applies here as well.
    """

    eid = root.find("EInvoiceData")
    if eid is None:
        return None

    seller_info = eid.find("SellerInformation")
    buyer_info = eid.find("BuyerInformation")
    basic_info = eid.find("BasicInformation")
    tax_super = root.find("TaxSupervisionInfo")

    invoice_number = _text(tax_super, "InvoiceNumber") or _text(root, "Header/EIid")
    date_raw = _text(tax_super, "IssueTime") or _text(basic_info, "RequestTime")
    issue_date = _format_date(date_raw)

    invoice_type = _detect_invoice_type(root)

    return InvoiceRecord(
        file="",
        page_count=1,
        invoice_type=invoice_type,
        invoice_number=invoice_number,
        issue_date=issue_date,
        buyer_name=_text(buyer_info, "BuyerName"),
        buyer_tax_id=_text(buyer_info, "BuyerIdNum"),
        seller_name=_text(seller_info, "SellerName"),
        seller_tax_id=_text(seller_info, "SellerIdNum"),
        amount_without_tax=_text(basic_info, "TotalAmWithoutTax"),
        tax_amount=_text(basic_info, "TotalTaxAm"),
        total_amount=_text(basic_info, "TotalTax-includedAmount"),
        total_amount_upper=_text(basic_info, "TotalTax-includedAmountInChinese"),
        payee=_text(basic_info, "Payee"),
        reviewer=_text(basic_info, "Reviewer"),
        drawer=_text(basic_info, "Drawer"),
    ).stripped()
