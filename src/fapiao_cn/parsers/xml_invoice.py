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

import logging
import re
from decimal import Decimal
from typing import List, Optional, Union

import defusedxml.ElementTree as DET

from fapiao_cn.schemas import InvoiceItem, InvoiceRecord
from fapiao_cn.utils import normalize_issue_date

log = logging.getLogger("fapiao_cn.xml_invoice")


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


# ``ItemName`` is written as ``*类别*品名`` in every 数电票 sample (47/47),
# e.g. ``*齿轮*从动齿轮（精车坯）``.  Split the leading category so callers
# do not have to re-parse the asterisk-delimited prefix.
_ITEM_NAME_RE = re.compile(r"^\*([^*]+)\*(.*)$", re.S)

# ``InvoiceItem`` field -> ``EInvoice`` schema element name.
_ITEM_FIELD_TAGS = {
    "name": "ItemName",
    "spec": "SpecMod",
    "unit": "MeaUnits",
    "quantity": "Quantity",
    "unit_price": "UnPrice",
    "amount": "Amount",
    "tax_rate": "TaxRate",
    "tax_amount": "ComTaxAm",
    "total_amount": "TotaltaxIncludedAmount",
    "tax_classification_code": "TaxClassificationCode",
}


def _is_negative(value: str) -> bool:
    """True when a numeric string carries a leading minus sign (红字冲销行)."""

    return bool(value) and value.lstrip().startswith("-")


def _parse_items(eid: DET.Element) -> List[InvoiceItem]:
    """Extract the ``<IssuItemInformation>`` line items under ``EInvoiceData``.

    Every 数电票 writer emits one ``<IssuItemInformation>`` per line, repeated
    as siblings.  All fields except the quantity/price quartet
    (``SpecMod``/``MeaUnits``/``Quantity``/``UnPrice``) are present on every
    row; the quartet is absent on 红字冲销行, which carries only a negative
    amount triple.  Missing tags therefore yield ``""`` rather than a row
    being skipped — the row still has to be reported so the amounts reconcile.
    """

    items: List[InvoiceItem] = []
    for idx, elem in enumerate(eid.findall("IssuItemInformation"), start=1):
        values = {
            field: _text(elem, tag) for field, tag in _ITEM_FIELD_TAGS.items()
        }
        raw_name = values["name"]
        category = ""
        name = raw_name
        m = _ITEM_NAME_RE.match(raw_name)
        if m:
            category, name = m.group(1).strip(), m.group(2).strip()
        items.append(
            InvoiceItem(
                item_no=idx,
                name=name,
                category=category,
                spec=values["spec"],
                unit=values["unit"],
                quantity=values["quantity"],
                unit_price=values["unit_price"],
                amount=values["amount"],
                tax_rate=values["tax_rate"],
                tax_amount=values["tax_amount"],
                total_amount=values["total_amount"],
                tax_classification_code=values["tax_classification_code"],
                is_reversal=_is_negative(values["amount"]),
            )
        )
    return items


def _check_items_reconcile(items: List[InvoiceItem], basic_info: DET.Element) -> None:
    """Warn when the line items do not sum to the header totals.

    This is the only signal that catches a *silently truncated* item list —
    every extracted row can look perfectly well-formed while rows are
    missing, so consumers must not have to re-derive the check themselves.
    """

    if not items:
        return

    def _total(field: str) -> Decimal:
        acc = Decimal(0)
        for it in items:
            try:
                acc += Decimal(getattr(it, field) or 0)
            except (ArithmeticError, ValueError):
                continue
        return acc

    for item_field, header_tag in (
        ("amount", "TotalAmWithoutTax"),
        ("tax_amount", "TotalTaxAm"),
        ("total_amount", "TotalTax-includedAmount"),
    ):
        header = _text(basic_info, header_tag)
        if not header:
            continue
        try:
            diff = _total(item_field) - Decimal(header)
        except (ArithmeticError, ValueError):
            continue
        if abs(diff) >= Decimal("0.01"):
            log.warning(
                "XML invoice line items do not reconcile with header: "
                "field=%s items_sum=%s header=%s diff=%s rows=%d "
                "-- the item list is likely incomplete",
                item_field, _total(item_field), header, diff, len(items),
            )


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

    items = _parse_items(eid)
    _check_items_reconcile(items, basic_info)

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
        items=items,
    ).stripped()
