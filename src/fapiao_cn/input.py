"""HTTP request input resolution.

Supports ``multipart/form-data`` (uploaded file) and
``application/json`` (``{"file_path": "..."}``,
``{"file_base64": "..."}``, or ``{"url": "..."}``).

Security guards (H1 LFI / H2 SSRF / H3 OOM):

* ``file_path`` is only honoured when the resolved path falls inside one
  of the directories listed in ``FC_ALLOWED_FILE_PATHS`` (CSV).  An empty
  whitelist disables ``file_path`` entirely.  Symlinks and ``../``
  escapes are caught because we ``Path.resolve()`` before comparing.
* ``url`` fetches are blocked when the resolved IP is in the cloud
  metadata service range (``169.254.169.254`` / ``fd00:ec2::254`` and
  the IPv6 link-local prefix).  Other internal networks are left to the
  Docker / K8s network policy to block (per the agreed risk model).
* ``file_base64`` is size-checked **before** decoding to avoid attacker
  memory exhaustion.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import socket
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from fastapi import Request

from fapiao_cn.config import settings

# Cloud metadata service addresses (AWS / GCP / Azure).  We block these
# unconditionally regardless of how the operator deploys the service.
_BLOCKED_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),   # AWS / GCP / Azure IMDS
        ipaddress.ip_address("fd00:ec2::254"),     # AWS IMDSv2 IPv6
    }
)


async def resolve_input(request: Request) -> Tuple[Optional[bytes], str]:
    """Return ``(contents, filename)`` or ``(None, "")`` if absent."""

    ct = request.headers.get("content-type", "")
    if "multipart" in ct:
        form = await request.form()
        file = form.get("file")
        if file is not None and hasattr(file, "read"):
            data = await file.read()
            return data, getattr(file, "filename", "") or ""
        return await _read_url_or_path_or_b64(
            form.get("url"), form.get("file_path"), form.get("file_base64")
        )
    if "json" in ct:
        try:
            body = await request.json()
        except Exception:
            return None, ""
        return await _read_url_or_path_or_b64(
            body.get("url"), body.get("file_path"), body.get("file_base64")
        )
    return None, ""


def _is_metadata_host(host: str) -> bool:
    """Return True if ``host`` resolves to a blocked metadata IP.

    DNS lookup failures (NXDOMAIN, timeout, etc.) are treated as "not
    metadata" — the request will fail later with a normal network error
    rather than a metadata-blocked error.  We only short-circuit when
    we can positively identify the metadata service.
    """

    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip in _BLOCKED_METADATA_IPS:
            return True
    return False


async def _safe_fetch_url(url: str) -> Optional[bytes]:
    """Fetch ``url`` after blocking cloud-metadata destinations.

    The response body is streamed with a hard byte cap to keep memory
    bounded regardless of upstream behaviour.
    """

    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.hostname or ""
    if _is_metadata_host(host):
        return None

    timeout = httpx.Timeout(30.0, connect=10.0)
    max_bytes = settings.max_file_mb * 1024 * 1024
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", str(url)) as resp:
                resp.raise_for_status()
                chunks: List[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
    except (httpx.HTTPError, httpx.RequestError):
        return None


def _safe_read_file_path(file_path: str) -> Optional[bytes]:
    """Read a file only if it lies inside the configured whitelist.

    ``FC_ALLOWED_FILE_PATHS`` is a CSV of directory paths; empty
    disables ``file_path`` input entirely.  ``Path.resolve()`` follows
    symlinks and ``..`` so an attacker cannot escape the whitelist by
    pointing at a symlink or using relative paths.
    """

    raw = (settings.allowed_file_paths or "").strip()
    if not raw:
        return None
    try:
        target = Path(str(file_path)).resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        return None
    for root_str in raw.split(","):
        root_str = root_str.strip()
        if not root_str:
            continue
        try:
            root = Path(root_str).resolve()
        except (FileNotFoundError, RuntimeError):
            continue
        try:
            target.relative_to(root)
        except ValueError:
            continue
        try:
            return target.read_bytes()
        except OSError:
            return None
    return None


def _safe_decode_base64(data: str) -> Optional[bytes]:
    """Decode base64 with a pre-decode size guard.

    base64 expands by a factor of ~4/3; we reject inputs that would
    exceed ``max_file_mb`` *after* decoding without actually allocating
    the buffer.
    """

    if not data:
        return None
    max_bytes = settings.max_file_mb * 1024 * 1024
    # Cheap upper bound: every 4 chars → 3 bytes.  Strip whitespace
    # before counting so the bound isn't blown up by formatters.
    cleaned = "".join(data.split())
    if (len(cleaned) // 4) * 3 > max_bytes:
        return None
    try:
        decoded = base64.b64decode(cleaned)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) > max_bytes:
        return None
    return decoded


async def _read_url_or_path_or_b64(
    url, file_path, file_base64
) -> Tuple[Optional[bytes], str]:
    if url:
        data = await _safe_fetch_url(str(url))
        if data is None:
            return None, ""
        return data, os.path.basename(str(url).split("?")[0]) or "remote.pdf"
    if file_path:
        data = _safe_read_file_path(str(file_path))
        if data is None:
            return None, ""
        return data, os.path.basename(str(file_path))
    if file_base64:
        data = _safe_decode_base64(str(file_base64))
        if data is None:
            return None, ""
        return data, ""
    return None, ""


async def resolve_batch_input(request: Request) -> Tuple[Optional[List[Tuple[bytes, str]]], str]:
    """Return ``(list_of_contents_and_filenames, error_message)``.

    Supports:
    - ``multipart/form-data`` with repeated field ``files``
    - ``application/json`` with ``{"files": [{"filename": "...", "base64": "..."}]}``

    Enforces limits from ``settings`` (max files, single file size,
    total size).
    """

    ct = request.headers.get("content-type", "")

    uploaded: List[Tuple[bytes, str]] = []

    if "multipart" in ct:
        form = await request.form()
        files = form.multi_items()

        for key, value in files:
            if key == "files" and value is not None and hasattr(value, "read"):
                data = await value.read()
                fname = getattr(value, "filename", "") or ""
                uploaded.append((data, fname))

    elif "json" in ct:
        try:
            body = await request.json()
        except Exception:
            return None, "Invalid JSON body"

        items = body.get("files")
        if not isinstance(items, list):
            return None, "Expected JSON field 'files' as array"

        for item in items:
            if not isinstance(item, dict):
                return None, "Each element in 'files' must be an object"

            fname = item.get("filename", "") or ""
            b64_data = item.get("base64", "")

            if not b64_data:
                return None, f"Missing 'base64' for file {fname or 'unnamed'}"

            data = _safe_decode_base64(str(b64_data))
            if data is None:
                return None, f"Invalid base64 for file {fname or 'unnamed'}"

            uploaded.append((data, fname))

    else:
        return None, "Content-Type must be multipart/form-data or application/json"

    if not uploaded:
        return None, "No files provided"

    max_files = settings.max_batch_files
    if len(uploaded) > max_files:
        return None, f"Too many files: {len(uploaded)} > {max_files}"

    # 单文件上限与 /extract 共用 FC_MAX_FILE_MB，不再另设 batch 专用值：
    # 同一个文件走单文件还是走批量，不该因为入口不同而有不同的可接受边界。
    # 注意 parse_pdf 内部（formats.py）还会再按同一个 max_file_mb 兜底校验一次，
    # 这里的闸门作用是让超限文件在批量入口就以 400 整批拒绝，而不是逐项 error。
    max_single = settings.max_file_mb * 1024 * 1024
    max_total = settings.max_batch_total_mb * 1024 * 1024

    total = 0
    for data, fname in uploaded:
        if len(data) > max_single:
            return None, f"File too large: {fname or 'unnamed'} ({len(data)} bytes)"
        total += len(data)

    if total > max_total:
        return None, f"Total size too large: {total} bytes"

    return uploaded, ""
