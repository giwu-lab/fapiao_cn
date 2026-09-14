"""HTTP request input resolution.

Supports ``multipart/form-data`` (uploaded file) and
``application/json`` (``{"file_path": "..."}``,
``{"file_base64": "..."}``, or ``{"url": "..."}``).
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import List, Optional, Tuple

import httpx
from fastapi import Request

from fapiao_cn.config import settings


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


async def _read_url_or_path_or_b64(
    url, file_path, file_base64
) -> Tuple[Optional[bytes], str]:
    if url:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(str(url))
                resp.raise_for_status()
                return resp.content, os.path.basename(str(url).split("?")[0]) or "remote.pdf"
        except Exception:
            return None, ""
    if file_path:
        try:
            with open(str(file_path), "rb") as f:
                return f.read(), os.path.basename(str(file_path))
        except (FileNotFoundError, OSError):
            return None, ""
    if file_base64:
        try:
            return base64.b64decode(str(file_base64)), ""
        except (binascii.Error, ValueError):
            return None, ""
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

            try:
                data = base64.b64decode(str(b64_data))
            except (binascii.Error, ValueError):
                return None, f"Invalid base64 for file {fname or 'unnamed'}"

            uploaded.append((data, fname))

    else:
        return None, "Content-Type must be multipart/form-data or application/json"

    if not uploaded:
        return None, "No files provided"

    max_files = settings.max_batch_files
    if len(uploaded) > max_files:
        return None, f"Too many files: {len(uploaded)} > {max_files}"

    max_single = settings.max_batch_single_mb * 1024 * 1024
    max_total = settings.max_batch_total_mb * 1024 * 1024

    total = 0
    for data, fname in uploaded:
        if len(data) > max_single:
            return None, f"File too large: {fname or 'unnamed'} ({len(data)} bytes)"
        total += len(data)

    if total > max_total:
        return None, f"Total size too large: {total} bytes"

    return uploaded, ""
