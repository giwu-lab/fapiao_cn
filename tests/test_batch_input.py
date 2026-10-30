"""批量入参解析的契约测试（fapiao_cn.input.resolve_batch_input）。

批量端点的「入参怎么取到字节」住在**开源版**的 ``fapiao_cn.input`` 里
（企业版包只负责路由、许可证与线程池），所以这里的用例既不依赖
``fapiao_cn_ee``，也不依赖 ``samples/`` —— 公开仓库的 CI 同样跑得到。

服务端用真实的 HTTP 回环地址（``serve_http``）而不是 mock，这样 URL
抓取路径（含超时、体积上限、元数据地址拦截）走的是生产代码。
"""

from __future__ import annotations

import base64
import io
from typing import Iterator, List

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from fapiao_cn.input import resolve_batch_input
from tests._helpers import serve_http

# 一个体积很小的假 PDF —— 这里只关心「入参能不能取到正确的字节」，
# 不关心解析结果（解析在 test_batch.py 里测）。
FAKE_PDF = b"%PDF-1.4\n% fake pdf for batch input tests\n%%EOF\n"

# 云元数据地址：必须被拒，而且**不能**真的去连（连不上会等超时）。
METADATA_URL = "http://169.254.169.254/latest/meta-data/"


def _make_client() -> TestClient:
    """Probe app：把解析结果回显出来，便于断言。"""

    app = FastAPI()

    @app.post("/probe")
    async def _probe(request: Request):
        files, error = await resolve_batch_input(request)
        if files is None:
            return JSONResponse({"error": error}, status_code=400)
        return {
            "count": len(files),
            "names": [name for _, name in files],
            "sizes": [len(data) for data, _ in files],
            "bodies": [data.decode("latin-1") for data, _ in files],
        }

    return TestClient(app)


client = _make_client()


@pytest.fixture
def hits() -> List[str]:
    """记录 HTTP 服务收到的请求路径。"""

    return []


@pytest.fixture
def server(hits: List[str]) -> Iterator[str]:
    """本地 HTTP 服务：``/invoice.pdf`` 返回假 PDF，其余 404。"""

    def _resolve(path: str):
        return FAKE_PDF if path == "/invoice.pdf" else None

    with serve_http(_resolve, hits=hits) as base:
        yield base


# ── multipart ────────────────────────────────────────────────────────


