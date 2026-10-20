"""OFD 数电票 parser.

OFD (Open Fixed-layout Document, GB/T 33190-2016) is the rendering-side
counterpart of the XML 数电发票 (fully-digitalised e-invoice).  An
``.ofd`` file is a ZIP archive containing:

* ``OFD.xml``                     — document root metadata
* ``Doc_0/Document.xml``          — page index (``<ofd:Page ID="x" BaseLoc="..."/>``)
* ``Doc_0/Pages/Page_<N>/Content.xml`` — text objects (``<ofd:TextObject ID="..."
  Boundary="..."><ofd:TextCode>VALUE</ofd:TextCode></ofd:TextObject>``)
* ``Doc_0/Tags/CustomTag.xml``    — optional field dictionary that points each
  semantic name (e.g. ``InvoiceNo``, ``TaxExclusiveTotalAmount``) at a
  ``<ofd:ObjectRef PageRef="X">OBJECT_ID</ofd:ObjectRef>`` — the
  recommended fast-path for 数电票.

The parser tries the structured path first (CustomTag.xml → TextObject
lookup) and falls back to a position-aware row scan over the page
content when the dictionary is missing or incomplete (some small-amount
B2C receipts omit it).

Field semantics share the same ``EInvoice`` schema as the standalone XML
parser; the underlying extraction logic is delegated to
``fapiao_cn.parsers.xml_invoice.extract_invoice_from_root`` for any
fragment that already exposes the XML form.  See H43 ("XML schema
sync responsibility") in the README.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Dict, List, Optional, Tuple

import defusedxml.ElementTree as DET

from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import normalize_issue_date

# Maximum number of entries allowed in an OFD container (defends against
# zip-bomb-style attacks that flood the central directory).
_MAX_ENTRIES = 60

# Maximum decompressed size of any single member (60 MiB).  Real 数电票
# OFDs are 50-500 KiB; the headroom accommodates extra-large signed
# copies without trusting unbounded input.
_MAX_ENTRY_SIZE = 60 * 1024 * 1024

_OF_NS = "{http://www.ofdspec.org/2016}"

# Field name → CustomTag parent tag for the structured path.
# Each maps to either a top-level ``<ofd:NAME>`` or a nested
# ``<ofd:OUTER><ofd:INNER>`` pair.
_STRUCTURED_FIELD_MAP: Dict[str, Tuple[str, ...]] = {
    "invoice_number": ("InvoiceNo",),
    "issue_date": ("IssueDate",),
    "amount_without_tax": ("TaxExclusiveTotalAmount",),
    "tax_amount": ("TaxTotalAmount",),
    "total_amount": ("TaxInclusiveTotalAmount",),
    "drawer": ("InvoiceClerk",),
    "buyer_name": ("Buyer", "BuyerName"),
    "buyer_tax_id": ("Buyer", "BuyerTaxID"),
    "seller_name": ("Seller", "SellerName"),
    "seller_tax_id": ("Seller", "SellerTaxID"),
}

# Regex patterns used by the layout-based fallback.  Each pattern is
# applied per row (text items grouped by y_max proximity, sorted by
# x_min).  Tuple ``(compiled, post-process)``; ``post-process`` may be
# None to keep the raw match.
_INVOICE_TYPE_RE = re.compile(r"电子发票[（(]([^）)]+?)[）)]")
_INVOICE_NUMBER_RE = re.compile(r"发票号码[：:]?\s*(\d{15,25})")
_ISSUE_DATE_RE = re.compile(
    r"开票日期[：:]?\s*((?:20|19)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
)
_DRAWER_RE = re.compile(r"开票人[：:]?\s*(\S+?)(?=\s|$)")
# `名称：` typically appears twice on one row in a buyer/seller info
# block; splitting on it yields ``["", buyer_value, seller_value]``.
_TAX_ID_RE = re.compile(
    r"统一社会信用代码/(?:纳税人识别号|纳税人识别号|纳稅人识别号)[：:]?\s*([0-9A-Z]{15,20})"
)
# Total-amount row carries 价税合计（大写）+ Chinese uppercase + （小写）+ amount.
# OFD writers don't agree on the order of labels: most put 小写 BEFORE the
# amount, but烤匠-style ones put 小写 AFTER.  Use a sequence of Chinese
# amount digits (the uppercase value) that is at least 2 chars long; we
# then look for a decimal/number on the same row for the 小写 value.
_TOTAL_CHIN_AMOUNT_RE = re.compile(
    r"([壹贰叁肆伍陆柒捌玖拾佰仟万亿圆角分零〇一二三四五六七八九]{2,})"
)
_TOTAL_NUM_RE = re.compile(r"(\d+\.\d{2})")
# Sub-amount row carries 合计 + ¥ amount (+ ¥ amount for tax).
_SUBTOTAL_RE = re.compile(
    r"合\s*计\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*[¥￥]?\s*(\d+(?:\.\d+)?)?"
)
_AMOUNT_RE = re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)")

# Path matcher for OFD template-layer Content.xml files.  GB/T 33190
# stores static banner text (e.g. the "电子发票（X发票）" title) under
# ``Doc_0/Tpls/Tpl_<index>/Content.xml``; pages only carry per-page
# dynamic data.  We scan those files separately to recover
# ``invoice_type`` when the CustomTag dictionary does not surface it.
_TPL_CONTENT_RE = re.compile(r"^Doc_0/Tpls/Tpl_\d+/Content\.xml$")

# Tolerance for grouping text objects into rows by y_max.  OFD
# coordinates are in millimetres; 1.0 mm catches typical kerning
# variance plus the small offsets OFD writers introduce when splitting
# a label like "价税合计（大写）" across adjacent text boxes.
_ROW_TOLERANCE = 1.0


def parse_ofd_invoice(ofd_bytes: bytes) -> Optional[InvoiceRecord]:
    """Open an OFD 数电票 archive and extract an ``InvoiceRecord``.

    Returns ``None`` for non-OFD input, files without a recognisable
    ``Document.xml``, security violations (too many entries, oversized
    members), or XML that cannot be defused-parsed.
    """

    try:
        with zipfile.ZipFile(io.BytesIO(ofd_bytes)) as z:
            names = z.namelist()
            if len(names) > _MAX_ENTRIES:
                return None
            for info in z.infolist():
                if info.file_size > _MAX_ENTRY_SIZE:
                    return None

            if "Doc_0/Document.xml" not in names:
                return None

            doc_xml = _safe_read(z, "Doc_0/Document.xml")
            if doc_xml is None:
                return None

            page_map = _parse_page_map(doc_xml)
            if not page_map:
                return None

            # Load every referenced page's Content.xml once.
            page_contents: Dict[str, str] = {}
            for page_id, rel_path in page_map.items():
                content = _safe_read(z, f"Doc_0/{rel_path}")
                if content is not None:
                    page_contents[page_id] = content

            # Load template-layer Content.xml (banner text like
            # "电子发票（增值税专用发票）").  CustomTag never references
            # these, but the title banner lives here per GB/T 33190.
            template_contents: Dict[str, str] = {}
            for name in names:
                if _TPL_CONTENT_RE.match(name):
                    content = _safe_read(z, name)
                    if content is not None:
                        template_contents[name] = content

            record = InvoiceRecord(page_count=len(page_contents))

            # Strategy A: CustomTag structured path.
            tag_path = _find_custom_tag(names)
            if tag_path is not None:
                tag_xml = _safe_read(z, tag_path)
                if tag_xml is not None:
                    try:
                        tag_root = DET.fromstring(tag_xml)
                    except Exception:
                        tag_root = None
                    if tag_root is not None:
                        record = _extract_from_custom_tag(
                            tag_root, page_contents
                        )

            # Strategy B: layout-based fallback / enrichment.
            rows = _build_rows(page_contents)
            record = _fill_from_layout(record, rows)
            # Templates carry static banner text not present in any
            # page — scan them only for ``invoice_type`` to avoid
            # polluting dynamic fields.
            _fill_invoice_type_from_templates(record, template_contents)
            record.page_count = len(page_contents) or record.page_count

            # ``with_expense_category`` 与 XML / PDF 两条路径对齐：OFD 全电票
            # 同样要产出消费类别，否则 formats._parse_ofd 拷出去的三个字段
            # 永远是空串（空串与「报关单跳过 / 开关关闭」语义无法区分）。
            return record.stripped().with_expense_category() if record else None
    except (zipfile.BadZipFile, KeyError, ValueError):
        return None


def _safe_read(z: zipfile.ZipFile, name: str) -> Optional[str]:
    """Read a single UTF-8 string from a zip member; return ``None`` if it
    cannot be decoded.  Defends against members that are not text XML.
    """

    try:
        data = z.read(name)
    except KeyError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse_page_map(doc_xml: str) -> Dict[str, str]:
    """Return ``{Page_ID: Content.xml relative path}`` from Document.xml.

    Strips the ``Doc_0/`` prefix so the caller can re-prepend it.
    """

    out: Dict[str, str] = {}
    for m in re.finditer(
        r'<ofd:Page\s+ID="(\d+)"\s+BaseLoc="([^"]+)"', doc_xml
    ):
        page_id = m.group(1)
        base_loc = m.group(2)
        if base_loc.startswith("Doc_0/"):
            base_loc = base_loc[len("Doc_0/"):]
        out[page_id] = base_loc
    return out


def _find_custom_tag(names: List[str]) -> Optional[str]:
    """Locate the ``CustomTag.xml`` member if present.

    Some OFD writers place it under ``Doc_0/Tags/CustomTag.xml`` (most
    common) or at the document root.  We prefer the Tags path.
    """

    if "Doc_0/Tags/CustomTag.xml" in names:
        return "Doc_0/Tags/CustomTag.xml"
    for n in names:
        if n.endswith("CustomTag.xml"):
            return n
    return None


def _extract_textcode(content: str, obj_id: str) -> str:
    """Find a ``<ofd:TextObject ID="obj_id">`` and concatenate its
    ``<ofd:TextCode>`` segments.  Returns ``""`` if not found.
    """

    pattern = (
        rf'<ofd:TextObject[^>]*ID="{re.escape(obj_id)}"[^>]*>'
        r"(.*?)</ofd:TextObject>"
    )
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return ""
    codes = re.findall(r"<ofd:TextCode[^>]*>([^<]*)</ofd:TextCode>", m.group(1))
    return "".join(codes)


def _resolve_field(
    parent: DET.Element,
    page_contents: Dict[str, str],
    tag_path: Tuple[str, ...],
) -> str:
    """Resolve a CustomTag field by following ``tag_path`` (one or two
    tags) and joining the text of every ObjectRef it carries.
    """

    elem = parent
    for tag in tag_path:
        elem = elem.find(f"{_OF_NS}{tag}")
        if elem is None:
            return ""
    refs = elem.findall(f"{_OF_NS}ObjectRef")
    if not refs:
        return ""

    parts: List[str] = []
    for ref in refs:
        page_ref = ref.get("PageRef")
        obj_id = (ref.text or "").strip()
        if not page_ref or not obj_id:
            continue
        content = page_contents.get(page_ref, "")
        if not content:
            continue
        text = _extract_textcode(content, obj_id)
        if text:
            parts.append(text)
    return "".join(parts)


def _extract_from_custom_tag(
    tag_root: DET.Element,
    page_contents: Dict[str, str],
) -> InvoiceRecord:
    """Strategy A: drive the record from ``<ofd:root>`` in CustomTag.xml.

    The ``<ofd:root>`` element is the document root of ``CustomTag.xml``
    itself; we walk it directly without another ``find`` lookup.
    """

    root = tag_root

    def _field(path: Tuple[str, ...]) -> str:
        return _resolve_field(root, page_contents, path)

    raw_amount_without_tax = _field(("TaxExclusiveTotalAmount",))
    raw_tax_amount = _field(("TaxTotalAmount",))
    raw_total_amount = _field(("TaxInclusiveTotalAmount",))

    return InvoiceRecord(
        page_count=len(page_contents) or 1,
        invoice_number=_field(("InvoiceNo",)),
        issue_date=normalize_issue_date(_field(("IssueDate",))),
        buyer_name=_field(("Buyer", "BuyerName")),
        buyer_tax_id=_field(("Buyer", "BuyerTaxID")),
        seller_name=_field(("Seller", "SellerName")),
        seller_tax_id=_field(("Seller", "SellerTaxID")),
        amount_without_tax=_strip_yuan(raw_amount_without_tax),
        tax_amount=_strip_yuan(raw_tax_amount),
        total_amount=_strip_yuan(raw_total_amount),
        total_amount_upper=_field(("TaxInclusiveTotalAmountInChinese",)),
        drawer=_field(("InvoiceClerk",)),
    )


def _strip_yuan(value: str) -> str:
    """Remove ``¥`` / ``￥`` and surrounding whitespace; keep digits/dot."""

    if not value:
        return ""
    stripped = value.replace("¥", "").replace("￥", "").strip()
    return stripped


# ── Strategy B: layout-based extraction ─────────────────────────────────


def _build_rows(page_contents: Dict[str, str]) -> List[str]:
    """Cluster every ``<ofd:TextObject>`` across all pages into rows by
    ``y_max`` proximity and return one string per row, items sorted
    left-to-right.
    """

    items: List[Tuple[float, float, str]] = []
    for content in page_contents.values():
        try:
            root = DET.fromstring(content)
        except Exception:
            continue
        for to in root.iter(f"{_OF_NS}TextObject"):
            boundary = (to.get("Boundary") or "0 0 0 0").split()
            if len(boundary) < 4:
                continue
            try:
                x_min = float(boundary[0])
                y_max = float(boundary[1])
            except ValueError:
                continue
            text_elems = to.findall(f"{_OF_NS}TextCode")
            text = "".join(te.text or "" for te in text_elems).strip()
            if text:
                items.append((x_min, y_max, text))

    items.sort(key=lambda i: (-i[1], i[0]))

    rows: List[List[Tuple[float, str]]] = []
    current: List[Tuple[float, str]] = []
    last_y: Optional[float] = None
    for x, y, text in items:
        if last_y is None or abs(last_y - y) <= _ROW_TOLERANCE:
            current.append((x, text))
            if last_y is None:
                last_y = y
        else:
            rows.append(current)
            current = [(x, text)]
            last_y = y
    if current:
        rows.append(current)
    for row in rows:
        row.sort(key=lambda i: i[0])
    return [" ".join(text for _, text in row) for row in rows]


def _fill_from_layout(
    record: InvoiceRecord,
    rows: List[str],
) -> InvoiceRecord:
    """For any field still empty, scan the position-sorted rows and
    apply label-anchored regex.
    """

    if not rows:
        return record

    flat = "\n".join(rows)

    if not record.invoice_number:
        m = _INVOICE_NUMBER_RE.search(flat)
        if m:
            record.invoice_number = m.group(1)

    if not record.issue_date:
        m = _ISSUE_DATE_RE.search(flat)
        if m:
            record.issue_date = normalize_issue_date(m.group(1))

    if not record.invoice_type:
        m = _INVOICE_TYPE_RE.search(flat)
        if m:
            vat = m.group(1).strip()
            # Normalise half-width parentheses (which appear when OFD
            # round-trips through latin-1 codepoints) to full-width for
            # consistency with the PDF and XML pipelines.
            if vat == "增值税专用发票" or "专" in vat:
                record.invoice_type = "电子发票（增值税专用发票）"
            elif vat == "普通发票" or "普" in vat:
                record.invoice_type = "电子发票（普通发票）"

    if not record.drawer:
        m = _DRAWER_RE.search(flat)
        if m:
            record.drawer = m.group(1).strip()

    # Multi-row name extraction: 名称： appears twice (buyer, seller)
    # within the buyer/seller info block; collect them in left-to-right
    # order from the rows that mention the label.
    if not record.buyer_name or not record.seller_name:
        names = _collect_names(rows)
        if len(names) >= 1 and not record.buyer_name:
            record.buyer_name = names[0]
        if len(names) >= 2 and not record.seller_name:
            record.seller_name = names[1]

    # Tax IDs — gather all matches; longest 18-20 char one is the seller,
    # any other (typically absent in B2C receipts) is the buyer.
    if not record.seller_tax_id or not record.buyer_tax_id:
        ids = _collect_tax_ids(rows)
        if ids:
            ids.sort(key=len, reverse=True)
            if not record.seller_tax_id:
                record.seller_tax_id = ids[0]
            if len(ids) >= 2 and not record.buyer_tax_id:
                record.buyer_tax_id = ids[1]

    # Amount fallback: only kick in if CustomTag didn't supply them.
    # We handle 价税合计 (total) and 合计 (subtotal = amount without tax
    # + tax amount) as distinct rows.
    if not record.total_amount or not record.total_amount_upper:
        total_row = _find_row_containing(rows, "价税合计")
        if total_row is not None:
            if not record.total_amount_upper:
                m = _TOTAL_CHIN_AMOUNT_RE.search(total_row)
                if m:
                    record.total_amount_upper = m.group(1).strip()
            if not record.total_amount:
                m = _TOTAL_NUM_RE.search(total_row)
                if m:
                    record.total_amount = m.group(1)

    if not record.amount_without_tax or not record.tax_amount:
        subtotal_row = _find_subtotal_row(rows)
        if subtotal_row is not None:
            m = _SUBTOTAL_RE.search(subtotal_row)
            if m:
                if not record.amount_without_tax:
                    record.amount_without_tax = m.group(1)
                if (
                    not record.tax_amount
                    and m.group(2)
                    and m.group(2) != m.group(1)
                ):
                    record.tax_amount = m.group(2)

    return record


def _find_row_containing(rows: List[str], keyword: str) -> Optional[str]:
    """Return the first row whose text contains ``keyword``."""

    for row in rows:
        if keyword in row:
            return row
    return None


def _find_subtotal_row(rows: List[str]) -> Optional[str]:
    """Return the row containing the subtotal label (合计 or 合 计) but not
    the 价税合计 row that carries the grand-total label.
    """

    for row in rows:
        if "价税合计" in row:
            continue
        if "合计" in row or "合 计" in row:
            return row
    return None


def _collect_names(rows: List[str]) -> List[str]:
    """Return name values adjacent to ``名称：`` labels, in left-to-right
    page order.  Most 数电票 layouts have a single row with two
    ``名称：`` labels (buyer side, seller side) — split on the label and
    extract a Chinese-character blob from each segment.  The blob regex
    is robust against isolated single-char section labels (购 / 销 / 买 /
    售 / 方 / 信 / 息) that often leak into the same cluster.
    """

    out: List[str] = []
    for row in rows:
        if "名称" not in row:
            continue
        parts = re.split(r"名称[：:]", row)
        # parts[0] = text before first label (usually empty or noise);
        # parts[1] = first value (buyer); parts[2] = second value (seller).
        for seg in parts[1:]:
            # Pull the longest CJK blob (>= 2 chars) — this naturally
            # skips isolated label chars like "销" that sometimes trail
            # the value within the same x-cluster.
            blob = _LONG_NAME_CHUNKS_RE.search(seg)
            if blob:
                out.append(blob.group(1).strip())
        if out:
            return out
    return out


# Match a contiguous CJK chunk (optionally followed by a bracketed
# sub-chunk like "（个人）" or "（苏州）有限公司").  Length >= 2 filters
# out isolated single-char section labels (购/销/买/售/方/信/息).
_LONG_NAME_CHUNKS_RE = re.compile(
    r"([一-鿿]{2,}(?:[（(][^)）]+[）)])?)"
)


def _collect_tax_ids(rows: List[str]) -> List[str]:
    """Return 统一社会信用代码 values from any row that has the label."""

    out: List[str] = []
    for row in rows:
        if "统一社会信用代码" not in row and "纳税人识别号" not in row:
            continue
        ids = _TAX_ID_RE.findall(row)
        out.extend(ids)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: List[str] = []
    for tid in out:
        if tid not in seen:
            seen.add(tid)
            unique.append(tid)
    return unique


def _find_amount_row(rows: List[str]) -> Optional[str]:
    """Locate the row containing the ``价税合计`` or ``合计`` keyword
    block (which carries ``¥ amount`` values).
    """

    for row in rows:
        if "价税合计" in row or ("¥" in row and "合计" in row):
            return row
    return None


__all__ = ["parse_ofd_invoice"]


def _fill_invoice_type_from_templates(
    record: InvoiceRecord,
    template_contents: Dict[str, str],
) -> None:
    """Scan template-layer (Tpls/) TextObjects for the static invoice-type
    banner (``电子发票（X发票）``) and set ``record.invoice_type`` when it
    hasn't been resolved from the page layer or CustomTag.

    OFD 数电票 layouts split dynamic data (pages) from static chrome
    (templates).  The title banner lives in templates because every
    page references the same template ID; the CustomTag dictionary never
    carries an ``invoice_type`` field, so we have to look here.
    """

    if record.invoice_type or not template_contents:
        return
    for content in template_contents.values():
        try:
            root = DET.fromstring(content)
        except Exception:
            continue
        for to in root.iter(f"{_OF_NS}TextObject"):
            text_elems = to.findall(f"{_OF_NS}TextCode")
            text = "".join(te.text or "" for te in text_elems)
            m = _INVOICE_TYPE_RE.search(text)
            if not m:
                continue
            vat = m.group(1).strip()
            if vat == "增值税专用发票" or "专" in vat:
                record.invoice_type = "电子发票（增值税专用发票）"
            elif vat == "普通发票" or "普" in vat:
                record.invoice_type = "电子发票（普通发票）"
            return