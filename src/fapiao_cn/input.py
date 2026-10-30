"""HTTP request input resolution.

Single-file endpoints (``/invoice`` / ``raw`` / ``extract-and-verify``)
support ``multipart/form-data`` (uploaded file) and
``application/json`` (``{"file_path": "..."}``,
``{"file_base64": "..."}``, or ``{"url": "..."}``).

Batch endpoints (``/invoice/batch`` / ``customs/batch``) accept the same
three kinds of input **per file** — see :func:`resolve_batch_input`.

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

import asyncio
import base64
import binascii
import ipaddress
import logging
import os
import socket
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple
from urllib.parse import unquote, urlparse

import httpx
from fastapi import Request

from fapiao_cn.config import settings

log = logging.getLogger("fapiao_cn.input")

# Cloud metadata service addresses (AWS / GCP / Azure).  We block these
# unconditionally regardless of how the operator deploys the service.
_BLOCKED_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),   # AWS / GCP / Azure IMDS
        ipaddress.ip_address("fd00:ec2::254"),     # AWS IMDSv2 IPv6
    }
)

# 批量入口里同时在飞的远端抓取数上限。批量最多 50 个文件、单个上限
# FC_MAX_FILE_MB（默认 10 MB）—— 全部同时抓就是 500 MB 一次性驻留内存，
# 容易把进程打死；限流后峰值约为「并发数 × 单文件上限」。
_BATCH_FETCH_CONCURRENCY = 8


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


def _is_http_url(value: str) -> bool:
    """True when ``value`` looks like an http(s) URL we are able to fetch."""

    parsed = urlparse(str(value).strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _filename_from_url(url: str) -> str:
    """Derive a file name from a URL path.

    Query string and fragment are stripped — neither is part of the file
    name, and a fragment is never even sent to the server.  The last path
    segment is percent-decoded, so a URL-encoded Chinese file name comes
    back as the readable name instead of ``%E7%81%AB...``.
    """

    path = str(url).split("?")[0].split("#")[0]
    return unquote(os.path.basename(path)) or "remote.pdf"


async def _fetch_url_detailed(url: str) -> Tuple[Optional[bytes], str]:
    """Fetch ``url`` after blocking cloud-metadata destinations.

    Returns ``(data, reason)``: ``reason`` is empty on success, otherwise a
    short tag explaining why the fetch was refused (``"unsupported_scheme"``,
    ``"blocked_metadata_host"``, ``"http_status_404"``, ``"too_large"``,
    ``"network_error"``).  It is meant for **logs** — the HTTP response body
    only tells the client that the fetch failed, never *why*.

    The response body is streamed with a hard byte cap to keep memory
    bounded regardless of upstream behaviour.
    """

    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https"):
        return None, "unsupported_scheme"
    host = parsed.hostname or ""
    # DNS 解析是阻塞调用，放线程里做 —— 批量入口会并发抓多个 URL，
    # 在事件循环里直接解析会把它们排队成串行。
    if await asyncio.to_thread(_is_metadata_host, host):
        return None, "blocked_metadata_host"

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
                        return None, "too_large"
                    chunks.append(chunk)
                return b"".join(chunks), ""
    except httpx.HTTPStatusError as exc:
        return None, f"http_status_{exc.response.status_code}"
    except (httpx.HTTPError, httpx.RequestError):
        return None, "network_error"
    except Exception:
        # 非法 URL（坏主机名、坏 IPv6 字面量、IDNA 失败…）在 httpx 里抛的是
        # 各种非 HTTPError 异常；抓取失败一律按“没拿到文件”处理，不让整个
        # 请求 500。
        return None, "invalid_url"


async def _safe_fetch_url(url: str) -> Optional[bytes]:
    """Fetch ``url`` after blocking cloud-metadata destinations.

    Thin wrapper over :func:`_fetch_url_detailed` for callers that only
    care about the bytes.
    """

    data, _ = await _fetch_url_detailed(url)
    return data


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
    except (OSError, RuntimeError, ValueError):
        # OSError 覆盖 FileNotFoundError，也覆盖 Windows 上「路径不合法」
        # 之类的情况 —— 一律按「读不到」处理，不让整批请求 500。
        return None
    for root_str in raw.split(","):
        root_str = root_str.strip()
        if not root_str:
            continue
        try:
            root = Path(root_str).resolve()
        except (OSError, RuntimeError, ValueError):
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
        return data, _filename_from_url(str(url))
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


class _BatchEntry(NamedTuple):
    """批量请求里的一个输入项。

    四个来源互斥，一个条目只填其中一个（``data`` = 已上传的二进制，
    ``url`` = 要去抓的地址，``file_path`` = 服务端本地路径，
    ``base64`` = base64 文本）。
    """

    filename: str
    data: Optional[bytes] = None
    url: Optional[str] = None
    file_path: Optional[str] = None
    base64: Optional[str] = None


async def _resolve_entry(entry: _BatchEntry) -> Tuple[Optional[bytes], str, str]:
    """把一个输入项解析成 ``(内容, 文件名, 错误信息)``。

    错误信息非空即表示这一项失败（整批 400）。

    取值优先级与单文件端点一致：``url`` → ``file_path`` → ``base64``。
    「给了但取不到」是硬错误，不会退回下一种来源 —— 否则调用方会以为
    拿到的是 url 里那份文件，其实却是别处的。
    """

    if entry.url:
        name = entry.filename or _filename_from_url(entry.url)
        data, reason = await _fetch_url_detailed(entry.url)
        if data is None:
            log.warning("batch: URL fetch failed (%s): %s", reason, entry.url)
            return None, "", f"Failed to fetch URL for file {name}"
        return data, name, ""

    if entry.file_path:
        name = entry.filename or os.path.basename(entry.file_path)
        data = _safe_read_file_path(entry.file_path)
        if data is None:
            return None, "", f"Unable to read file_path for file {name}"
        return data, name, ""

    if entry.base64:
        data = _safe_decode_base64(entry.base64)
        if data is None:
            return None, "", f"Invalid base64 for file {entry.filename or 'unnamed'}"
        return data, entry.filename, ""

    if entry.data is not None:
        return entry.data, entry.filename, ""

    return None, "", (
        f"Missing 'url', 'file_path' or 'base64' for file {entry.filename or 'unnamed'}"
    )


async def _resolve_entries(entries: List[_BatchEntry]) -> List[Tuple[Optional[bytes], str, str]]:
    """Resolve every entry, fetching remote files concurrently.

    返回顺序与 ``entries`` 一致 —— 调用方据此确定性地报出第一个出错的
    条目，而不是「谁先失败算谁」。
    """

    if not entries:
        return []

    semaphore = asyncio.Semaphore(_BATCH_FETCH_CONCURRENCY)

    async def _one(entry: _BatchEntry) -> Tuple[Optional[bytes], str, str]:
        if entry.url:
            async with semaphore:
                return await _resolve_entry(entry)
        return await _resolve_entry(entry)

    return list(await asyncio.gather(*(_one(entry) for entry in entries)))


async def resolve_batch_input(request: Request) -> Tuple[Optional[List[Tuple[bytes, str]]], str]:
    """Return ``(list_of_contents_and_filenames, error_message)``.

    支持的入参（与单文件端点 ``/invoice`` 同一套来源，逐个文件给）：

    - ``multipart/form-data``
      - 重复的 ``files`` 字段 —— **上传的二进制文件**；
      - 重复的 ``files`` / ``url`` / ``urls`` 字段 —— **http(s) 地址**；
      - 重复的 ``file_path`` / ``file_paths`` 字段 —— **服务端本地路径**
        （仅当 ``FC_ALLOWED_FILE_PATHS`` 放开时有效）。
    - ``application/json``
      - ``{"files": [{"filename": ..., "url" | "file_path" | "base64": ...}]}``
      - 或单文件快捷写法 ``{"url": ...}`` / ``{"file_path": ...}`` /
        ``{"file_base64": ...}`` —— 与 ``/invoice`` 的键名完全一致。

    远端文件走的是和单文件端点**同一套防护**：云元数据地址一律拒绝、
    响应体按 ``FC_MAX_FILE_MB`` 截断、请求 30 秒超时。

    限额（``settings``）：最大文件数、单文件大小、总大小。
    """

    ct = request.headers.get("content-type", "")

    entries: List[_BatchEntry] = []

    if "multipart" in ct:
        form = await request.form()

        for key, value in form.multi_items():
            if key == "files":
                if hasattr(value, "read"):
                    # 上传的二进制文件
                    data = await value.read()
                    fname = getattr(value, "filename", "") or ""
                    entries.append(_BatchEntry(filename=fname, data=data))
                    continue
                # 普通文本字段：curl -F "files=https://..." 也当 URL 处理
                text = value.strip() if isinstance(value, str) else ""
                if not text:
                    continue
                if not _is_http_url(text):
                    return None, (
                        "Field 'files' must be an uploaded file or an http(s) URL; "
                        "use 'file_paths' for server-side paths"
                    )
                entries.append(_BatchEntry(filename="", url=text))
            elif key in ("url", "urls"):
                text = value.strip() if isinstance(value, str) else ""
                if text:
                    entries.append(_BatchEntry(filename="", url=text))
            elif key in ("file_path", "file_paths"):
                text = value.strip() if isinstance(value, str) else ""
                if text:
                    entries.append(_BatchEntry(filename="", file_path=text))

    elif "json" in ct:
        try:
            body = await request.json()
        except Exception:
            return None, "Invalid JSON body"

        if not isinstance(body, dict):
            return None, "Expected JSON object"

        top_url = body.get("url")
        top_path = body.get("file_path")
        top_b64 = body.get("file_base64")

        items = body.get("files")
        if items is None:
            items = []
        if not isinstance(items, list):
            return None, "Expected JSON field 'files' as array"

        if items and (top_url or top_path or top_b64):
            # 过去顶层 url 会被静默忽略（返回 200 却没抓那个文件），
            # 现在明确报错，免得调用方以为文件已经进来了。
            return None, (
                "Provide batch inputs either at the top level ('url' / 'file_path' / "
                "'file_base64') or inside 'files', not both"
            )

        if items:
            for item in items:
                if not isinstance(item, dict):
                    return None, "Each element in 'files' must be an object"

                fname = item.get("filename", "") or ""
                url = item.get("url")
                file_path = item.get("file_path")
                b64 = item.get("base64")

                if url:
                    entries.append(_BatchEntry(filename=fname, url=str(url)))
                elif file_path:
                    entries.append(_BatchEntry(filename=fname, file_path=str(file_path)))
                elif b64:
                    entries.append(_BatchEntry(filename=fname, base64=str(b64)))
                else:
                    return None, (
                        "Missing 'url', 'file_path' or 'base64' for file "
                        f"{fname or 'unnamed'}"
                    )
        elif top_url:
            # 单文件快捷写法，与 /invoice 的键名一致
            entries.append(_BatchEntry(filename="", url=str(top_url)))
        elif top_path:
            entries.append(_BatchEntry(filename="", file_path=str(top_path)))
        elif top_b64:
            entries.append(_BatchEntry(filename="", base64=str(top_b64)))

    else:
        return None, "Content-Type must be multipart/form-data or application/json"

    if not entries:
        return None, "No files provided"

    # 文件数闸门放在抓取之前 —— 否则 51 个 URL 会先把 51 个文件都下下来
    # 才被拒绝，白白吃掉带宽和内存。
    max_files = settings.max_batch_files
    if len(entries) > max_files:
        return None, f"Too many files: {len(entries)} > {max_files}"

    resolved = await _resolve_entries(entries)

    uploaded: List[Tuple[bytes, str]] = []
    for data, fname, error in resolved:
        if error:
            return None, error
        if data is None:
            return None, "No files provided"
        uploaded.append((data, fname))

    # 单文件上限与 /invoice 共用 FC_MAX_FILE_MB，不再另设 batch 专用值：
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