class TestMultipartBatchInput:
    def test_uploaded_file_still_works(self):
        """回归：重复的 ``files`` 字段传二进制，行为不变。"""

        resp = client.post(
            "/probe",
            files=[("files", ("a.pdf", io.BytesIO(FAKE_PDF), "application/pdf"))],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == 1
        assert body["names"] == ["a.pdf"]
        assert body["bodies"][0] == FAKE_PDF.decode("latin-1")

    def test_files_field_as_url(self, server):
        """``files`` 字段给一个 http(s) 地址 → 当成远端文件取。"""

        resp = client.post("/probe", files={"files": (None, f"{server}/invoice.pdf")})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == 1
        # 文件名从 URL 路径推导
        assert body["names"] == ["invoice.pdf"]
        assert body["bodies"][0] == FAKE_PDF.decode("latin-1")

    def test_urls_field(self, server):
        """重复的 ``urls`` 字段 —— 每项一个地址。"""

        resp = client.post(
            "/probe",
            files=[
                ("urls", (None, f"{server}/invoice.pdf")),
                ("urls", (None, f"{server}/invoice.pdf?token=abc")),
            ],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == 2
        assert body["names"] == ["invoice.pdf", "invoice.pdf"]
        assert all(b == FAKE_PDF.decode("latin-1") for b in body["bodies"])

    def test_url_field_singular(self, server):
        """单数 ``url`` 字段（与 ``/invoice`` 的 multipart 键名一致）。"""

        resp = client.post("/probe", files={"url": (None, f"{server}/invoice.pdf")})
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 1

    def test_file_paths_field(self, tmp_path):
        """``file_paths`` 字段 —— 走 FC_ALLOWED_FILE_PATHS 白名单。"""

        from fapiao_cn.config import settings

        target = tmp_path / "local.pdf"
        target.write_bytes(FAKE_PDF)
        settings.allowed_file_paths = str(tmp_path)

        resp = client.post("/probe", files={"file_paths": (None, str(target))})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["names"] == ["local.pdf"]
        assert body["bodies"][0] == FAKE_PDF.decode("latin-1")

    def test_mixed_upload_and_url_keep_order(self, server):
        """上传文件与 URL 混着传时，顺序按表单字段出现顺序。"""

        resp = client.post(
            "/probe",
            files=[
                ("files", ("first.pdf", io.BytesIO(FAKE_PDF), "application/pdf")),
                ("urls", (None, f"{server}/invoice.pdf")),
                ("files", ("third.pdf", io.BytesIO(FAKE_PDF), "application/pdf")),
            ],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["names"] == ["first.pdf", "invoice.pdf", "third.pdf"]

    def test_plain_text_in_files_field_is_rejected(self):
        """``files`` 字段给一个既不是文件也不是 http(s) 的字符串 → 明确报错。

        过去这种值会被**静默忽略**，最后只看到一句含糊的 "No files provided"。
        """

        resp = client.post("/probe", files={"files": (None, "/etc/passwd")})
        assert resp.status_code == 400
        detail = resp.json()["error"]
        assert "http(s) URL" in detail
        assert "file_paths" in detail

    def test_no_inputs_at_all(self):
        resp = client.post("/probe", files={"dummy": (None, "x")})
        assert resp.status_code == 400
        assert resp.json()["error"] == "No files provided"


# ── JSON ─────────────────────────────────────────────────────────────


class TestJsonBatchInput:
    def test_files_array_with_url(self, server):
        resp = client.post(
            "/probe",
            json={"files": [{"filename": "远端.pdf", "url": f"{server}/invoice.pdf"}]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == 1
        assert body["names"] == ["远端.pdf"]
        assert body["bodies"][0] == FAKE_PDF.decode("latin-1")

    def test_files_array_with_base64_still_works(self):
        """回归：base64 入参行为不变。"""

        payload = base64.b64encode(FAKE_PDF).decode()
        resp = client.post(
            "/probe",
            json={"files": [{"filename": "b64.pdf", "base64": payload}]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["names"] == ["b64.pdf"]
        assert body["bodies"][0] == FAKE_PDF.decode("latin-1")

    def test_files_array_with_file_path(self, tmp_path):
        from fapiao_cn.config import settings

        target = tmp_path / "local.pdf"
        target.write_bytes(FAKE_PDF)
        settings.allowed_file_paths = str(tmp_path)

        resp = client.post(
            "/probe",
            json={"files": [{"filename": "", "file_path": str(target)}]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 没给 filename 时从路径推导
        assert body["names"] == ["local.pdf"]

    def test_mixed_sources_in_one_array(self, server, tmp_path):
        """同一批里可以混用三种来源。"""

        from fapiao_cn.config import settings

        target = tmp_path / "local.pdf"
        target.write_bytes(FAKE_PDF)
        settings.allowed_file_paths = str(tmp_path)
        payload = base64.b64encode(FAKE_PDF).decode()

        resp = client.post(
            "/probe",
            json={
                "files": [
                    {"filename": "a.pdf", "url": f"{server}/invoice.pdf"},
                    {"filename": "b.pdf", "file_path": str(target)},
                    {"filename": "c.pdf", "base64": payload},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["names"] == ["a.pdf", "b.pdf", "c.pdf"]

    def test_top_level_url_shortcut(self, server):
        """顶层 ``url`` —— 与 /invoice 的键名一致，等价于一份文件的批量。"""

        resp = client.post("/probe", json={"url": f"{server}/invoice.pdf"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == 1
        assert body["names"] == ["invoice.pdf"]

    def test_top_level_url_with_files_is_rejected(self, server):
        """顶层 url 与 files 数组同时给 → 报错，不再静默忽略。

        过去的行为是「返回 200，但顶层 url 那份文件根本没进来」——
        调用方会以为文件已经处理过了。
        """

        payload = base64.b64encode(FAKE_PDF).decode()
        resp = client.post(
            "/probe",
            json={
                "files": [{"filename": "a.pdf", "base64": payload}],
                "url": f"{server}/invoice.pdf",
            },
        )
        assert resp.status_code == 400
        assert "not both" in resp.json()["error"]

    def test_item_without_any_source(self):
        resp = client.post("/probe", json={"files": [{"filename": "bad.pdf"}]})
        assert resp.status_code == 400
        detail = resp.json()["error"]
        assert "Missing" in detail
        assert "url" in detail and "file_path" in detail and "base64" in detail

    def test_invalid_base64(self):
        resp = client.post(
            "/probe",
            json={"files": [{"filename": "bad.pdf", "base64": "not-valid-base64!!!"}]},
        )
        assert resp.status_code == 400
        assert "Invalid base64" in resp.json()["error"]

    def test_files_not_an_array(self):
        resp = client.post("/probe", json={"files": "nope"})
        assert resp.status_code == 400
        assert "array" in resp.json()["error"]


# ── 失败路径与防护 ───────────────────────────────────────────────────


class TestBatchInputGuards:
    def test_unreachable_url_is_reported(self, server):
        """远端 404 → 明确报「抓取失败」，不是含糊的 500。"""

        resp = client.post(
            "/probe",
            json={"files": [{"filename": "missing.pdf", "url": f"{server}/nope.pdf"}]},
        )
        assert resp.status_code == 400
        assert "Failed to fetch URL" in resp.json()["error"]

    def test_metadata_address_is_blocked(self):
        """云元数据地址一律拒绝，且不去真连。"""

        resp = client.post("/probe", json={"url": METADATA_URL})
        assert resp.status_code == 400
        assert "Failed to fetch URL" in resp.json()["error"]

    def test_non_http_scheme_is_rejected(self):
        resp = client.post("/probe", json={"url": "file:///etc/passwd"})
        assert resp.status_code == 400
        assert "Failed to fetch URL" in resp.json()["error"]

    def test_file_path_outside_whitelist(self, tmp_path):
        """白名单外的路径 —— 读不到，且不透露「文件存不存在」。"""

        from fapiao_cn.config import settings

        outside = tmp_path / "outside.pdf"
        outside.write_bytes(FAKE_PDF)
        settings.allowed_file_paths = str(tmp_path / "nowhere")

        resp = client.post("/probe", json={"files": [{"file_path": str(outside)}]})
        assert resp.status_code == 400
        assert "Unable to read file_path" in resp.json()["error"]

    def test_file_path_disabled_when_whitelist_empty(self, tmp_path):
        """白名单为空 → file_path 输入整体关闭。"""

        from fapiao_cn.config import settings

        target = tmp_path / "local.pdf"
        target.write_bytes(FAKE_PDF)
        settings.allowed_file_paths = ""

        resp = client.post("/probe", json={"files": [{"file_path": str(target)}]})
        assert resp.status_code == 400
        assert "Unable to read file_path" in resp.json()["error"]

    def test_too_many_urls_rejected_without_downloading(self, server, hits):
        """文件数闸门在抓取之前 —— 超限时一个 URL 都不该去下载。"""

        resp = client.post(
            "/probe",
            json={
                "files": [
                    {"filename": f"{i}.pdf", "url": f"{server}/invoice.pdf"}
                    for i in range(51)
                ]
            },
        )
        assert resp.status_code == 400
        assert "Too many files" in resp.json()["error"]
        assert hits == [], f"超限时不应发起任何下载，实际请求了 {len(hits)} 次"

    def test_single_file_size_limit(self, monkeypatch):
        """单文件上限与 /invoice 共用 FC_MAX_FILE_MB。"""

        from fapiao_cn.config import settings

        monkeypatch.setattr(settings, "max_file_mb", 1)
        big = b"x" * (2 * 1024 * 1024)
        resp = client.post(
            "/probe",
            files=[("files", ("big.pdf", io.BytesIO(big), "application/pdf"))],
        )
        assert resp.status_code == 400
        assert "File too large" in resp.json()["error"]

    def test_base64_pre_decode_size_guard(self, monkeypatch):
        """base64 走的是**解码前**的体积预检 —— 超限直接拒，不先展开内存。"""

        from fapiao_cn.config import settings

        monkeypatch.setattr(settings, "max_file_mb", 1)
        big = b"x" * (2 * 1024 * 1024)
        payload = base64.b64encode(big).decode()
        resp = client.post(
            "/probe",
            json={"files": [{"filename": "big.pdf", "base64": payload}]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "Invalid base64 for file big.pdf"

    def test_total_size_limit(self, monkeypatch):
        from fapiao_cn.config import settings

        monkeypatch.setattr(settings, "max_batch_total_mb", 1)
        chunk = base64.b64encode(b"x" * (512 * 1024)).decode()
        resp = client.post(
            "/probe",
            json={"files": [{"filename": f"{i}.pdf", "base64": chunk} for i in range(3)]},
        )
        assert resp.status_code == 400
        assert "Total size too large" in resp.json()["error"]
