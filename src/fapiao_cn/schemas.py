"""Pydantic schemas for the einvoice API.

Field names form the stable wire contract across PDF / XML / batch
inputs; downstream consumers parse one shape regardless of source.
"""

from __future__ import annotations

import re
from typing import List

from pydantic import BaseModel, Field

# Pattern used to normalise whitespace inside extracted field values.
_WHITESPACE_RE = re.compile(r"\s+")


class InvoiceItem(BaseModel):
    """全电发票商品明细（一行）。

    字段名 → ``EInvoice`` schema 元素对应关系::

        name                      -> ItemName              （含 ``*类别*`` 前缀）
        category                  -> 从 ItemName 的 ``*类别*`` 段拆分
        spec                      -> SpecMod
        unit                      -> MeaUnits
        quantity                  -> Quantity
        unit_price                -> UnPrice
        amount                    -> Amount                （不含税金额）
        tax_rate                  -> TaxRate
        tax_amount                -> ComTaxAm
        total_amount              -> TotaltaxIncludedAmount（价税合计）
        tax_classification_code   -> TaxClassificationCode （19 位税收分类编码）

    全部字段保持 ``str`` 原值：红字冲销行带负号，且部分票种的税率写作
    ``0.13``、数量可能是 ``4333千克`` 这类带单位的串，转 float 会丢信息。

    ``is_reversal`` 不来自 XML，而是解析器依据负金额推导——红字冲销行在
    报税时要单独统计，调用方不应把它当普通商品行累加。

    H43: 字段名与国家税务总局 ``EInvoice`` schema 硬编码对应，schema 升级
    需要同步本文件。
    """

    item_no: int = 0
    name: str = ""
    category: str = ""
    spec: str = ""
    unit: str = ""
    quantity: str = ""
    unit_price: str = ""
    amount: str = ""
    tax_rate: str = ""
    tax_amount: str = ""
    total_amount: str = ""
    tax_classification_code: str = ""
    is_reversal: bool = False


class InvoiceRecord(BaseModel):
    file: str = ""
    page_count: int = 0
    invoice_type: str = ""
    invoice_number: str = ""
    issue_date: str = ""
    buyer_name: str = ""
    buyer_tax_id: str = ""
    seller_name: str = ""
    seller_tax_id: str = ""
    amount_without_tax: str = ""
    tax_amount: str = ""
    total_amount: str = ""
    total_amount_upper: str = ""
    payee: str = ""
    reviewer: str = ""
    drawer: str = ""
    # 商品明细。仅 XML 全电发票填充；PDF / OFD 恒为空数组
    # （OFD 暂不解析明细，见 README）。
    items: List[InvoiceItem] = Field(default_factory=list)
    # 消费类别（由 expense_category.classify_expense 写入；空表示未命中或总开关关闭）。
    # 字段说明：
    #     expense_category        —— 枚举字符串（transportation/lodging/.../other）
    #     expense_category_label  —— 中文显示（交通/住宿/.../其他）
    #     expense_category_source —— 命中层级："ticket" | "seller" | "item" | "code"
    expense_category: str = ""
    expense_category_label: str = ""
    expense_category_source: str = ""

    def stripped(self) -> "InvoiceRecord":
        """Return a copy with all whitespace removed from string fields."""

        # ``page_count`` is an int and must round-trip verbatim; the
        # rest are strings that get their whitespace stripped.  ``items``
        # is a list of models and passes through untouched.
        data = self.model_dump()
        for k, v in data.items():
            if isinstance(v, str):
                data[k] = _WHITESPACE_RE.sub("", v)
        return InvoiceRecord(**data)

    def with_expense_category(
        self, parser_name: str = ""
    ) -> "InvoiceRecord":
        """Return a copy with ``expense_category*`` fields populated.

        Imports are kept local to avoid a top-level cycle (expense_category
        imports ``InvoiceItem``).  The classifier respects the
        ``FC_EXPENSE_CATEGORY_ENABLED`` config; when disabled the three
        fields stay empty strings.
        """

        from fapiao_cn.config import settings
        from fapiao_cn.expense_category import classify_expense

        cat, label, source = classify_expense(
            self,
            parser_name=parser_name,
            enabled=settings.expense_category_enabled,
        )
        data = self.model_dump()
        data["expense_category"] = cat
        data["expense_category_label"] = label
        data["expense_category_source"] = source
        return InvoiceRecord(**data)


class PdfRaw(BaseModel):
    text: str = ""
    lines: List[str] = Field(default_factory=list)
    page_count: int = 0
    message: str = "success"


class ExtractResponse(BaseModel):
    file: str = ""
    page_count: int = 0
    invoice_type: str = ""
    invoice_number: str = ""
    issue_date: str = ""
    buyer_name: str = ""
    buyer_tax_id: str = ""
    seller_name: str = ""
    seller_tax_id: str = ""
    amount_without_tax: str = ""
    tax_amount: str = ""
    total_amount: str = ""
    total_amount_upper: str = ""
    payee: str = ""
    reviewer: str = ""
    drawer: str = ""
    # 商品明细。仅 XML 全电发票填充；PDF / OFD 恒为空数组。
    items: List[InvoiceItem] = Field(default_factory=list)
    # 消费类别（参见 InvoiceRecord 同名字段说明）
    expense_category: str = ""
    expense_category_label: str = ""
    expense_category_source: str = ""
    message: str = "success"


# ── 海关出口货物报关单专用 schema ──
#
# 报关单没有「价税合计 / 大写金额」概念，金额字段使用「明细总价求和 + 币制」。
# 明细跨页分布（例如出口退税联 6 页），由 ``full_text`` 跨页聚合后填充 ``items[]``。
# 字段命名遵循现有 schema 的英文 snake_case 风格，docstring 注明对应的中文表标签。


