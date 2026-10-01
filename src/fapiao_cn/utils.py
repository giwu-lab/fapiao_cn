"""Small helpers used across parsers.

Tuned for the full text produced by ``pdfplumber``: NBSP / fullwidth
spaces, sparse CJK glyphs, and label / value separators printed with
internal whitespace (``收 款 人`` etc.).
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Company name keywords (used to detect names that look like real organisations
# versus labels, in two-column layouts and fallback patterns).
_COMPANY_KEYWORDS = (
    "公司", "厂", "店", "部", "社", "中心", "企业",
    "超市", "加油站", "分行", "分公司", "分店", "经营部", "合作社",
)

# Pre-compiled patterns
CIPHER_RE = re.compile(r"[><*+\-/\\]{3,}")
_TAX_ID_RE = re.compile(r"[0-9A-Z]{15,20}")
_WHITESPACE_RE = re.compile(r"\s+")

# Date patterns — ``YYYY年MM月DD日`` from PDF text and ``YYYY-MM-DD``
# from XML / 非税票据.  Both paths should converge to ISO ``YYYY-MM-DD``.
_DATE_CN_RE = re.compile(r"(?:20\d{2}|19\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_ISO_RE = re.compile(r"(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})")

# People extraction — used by invoice.py and old_invoice.py
# CJK-only to avoid capturing semicolons / punctuation like "梁慧;"
_PEOPLE_INLINE_RE = re.compile(
    r"收\s*款\s*人\s*[:：]\s*([\u4e00-\u9fff]+).*?"
    r"复\s*核\s*[:：人]?\s*([\u4e00-\u9fff]+).*?"
    r"开\s*票\s*人\s*[:：]?\s*([\u4e00-\u9fff]+)"
)
_PAYEE_RE = re.compile(r"收\s*款\s*人\s*[:：]\s*([\u4e00-\u9fff]+)")
_REVIEWER_RE = re.compile(r"复\s*核\s*人?\s*[:：]\s*([\u4e00-\u9fff]+)")
_DRAWER_RE = re.compile(r"开\s*票\s*人\s*[:：]?\s*([\u4e00-\u9fff]+)")


def first_match(pattern: str, text: str, group: int = 1, flags: int = 0) -> str:
    """Return the first regex match group, or empty string."""

    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else ""


def truncate(value: object, limit: int = 200) -> str:
    """Stringify ``value`` and clamp it to ``limit`` characters.

    Long internal-exception messages (PdfminerException in particular)
    can embed PDF trailer / filesystem paths that should not appear on
    the wire — see H13.
    """

    s = str(value)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def strip_spaces(s: str) -> str:
    """Remove all whitespace from a string (digits, dates, etc.)."""

    return _WHITESPACE_RE.sub("", s) if s else ""


def normalize_issue_date(s: str) -> str:
    """Return ``s`` as ISO ``YYYY-MM-DD``.

    Handles two upstream representations:

    * Chinese ``2026年06月20日`` (or with internal whitespace) — typical
      output of regexes scanning pdfplumber text.
    * ISO ``2026-06-29`` (or with optional trailing time) — typical from
      XML and the 中央非税收入统一票据 parser.

    Both forms are zero-padded on month/day.  ``s`` is returned as-is
    when neither pattern matches, preserving the raw extraction so the
    caller can decide what to do.
    """

    if not s:
        return ""
    cn = _DATE_CN_RE.search(s)
    if cn:
        y_match = re.search(r"(20\d{2}|19\d{2})", s)
        if y_match:
            return f"{y_match.group(1)}-{int(cn.group(1)):02d}-{int(cn.group(2)):02d}"
    iso = _DATE_ISO_RE.search(s)
    if iso:
        return f"{iso.group(1)}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    return s


def normalize_lines(text: str) -> List[str]:
    """Split text into clean visual lines.

    pdfplumber uses ``\\n`` as line separator and embeds single spaces
    inside horizontal runs. We collapse runs of internal whitespace so
    downstream regexes work against a single space delimiter.
    """

    return [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]


def looks_like_company_name(s: str, *, tail_only: bool = True) -> bool:
    """Return True if ``s`` looks like a real company / merchant name.

    ``tail_only=True`` (default): only the last 6 characters are checked
    against the keyword set, which prevents labels like
    "统一社会信用代码/纳税人识别号：" from being mis-detected.
    ``tail_only=False``: the entire string is checked (used by the
    merged-code parser, which deals with looser text).
    """

    if not s or len(s) < 4 or len(s) > 40:
        return False
    haystack = s[-6:] if tail_only else s
    return any(k in haystack for k in _COMPANY_KEYWORDS)


def looks_like_tax_id(s: str) -> bool:
    """Return True if ``s`` is a 15-20 char alphanumeric tax id."""

    return bool(_TAX_ID_RE.fullmatch(s.strip()))


def extract_people(text: str) -> Tuple[str, str, str]:
    """Return ``(payee, reviewer, drawer)``.

    Tries the inline pattern (all three on one line) first; if that
    fails, scans for each label independently. Names are CJK-only to
    avoid capturing trailing punctuation like ``;``.
    """

    inline = _PEOPLE_INLINE_RE.search(text)
    if inline:
        return inline.group(1), inline.group(2), inline.group(3)
    payee_m = _PAYEE_RE.search(text)
    reviewer_m = _REVIEWER_RE.search(text)
    drawer_m = _DRAWER_RE.search(text)
    return (
        payee_m.group(1) if payee_m else "",
        reviewer_m.group(1) if reviewer_m else "",
        drawer_m.group(1) if drawer_m else "",
    )
