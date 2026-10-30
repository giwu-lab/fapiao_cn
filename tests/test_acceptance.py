"""电子银行承兑汇票端点的端到端测试。

三条不变量：

1. **两版路由表一致** —— ``/acceptance`` 与 ``/acceptance/batch`` 在开源版
   里也存在，未授权时返回 403 + 结构化 body（而不是 404）；
2. **鉴权优先于版本判定** —— 没有 key 先 401；
3. **解析实现随企业版发行包发布** —— 开源版跑不到真实解析，用
   ``ee_feature_enabled("acceptance")`` 门控。

真实样本在 ``samples/电子承兑/``，共 4 家银行的模板。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fapiao_cn.server import app
from tests._helpers import ee_feature_enabled

client = TestClient(app)

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples" / "电子承兑"

#: 4 家银行模板的样本（文件名 → 期望的关键要素）。
SAMPLES = {
    "电子银行承兑汇票 (1).pdf": {
        "bill_no": "510434545293120261027102176810000807400001-000816800000",
        "bill_status": "已收票-可流通",
        "issue_date": "2026-10-27",
        "due_date": "2027-04-27",
        "acceptance_date": "2026-10-27",
        "amount": "94000.00",
        "amount_upper": "玖万肆仟元整",
        "currency": "人民币",
        "drawer_name": "浙江钱江摩托股份有限公司",
        "drawer_account": "396158352033",
        "drawer_bank": "中国银行股份有限公司温岭支行",
        "payee_name": "浙江美可达摩托车有限公司",
        "payee_account": "1207041109041000330",
        "payee_bank": "中国工商银行股份有限公司温岭支行",
        "acceptor_name": "中国银行股份有限公司温岭支行",
        "acceptor_account": "0",
        "acceptor_bank_name": "中国银行股份有限公司温岭支行",
        "acceptor_bank_no": "104345452931",
        "contract_no": "",
        "transferable": "可再转让-可分包",
        "display_date": "2026-10-29 08:26",
        "endorsements": 1,
    },
    "电子银行承兑汇票 (2).pdf": {
        "bill_no": "531329001004520261012000772882000000000001-000012347238",
        "bill_status": "已收票-可流通",
        "issue_date": "2026-10-12",
        "due_date": "2027-04-12",
        "acceptance_date": "2026-10-12",
        "amount": "123472.38",
        "amount_upper": "壹拾贰万叁仟肆佰柒拾贰元叁角捌分",
        "currency": "人民币",
        "drawer_name": "德瑞泰克汽车系统（上海）有限公司",
        "drawer_account": "86020200808081500",
        "drawer_bank": "宁波银行股份有限公司上海闵行支行",
        "payee_name": "重庆市旺成科技股份有限公司",
        "payee_account": "0701020120010007511",
        "payee_bank": "重庆农村商业银行股份有限公司北碚支行",
        "acceptor_name": "宁波银行股份有限公司上海闵行支行",
        "acceptor_account": "0",
        "acceptor_bank_name": "宁波银行股份有限公司上海闵行支行",
        "acceptor_bank_no": "313290010045",
        "contract_no": "",
        "transferable": "可再转让-可分包",
        "display_date": "2026-10-26 17:02",
        "endorsements": 1,
    },
    "电子银行承兑汇票 (3).pdf": {
        "bill_no": "530939300995720260928104219133000209606456-000223606455",
        "bill_status": "已收票-可流通",
        "issue_date": "2026-09-28",
        "due_date": "2027-03-28",
        "acceptance_date": "2026-09-28",
        "amount": "140000.00",
        "amount_upper": "壹拾肆万元整",
        "currency": "人民币",
        "drawer_name": "厦门厦杏摩托有限公司",
        "drawer_account": "129950130400009579",
        "drawer_bank": "兴业银行股份有限公司厦门杏林支行",
        "payee_name": "江苏罡阳股份有限公司",
        "payee_account": "1115020119000224984",
        "payee_bank": "中国工商银行股份有限公司泰州分行",
        "acceptor_name": "兴业银行股份有限公司厦门分行专业处理中心",
        "acceptor_account": "0",
        "acceptor_bank_name": "兴业银行股份有限公司厦门分行专业处理中心",
        "acceptor_bank_no": "309393009908",
        "contract_no": "",
        "transferable": "可再转让-可分包",
        "display_date": "2026-10-26 17:03",
        "endorsements": 2,
    },
    "电子银行承兑汇票 (4).pdf": {
        "bill_no": "530758100901320261019100939304000006704999-000009586937",
        "bill_status": "已收票-可流通",
        "issue_date": "2026-10-19",
        "due_date": "2027-04-19",
        "acceptance_date": "2026-10-19",
        "amount": "28819.39",
        "amount_upper": "贰万捌仟捌佰壹拾玖元叁角玖分",
        "currency": "人民币",
        "drawer_name": "海南丰亚汽车销售服务有限公司",
        "drawer_account": "15000106806287",
        "drawer_bank": "平安银行股份有限公司广州分行营业部",
        "payee_name": "长城汽车股份有限公司重庆销售分公司",
        "payee_account": "15401555130041",
        "payee_bank": "平安银行股份有限公司广州分行营业部",
        "acceptor_name": "平安银行股份有限公司广州分行",
        "acceptor_account": "0",
        "acceptor_bank_name": "平安银行股份有限公司广州分行",
        "acceptor_bank_no": "307581009005",
        "contract_no": "",
        "transferable": "可再转让-可分包",
        "display_date": "2026-10-29 08:26",
        "endorsements": 3,
    },
}


def _sample(name: str) -> bytes:
    path = SAMPLE_DIR / name
    if not path.exists():
        pytest.skip(f"sample not available: {name}")
    return path.read_bytes()


class TestRoutes:
    def test_routes_exist_in_both_editions(self):
        """两版路由表一致 —— 客户端不会因为版本差异拿到 404。"""
        paths = {route.path for route in app.routes}
        assert "/acceptance" in paths
        assert "/acceptance/batch" in paths

    def test_openapi_marks_enterprise_paths(self):
        schema = app.openapi()
        for path in ("/acceptance", "/acceptance/batch"):
            assert path in schema["paths"]
            assert schema["paths"][path]["post"]["x-required-edition"] == "ee"

    def test_unauthenticated_gets_401_not_403(self):
        """鉴权优先：没有 key 先 401，不泄漏版本信息。"""
        resp = client.post("/acceptance", files=[("file", ("x.pdf", b"%PDF-1.4"))])
        assert resp.status_code == 401
        resp = client.post(
            "/acceptance/batch", files=[("files", ("x.pdf", b"%PDF-1.4"))]
        )
        assert resp.status_code == 401

    def test_format_dictionary_is_available(self, auth_headers):
        """字段字典两版都有 —— 前端据此渲染界面。"""
        resp = client.get("/formats/acceptance", headers=auth_headers)
        assert resp.status_code == 200
        fields = resp.json()["fields"]
        names = [f["name"] for f in fields]
        assert "bill_no" in names
        assert "endorsements" in names
        for f in fields:
            assert set(f) == {"name", "type", "description"}


class TestGating:
    def test_acceptance_endpoint_gated(self, auth_headers):
        resp = client.post(
            "/acceptance",
            headers=auth_headers,
            files=[("file", ("x.pdf", b"%PDF-1.4"))],
        )
        if ee_feature_enabled("acceptance"):
            # 已授权 —— 走到解析阶段，失败原因不是 403
            assert resp.status_code != 403
        else:
            assert resp.status_code == 403
            detail = resp.json()["detail"]
            assert detail["error"] == "feature_not_available"
            assert detail["feature"] == "acceptance"
            assert detail["required_edition"] == "ee"

    def test_acceptance_batch_endpoint_gated(self, auth_headers):
        resp = client.post(
            "/acceptance/batch",
            headers=auth_headers,
            files=[("files", ("x.pdf", b"%PDF-1.4"))],
        )
        if ee_feature_enabled("acceptance"):
            assert resp.status_code != 403
        else:
            assert resp.status_code == 403
            detail = resp.json()["detail"]
            assert detail["feature"] == "acceptance"


@pytest.mark.skipif(
    not ee_feature_enabled("acceptance"),
    reason="acceptance capability requires an enterprise license with the acceptance feature",
)
class TestParseRealSamples:
    @pytest.mark.parametrize("name", sorted(SAMPLES))
    def test_single_file(self, auth_headers, name):
        expected = SAMPLES[name]
        resp = client.post(
            "/acceptance",
            headers=auth_headers,
            files=[("file", (name, _sample(name)))],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "success"
        assert data["bill_type"] == "电子银行承兑汇票"
        assert data["page_count"] == 2
        assert data["file"] == name
        for field, value in expected.items():
            if field == "endorsements":
                continue
            assert data[field] == value, f"{name}: {field}"

    @pytest.mark.parametrize("name", sorted(SAMPLES))
    def test_endorsements_are_present(self, auth_headers, name):
        expected = SAMPLES[name]
        resp = client.post(
            "/acceptance",
            headers=auth_headers,
            files=[("file", (name, _sample(name)))],
        )
        assert resp.status_code == 200
        endorsements = resp.json()["endorsements"]
        assert len(endorsements) == expected["endorsements"]
        for e in endorsements:
            assert e["endorser"]
            assert e["endorsee"]
            assert e["endorsement_date"]
            assert e["no_transfer"]

    def test_batch_endpoint(self, auth_headers):
        names = sorted(SAMPLES)
        files = [("files", (n, _sample(n))) for n in names]
        resp = client.post("/acceptance/batch", headers=auth_headers, files=files)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == len(names)
        assert [d["file"] for d in data] == names
        for item in data:
            assert item["message"] == "success"
            assert item["bill_no"]

    def test_json_body_with_base64(self, auth_headers):
        import base64

        name = "电子银行承兑汇票 (1).pdf"
        payload = {
            "file_base64": base64.b64encode(_sample(name)).decode("ascii"),
        }
        resp = client.post("/acceptance", headers=auth_headers, json=payload)
        assert resp.status_code == 200
        assert resp.json()["bill_no"] == SAMPLES[name]["bill_no"]

    def test_non_acceptance_pdf_returns_422(self, auth_headers):
        """不是电子承兑票 → 422，而不是一堆空字段。"""
        sample = (
            Path(__file__).resolve().parent.parent
            / "samples"
            / "报关单"
            / "L20260602000010_0429.pdf"
        )
        if not sample.exists():
            pytest.skip("sample not available")
        resp = client.post(
            "/acceptance",
            headers=auth_headers,
            files=[("file", ("bgd.pdf", sample.read_bytes()))],
        )
        assert resp.status_code == 422
        assert "acceptance" in str(resp.json()["detail"])

    def test_acceptance_pdf_on_invoice_endpoint_returns_422(self, auth_headers):
        """误投 ``/invoice`` → 422 并指明该走 ``/acceptance``（不能静默吞字段）。"""
        name = "电子银行承兑汇票 (1).pdf"
        resp = client.post(
            "/invoice",
            headers=auth_headers,
            files=[("file", (name, _sample(name)))],
        )
        assert resp.status_code == 422
        message = resp.json()["message"]
        assert message.startswith("error:")
        assert "/acceptance" in message

    def test_acceptance_pdf_on_invoice_batch_is_reported_per_item(self, auth_headers):
        """批量端点按设计返回 200，错误落在**该项**的 message 上。"""
        name = "电子银行承兑汇票 (1).pdf"
        resp = client.post(
            "/invoice/batch",
            headers=auth_headers,
            files=[("files", (name, _sample(name)))],
        )
        assert resp.status_code == 200
        item = resp.json()[0]
        assert item["message"].startswith("error:")
        assert "/acceptance" in item["message"]


class TestParserRegistry:
    def test_acceptance_rule_follows_entitlement(self):
        """电子承兑规则只在被授权时注册 —— 否则 /formats 会泄漏能力。"""
        from fapiao_cn.parsers import _RULES, get_format_descriptions

        names = {name for name, _, _ in _RULES}
        if ee_feature_enabled("acceptance"):
            assert "acceptance_bill" in names
            assert "acceptance_bill" in get_format_descriptions()
        else:
            assert "acceptance_bill" not in names
            assert "acceptance_bill" not in get_format_descriptions()