class CustomsDeclarationItem(BaseModel):
    """报关单商品明细 (一项).

    字段名 → 中文表标签::

        item_no            项号 / 商品序号
        hs_code            商品编号 (HS 编码，10 位)
        name_and_spec      商品名称及规格型号 (含规格续行，以 ``|`` 连接)
        legal_qty          法定数量 + 法定单位 (例 ``"210个"``)
        second_qty         第二数量 + 第二单位 (例 ``"325.5千克"``)
        declared_qty       申报数量 + 申报单位
        unit_price         单价
        total_price        总价
        currency_code      币制代码 (例 ``"142"``)
        currency_name      币制名 (例 ``"美元"`` / ``"USD"``)
        origin_country_code  原产国（地区）代码
        origin_country_name  原产国（地区）名称
        final_destination_code 最终目的国（地区）代码
        final_destination_name 最终目的国（地区）名称
        source_region_code 境内货源地代码
        source_region_name  境内货源地名称
        tax_status         征免（照章征税 / 全免 / 保税 等）
    """

    item_no: str = ""
    hs_code: str = ""
    name_and_spec: str = ""
    legal_qty: str = ""
    second_qty: str = ""
    declared_qty: str = ""
    unit_price: str = ""
    total_price: str = ""
    currency_code: str = ""
    currency_name: str = ""
    origin_country_code: str = ""
    origin_country_name: str = ""
    final_destination_code: str = ""
    final_destination_name: str = ""
    source_region_code: str = ""
    source_region_name: str = ""
    tax_status: str = ""


class CustomsDeclarationHeader(BaseModel):
    """海关出口货物报关单单据头.

    字段命名与中文表标签对应关系见源码注释。版式 A (操作联) 与
    版式 B (出口退税联) 共用同一 schema，差异字段（如 ``settlement_method``、
    ``approval_no``）在某一方缺省时留空。
    """

    # 文档元信息
    file: str = ""
    page_count: int = 0
    declaration_variant: str = ""  # 操作联 / 出口退税联
    direction: str = ""  # 出口 / 进口 (目前仅识别出口)

    # 主标识符
    customs_no: str = ""  # 海关编号：<18位>
    pre_entry_no: str = ""  # 预录入编号
    record_no: str = ""  # 备案号

    # 日期 (ISO 格式 YYYY-MM-DD)
    export_date: str = ""  # 出口日期
    declaration_date: str = ""  # 申报日期
    print_date: str = ""  # 打印日期 (退税联独有)

    # 贸易主体
    shipper_name: str = ""  # 境内发货人 / 境内收发货人 (卖家)
    shipper_tax_id: str = ""  # 统一社会信用代码
    consignee_name: str = ""  # 境外收货人 (买家；退税联版式无此字段)
    consignee_tax_id: str = ""
    producer_name: str = ""  # 生产销售单位
    producer_tax_id: str = ""
    declarant_name: str = ""  # 申报单位 (报关代理)
    declarant_tax_id: str = ""

    # 物流 / 关务
    customs_office_code: str = ""  # 关区代码 (如 8620, 2248)
    customs_office_name: str = ""  # 关区名
    transport_mode: str = ""  # 运输方式文本
    transport_mode_code: str = ""  # 运输方式代码
    vessel_or_flight: str = ""  # 运输工具名称及航次号
    bill_of_lading_no: str = ""  # 提运单号
    supervision_mode_code: str = ""  # 监管方式代码
    supervision_mode_name: str = ""  # 监管方式名
    tax_exemption_code: str = ""  # 征免性质代码
    tax_exemption_name: str = ""
    settlement_method: str = ""  # 结汇方式 (退税联独有)

    # 单证
    license_no: str = ""  # 许可证号
    approval_no: str = ""  # 批准文号 (退税联独有)

    # 贸易条款 / 地理
    contract_no: str = ""  # 合同协议号
    trade_country_code: str = ""  # 贸易国（地区）代码
    trade_country_name: str = ""
    destination_country_code: str = ""  # 运抵国（地区）代码
    destination_country_name: str = ""
    destination_port_code: str = ""  # 指运港代码
    destination_port_name: str = ""
    exit_port_code: str = ""  # 离境口岸代码 (操作联独有)
    exit_port_name: str = ""
    source_region_code: str = ""  # 境内货源地代码
    source_region_name: str = ""

    # 货物摘要
    package_type_code: str = ""  # 包装种类代码
    package_type_name: str = ""
    packages_count: str = ""  # 件数
    gross_weight_kg: str = ""  # 毛重（千克）
    net_weight_kg: str = ""  # 净重（千克）
    trade_term_code: str = ""  # 成交方式代码
    trade_term_name: str = ""  # 成交方式名 (FOB / C&F / CIF)
    freight: str = ""  # 运费
    insurance: str = ""  # 保费
    misc_charges: str = ""  # 杂费

    # 关联单据
    attached_docs: str = ""  # 随附单证及编号 / 随附单据
    container_no: str = ""  # 集装箱号 (退税联)
    container_summary: str = ""  # 集装箱标箱数及号码 (操作联)
    marks_and_remarks: str = ""  # 标记唛码及备注

    # 报关 / 申报
    declarant_person: str = ""  # 报关人员
    declarant_person_id: str = ""  # 报关人员证号
    declarant_phone: str = ""  # 电话

    # 汇总
    item_count: int = 0
    currency: str = ""  # 主导币制 (如 USD)
    total_amount: str = ""  # 全部明细 总价 列累计 (原值)

    # 明细
    items: List["CustomsDeclarationItem"] = []

    message: str = "success"


CustomsDeclarationHeader.model_rebuild()
