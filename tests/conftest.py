"""Shared pytest fixtures & parametrisation data."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

# H10/H47: prime FC_API_KEYS at conftest-load time so that
# ``from fapiao_cn.server import app`` at test-module top-level sees a
# configured KeyStore instead of failing fast on dev-fallback.  These
# are set BEFORE any fapiao_cn import below.
os.environ.setdefault(
    "FC_API_KEYS",
    "test-key-default-do-not-use-in-prod,test-key-extract-only-do-not-use-in-prod",
)
os.environ.setdefault("FC_API_KEYS_FILE", "")
os.environ.setdefault("FC_REQUIRE_CONFIGURED_KEYS", "false")

# H37: hard guard against accidentally pointing the test suite at a
# real keys file.  The conftest primes test keys via env above, so any
# non-empty FC_API_KEYS_FILE at this point means an operator forgot
# to unset it (and we would silently start using the real prod keys).
if os.environ.get("FC_API_KEYS_FILE"):
    raise RuntimeError(
        "FC_API_KEYS_FILE must be empty during tests "
        f"(found {os.environ['FC_API_KEYS_FILE']!r}); unset it before running pytest"
    )

import pytest  # noqa: E402

from fapiao_cn.auth import (  # noqa: E402
    reset_key_store_for_tests,
    reset_rate_limiter_for_tests,
)
from fapiao_cn.config import settings  # noqa: E402
from fapiao_cn.parsers.invoice import (  # noqa: E402
    clear_company_tax_id_cache,
    record_company_tax_id,
)

# 默认测试用 key；通过 env 注入，确保 KeyStore 在 simple mode 下也能加载
# TEST_API_KEY 是 admin 作用域（拿到 extract/verify/admin），覆盖 verify 测试
# TEST_EXTRACT_KEY 仅 extract 作用域，用来验证 scope 失败 → 403
TEST_API_KEY = "test-key-default-do-not-use-in-prod"
TEST_EXTRACT_KEY = "test-key-extract-only-do-not-use-in-prod"

# Map of filename → expected fields (partial).  We only check the
# fields that are stable and well-defined for each file.
EXPECTED: Dict[str, dict] = {
    "购_材料_01.pdf": dict(
        invoice_type="电子发票（增值税专用发票）",
        invoice_number="25502000000129241041",
        issue_date="2025-11-27",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="重庆铜睿机械有限公司",
        seller_tax_id="91500109339665941Q",
        amount_without_tax="124018.66",
        tax_amount="16122.43",
        total_amount="140141.09",
        total_amount_upper="壹拾肆万零壹佰肆拾壹圆零玖分",
        drawer="李春梅",
    ),
    "购_材料_02.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="14403260911012477509",
        issue_date="2026-05-29",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="深圳市福田区华之星办公设备经营部",
        seller_tax_id="92440300MA5F18Y894",
        amount_without_tax="257.43",
        tax_amount="2.57",
        total_amount="260.00",
        total_amount_upper="贰佰陆拾元整",
        drawer="蓝桂涛",
    ),
    "购_餐饮_02.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26442000001385046691",
        issue_date="2026-02-04",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="广州市增城亮平餐厅",
        seller_tax_id="92440101MA5CC9HG62",
        amount_without_tax="988.12",
        tax_amount="9.88",
        total_amount="998.00",
        total_amount_upper="玖佰玖拾捌圆整",
        drawer="吕亮平",
    ),
    "购_餐饮_03.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26317000001644463587",
        issue_date="2026-05-14",
        buyer_name="重庆市旺成科技股份有限公司",
        # The PDF omits both tax ids; the parser should fall back to
        # the (name -> tax_id) cache populated by other invoices.
        buyer_tax_id="91500106622015389N",
        seller_name="杭州宜家家居有限公司上海分公司",
        seller_tax_id="",
        amount_without_tax="37.73",
        tax_amount="2.26",
        total_amount="39.99",
        total_amount_upper="叁拾玖圆玖角玖分",
        payee="徐洁敏",
        reviewer="陈晨",
        drawer="汪兴平",
    ),
    "购_材料_03.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26322000001880018386",
        issue_date="2026-03-11",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="兴化市奥超不锈钢制品厂",
        seller_tax_id="92321281MA20G2JUXK",
        amount_without_tax="355.45",
        tax_amount="3.55",
        total_amount="359.00",
        total_amount_upper="叁佰伍拾玖圆整",
        drawer="张宇松",
    ),
    "购_自来水_01.pdf": dict(
        invoice_type="电子发票（增值税专用发票）",
        invoice_number="26502000001100907556",
        issue_date="2026-06-08",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="重庆水资源产业股份有限公司西城自来水分公司",
        seller_tax_id="91500107MA61QQQ4XP",
        amount_without_tax="14860.55",
        tax_amount="1337.45",
        total_amount="16198.00",
        total_amount_upper="壹万陆仟壹佰玖拾捌圆整",
        drawer="高玉玲",
    ),
    "购_租赁_01.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26137907690500263398",
        issue_date="2026-05-20",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="河北京石高速公路开发有限公司",
        seller_tax_id="9113000071788558XQ",
        amount_without_tax="27.69",
        tax_amount="0.83",
        total_amount="28.52",
        total_amount_upper="贰拾捌圆伍角贰分",
        drawer="田克",
    ),
    "购_油品_01.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="05000170021193369231",
        issue_date="2026-05-20",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="重庆中油红綦能源发展有限公司",
        seller_tax_id="91500222MA5XDW8PX6",
        amount_without_tax="284.07",
        tax_amount="36.93",
        total_amount="321.00",
        total_amount_upper="叁佰贰拾壹圆整",
        payee="张成蜀",
        reviewer="王瑜",
        drawer="张丽艺",
    ),
    "购_油品_03.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="05000170021193369673",
        issue_date="2026-06-11",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="重庆中油红綦能源发展有限公司",
        seller_tax_id="91500222MA5XDW8PX6",
        amount_without_tax="230.09",
        tax_amount="29.91",
        total_amount="260.00",
        total_amount_upper="贰佰陆拾圆整",
        payee="张成蜀",
        reviewer="王瑜",
        drawer="张丽艺",
    ),
    "购_维修_01.pdf": dict(
        invoice_type="电子发票（增值税专用发票）",
        invoice_number="26502000000235158361",
        issue_date="2026-02-06",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="九龙坡区九龙园区永创特机电设备销售部",
        seller_tax_id="92500107MA5UM6EL06",
        amount_without_tax="4950.50",
        tax_amount="49.50",
        total_amount="5000.00",
        total_amount_upper="伍仟圆整",
        drawer="岳维霄",
    ),
    "购_通信_01.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26507000000252287129",
        issue_date="2026-08-02",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="中国联合网络通信有限公司重庆市分公司",
        seller_tax_id="91500000709375352T",
        # 该发票为免税发票（合计行税额位置为 *），因此税额为 0。
        amount_without_tax="358.00",
        tax_amount="0.00",
        total_amount="358.00",
        total_amount_upper="叁佰伍拾捌圆整",
    ),
    "京东大药房-01.pdf": dict(
        # 京东大药房变体：表头/标签字符在 pdfplumber 输出中会被连续
        # 重复 3 次落字，由 pipeline._clean_text 折叠为单字后解析。
        invoice_type="电子发票（普通发票）",
        invoice_number="26507000000257623006",
        issue_date="2026-08-12",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="京东大药房重庆有限公司",
        seller_tax_id="91500108MA60K9RN12",
        amount_without_tax="181.81",
        tax_amount="23.63",
        total_amount="205.44",
        total_amount_upper="贰佰零伍圆肆角肆分",
        drawer="韦鸿捷",
    ),
    "购_滴滴_01.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26507000000214328984",
        issue_date="2026-06-23",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="重庆滴滴出行科技有限公司",
        seller_tax_id="91500105MA60P4HH22",
        amount_without_tax="125.93",
        tax_amount="3.77",
        total_amount="129.70",
        total_amount_upper="壹佰贰拾玖圆柒角整",
        drawer="赵笑林",
    ),
    "购_滴滴_02.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26507000000143070934",
        issue_date="2026-04-14",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="杭州携华网络科技有限公司重庆分公司",
        seller_tax_id="91500107MA60A09775",
        amount_without_tax="44.95",
        tax_amount="1.35",
        total_amount="46.30",
        total_amount_upper="肆拾陆元叁角",
        drawer="马苓苓",
    ),
    "个人代开_01.pdf": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26502000001222344391",
        issue_date="2026-06-20",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="颜国明",
        seller_tax_id="92500106MA5XUBM0XC",
        amount_without_tax="643.56",
        tax_amount="6.44",
        total_amount="650.00",
        total_amount_upper="陆佰伍拾圆整",
        drawer="颜俊",
    ),
    "专利_01.pdf": dict(
        invoice_type="中央非税收入统一票据（电子）",
        invoice_number="0012090205",
        issue_date="2026-06-29",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="国家知识产权局专利局",
        seller_tax_id="",
        amount_without_tax="135.00",
        total_amount="135.00",
        total_amount_upper="壹佰叁拾伍元整",
        payee="胡瀛",
        reviewer="王舒",
    ),
    "航_运输_01.pdf": dict(
        invoice_type="电子发票（航空运输电子客票行程单）",
        invoice_number="26118999111039155396",
        issue_date="2026-06-02",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="重庆市联丰航空服务有限公司",
        seller_tax_id="PEK888/08385241",
        amount_without_tax="664.68",
        tax_amount="55.32",
        total_amount="720.00",
    ),
    "航_运输_02.pdf": dict(
        invoice_type="电子发票（航空运输电子客票行程单）",
        invoice_number="26448784110007039749",
        issue_date="2026-08-04",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="重庆市联丰航空服务有限公司",
        seller_tax_id="CAN011/08310190",
        amount_without_tax="692.20",
        tax_amount="57.80",
        total_amount="750.00",
    ),
    "火车票01.pdf": dict(
        invoice_type="电子发票（铁路电子客票）",
        invoice_number="26319166100004714993",
        issue_date="2026-04-18",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="中国铁路",
        seller_tax_id="12306",
        amount_without_tax="37.00",
        tax_amount="0.00",
        total_amount="37.00",
    ),
    "火车票02.pdf": dict(
        invoice_type="电子发票（铁路电子客票）",
        invoice_number="26319166100005032784",
        issue_date="2026-04-24",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="中国铁路",
        seller_tax_id="12306",
        amount_without_tax="37.00",
        tax_amount="0.00",
        total_amount="37.00",
    ),
    # ── XML e-invoices (全电发票) ──
    "购_材料_01.xml": dict(
        invoice_type="电子发票（增值税专用发票）",
        invoice_number="25502000000129241041",
        issue_date="2025-11-27",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="重庆铜睿机械有限公司",
        seller_tax_id="91500109339665941Q",
        amount_without_tax="124018.66",
        tax_amount="16122.43",
        total_amount="140141.09",
        total_amount_upper="壹拾肆万零壹佰肆拾壹圆零玖分",
        drawer="李春梅",
    ),
    "购_油品_01.xml": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26507000000185284098",
        issue_date="2026-06-02",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="中国石化销售股份有限公司重庆江南石油分公司",
        seller_tax_id="915000005828170525",
        amount_without_tax="2665.17",
        tax_amount="346.47",
        total_amount="3011.64",
        total_amount_upper="叁仟零壹拾壹圆陆角肆分",
        drawer="邹玮",
    ),
    "购_油品_02.xml": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26507000000186658007",
        issue_date="2026-06-02",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="中国石油天然气股份有限公司重庆渝中销售分公司",
        seller_tax_id="915001077530778896",
        amount_without_tax="830.27",
        tax_amount="107.94",
        total_amount="938.21",
        total_amount_upper="玖佰叁拾捌圆贰角壹分",
        drawer="叶南",
    ),
    "购_零售_01.xml": dict(
        invoice_type="电子发票（普通发票）",
        invoice_number="26502000001147644976",
        issue_date="2026-06-13",
        buyer_name="重庆市旺成科技股份有限公司",
        buyer_tax_id="91500106622015389N",
        seller_name="新焙饮食文化重庆市有限公司",
        seller_tax_id="91500108MAABPBW56K",
        amount_without_tax="4516.51",
        tax_amount="45.17",
        total_amount="4561.68",
        total_amount_upper="肆仟伍佰陆拾壹圆陆角捌分",
        drawer="龙艳",
    ),
    "销_产品_01.xml": dict(
        invoice_type="电子发票（增值税专用发票）",
        invoice_number="25502000000126087334",
        issue_date="2025-11-21",
        buyer_name="新大洲本田摩托（苏州）有限公司",
        buyer_tax_id="91320585MA1T6M1R5F",
        seller_name="重庆市旺成科技股份有限公司",
        seller_tax_id="91500106622015389N",
        amount_without_tax="5340673.44",
        tax_amount="694287.61",
        total_amount="6034961.05",
        total_amount_upper="陆佰零叁万肆仟玖佰陆拾壹圆零伍分",
        drawer="周妍伶",
    ),
}


@pytest.fixture(autouse=True)
def _seed_company_tax_id_cache():
    """Pre-populate the in-process (name -> tax_id) cache so tests do
    not depend on parametrisation order.

    购_餐饮_03.pdf prints the buyer name but no tax id; the parser is
    expected to fall back to this cache for the missing value.  We seed
    the recurring buyer once per test rather than relying on a sibling
    test having run first.
    """

    clear_company_tax_id_cache()
    record_company_tax_id("重庆市旺成科技股份有限公司", "91500106622015389N")
    yield
    clear_company_tax_id_cache()


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    """Inject default API keys for every test and reset singletons.

    KeyStore / RateLimiter 都是进程级单例；测试间必须重建才能看到 env 变化。
    pydantic-settings 不会缓存 env，但 ``Settings()`` 字段值在模型构造时已
    固化为实例属性——直接 set 回去更直接。
    """

    # H1 LFI 防护默认禁用 file_path 输入；测试套件需要读取仓库根目录的
    # samples/，故在 fixture 期间显式打开白名单。涉及 file_path 安全检查的
    # 专项测试会临时清空该值以验证「白名单为空时全部拒绝」的语义。
    samples_root = str((Path(__file__).resolve().parent.parent / "samples").resolve())
    saved = {
        "api_auth_enabled": settings.api_auth_enabled,
        "api_keys": settings.api_keys,
        "api_keys_file": settings.api_keys_file,
        "api_legacy_allow": settings.api_legacy_allow,
        "allowed_file_paths": settings.allowed_file_paths,
        "require_configured_keys": settings.require_configured_keys,
    }
    settings.api_auth_enabled = True
    settings.api_keys = f"{TEST_API_KEY},{TEST_EXTRACT_KEY}"
    settings.api_keys_file = ""
    settings.api_legacy_allow = False
    settings.allowed_file_paths = samples_root
    settings.require_configured_keys = False  # 测试 fixture 显式 opt-out
    reset_key_store_for_tests()
    reset_rate_limiter_for_tests()
    yield
    for k, v in saved.items():
        setattr(settings, k, v)
    reset_key_store_for_tests()
    reset_rate_limiter_for_tests()


@pytest.fixture
def auth_headers():
    """默认测试请求 header（admin 作用域，覆盖 extract + verify + admin）。"""

    return {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
def extract_only_headers():
    """仅 extract 作用域：simple mode 默认全 scope → 这个 key 也是全 scope。

    若需要「仅 extract」的真实场景，使用 ``keys_file_env`` fixture。
    """

    return {"Authorization": f"Bearer {TEST_EXTRACT_KEY}"}


@pytest.fixture
def keys_file_env(tmp_path):
    """结构化 JSON 模式：可注入不同 scope 的多把 key。

    返回 ``(json_path, keys_by_label)``；调用方按 label 取 key。
    """

    payload = [
        {"key": "key-extract-only", "label": "extract-only", "scopes": ["extract"]},
        {"key": "key-verify-only", "label": "verify-only", "scopes": ["verify"]},
        {"key": "key-admin", "label": "admin", "scopes": ["extract", "verify", "admin"]},
        {"key": "key-rate1", "label": "rate1", "scopes": ["extract"], "rpm": 2, "burst": 2},
    ]
    p = tmp_path / "keys.json"
    p.write_text(__import__("json").dumps(payload), encoding="utf-8")
    saved_keys = settings.api_keys
    saved_file = settings.api_keys_file
    settings.api_keys = ""
    settings.api_keys_file = str(p)
    reset_key_store_for_tests()
    reset_rate_limiter_for_tests()
    yield {
        "extract_only": {"Authorization": "Bearer key-extract-only"},
        "verify_only": {"Authorization": "Bearer key-verify-only"},
        "admin": {"Authorization": "Bearer key-admin"},
        "rate1": {"Authorization": "Bearer key-rate1"},
    }
    settings.api_keys = saved_keys
    settings.api_keys_file = saved_file
    reset_key_store_for_tests()
    reset_rate_limiter_for_tests()


def pytest_generate_tests(metafunc):
    """Parametrize ``filename`` and ``expected`` for each test file."""
    if "filename" in metafunc.fixturenames and "expected" in metafunc.fixturenames:
        items = [(f, EXPECTED.get(f, {})) for f in sorted(EXPECTED)]
        metafunc.parametrize("filename,expected", items)
