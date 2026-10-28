"""平台下载版电子发票的版式识别与阅读顺序重建测试。

这些用例**不依赖 samples/**：版面几何用合成的词坐标列表描述，因此公开
仓库（samples/ 不入库）也能跑。真实样本的逐字段断言见
``tests/test_pipeline.py::TestKnownInvoices``。
"""

from __future__ import annotations

from typing import Dict, List

from fapiao_cn.parsers import dispatch
from fapiao_cn.parsers.platform_invoice import (
    _visual_text,
    looks_like_platform_layout,
    parse_platform_invoice,
)

# ── 合成版面 ────────────────────────────────────────────────────────────
#
# 真实平台下载版票面（志诚_26322000008364632971.pdf）的几何：
# 标签块与值块在**票面上同行**，但 PDF 内容流把标签写在前、值写在后，
# 于是 pdfplumber 的文本层顺序是「标签块 + 值块」两段。
#
# 下面的词列表按**视觉位置**给出（top / x0），解析器要自己把它们排回
# 阅读顺序；_BROKEN_TEXT 则是未修复的文本层顺序（值全被甩到末尾）。


def _w(text: str, top: float, x0: float, *, upright: bool = True, width: float = 0.0) -> Dict:
    """构造一个 pdfplumber 风格的 word 字典。"""

    if not width:
        width = len(text) * 5.0
    return {
        "text": text,
        "top": top,
        "bottom": top + 9.0,
        "doctop": top,
        "x0": x0,
        "x1": x0 + width,
        "height": 9.0,
        "upright": upright,
    }


def _platform_words() -> List[Dict]:
    """志诚票面的词坐标（值块在视觉上紧跟标签，文本层顺序却相反）。"""

    return [
        _w("电子发票（普通发票）", 22.7, 192.9),
        # 发票号码：标签在右上，值紧跟其右侧（同一行）
        _w("发票号码：", 31.7, 438.0),
        _w("26322000008364632971", 31.8, 481.9),
        _w("开票日期：", 48.7, 438.0),
        _w("2026年10月12日", 49.1, 481.9),
        # 购买方 / 销售方 竖排表头字
        _w("购", 92.9, 16.9),
        _w("销", 92.9, 301.8),
        _w("买", 102.9, 16.9),
        _w("名称：", 105.7, 32.7),
        _w("重庆市旺成科技股份有限公司", 104.9, 56.7),
        _w("售", 102.9, 301.8),
        _w("名称：", 105.7, 317.6),
        _w("兴化市志诚五金制品有限公司", 104.9, 340.2),
        _w("方", 112.8, 16.9),
        _w("方", 112.8, 301.8),
        _w("信", 122.7, 16.9),
        _w("统一社会信用代码/纳税人识别号：", 125.5, 32.5),
        _w("91500106622015389N", 123.6, 153.1),
        _w("信", 122.7, 301.8),
        _w("统一社会信用代码/纳税人识别号：", 125.5, 317.4),
        _w("92321281MAC97HLQ8N", 123.6, 438.0),
        _w("息", 132.6, 16.9),
        _w("息", 132.6, 301.8),
        # 明细表
        _w("项目名称", 151.1, 45.4),
        _w("规格型号", 151.1, 130.8),
        _w("单", 151.1, 204.2),
        _w("位", 151.1, 222.2),
        _w("数", 151.1, 263.7),
        _w("量", 151.1, 281.7),
        _w("单", 151.1, 334.6),
        _w("价", 151.1, 352.6),
        _w("金", 151.1, 406.8),
        _w("额", 151.1, 424.8),
        _w("税率/征收率", 151.1, 446.5),
        _w("税", 151.1, 551.4),
        _w("额", 151.1, 569.4),
        _w("*金属制品*一字槽外圆", 161.2, 12.8),
        _w("M6*25", 161.2, 113.4),
        _w("个", 161.2, 212.4),
        _w("500", 161.5, 277.1),
        _w("0.54554", 161.5, 335.6),
        _w("272.77", 161.5, 405.3),
        _w("1%", 161.5, 467.5),
        _w("2.73", 161.5, 561.7),
        _w("柱销", 174.0, 12.8),
        # 合计 / 价税合计
        _w("合", 291.4, 58.2),
        _w("计", 291.4, 103.5),
        _w("¥272.77", 293.3, 398.7),
        _w("¥2.73", 293.3, 555.1),
        _w("价税合计（大写）", 311.8, 48.0),
        _w("贰佰柒拾伍圆伍角整", 311.3, 178.4),
        _w("（小写）", 311.8, 406.8),
        _w("¥275.50", 310.9, 440.8),
        # 备注（值在标签上方一行，且票面没有「备注」字段，不参与取值）
        _w("订单：5127442814315027123", 333.1, 31.5),
        _w("店铺：斌致旗舰店", 333.1, 148.5),
        _w("备", 342.4, 17.1),
        _w("注", 359.4, 17.1),
        _w("开票人：", 392.0, 55.4),
        _w("邱惠燚", 392.0, 90.7),
    ]


# 平台盖的竖排水印（upright=False）—— 不是票面内容
_PLATFORM_WATERMARK = _w("下载次数：1", 83.7, 585.2, upright=False, width=9.0)

# 文本层顺序：标签块在前、值块在后（未修复的样子）
_BROKEN_TEXT = """电子发票（普通发票）
发票号码：
开票日期：
购 销
买 名称： 售 名称：
方 方
信 统一社会信用代码/纳税人识别号： 信 统一社会信用代码/纳税人识别号：
息 息
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
下载次数：1
26322000008364632971
2026年10月12日
重庆市旺成科技股份有限公司 兴化市志诚五金制品有限公司
91500106622015389N 92321281MAC97HLQ8N
*金属制品*一字槽外圆 M6*25 个 500 0.54554 272.77 1% 2.73
柱销
¥272.77 ¥2.73
贰佰柒拾伍圆伍角整 ¥275.50
订单：5127442814315027123 店铺：斌致旗舰店
邱惠燚"""


class TestPlatformLayoutDetection:
    """版式识别：靠结构特征，不靠关键字。"""

    def test_platform_text_detected(self):
        assert looks_like_platform_layout(_BROKEN_TEXT)

    def test_normal_invoice_not_detected(self):
        text = (
            "电子发票（增值税专用发票） 发票号码: 25502000000129241041\n"
            "开票日期: 2025年11月27日\n"
            "名 称 : 重庆市旺成科技股份有限公司\n"
        )
        assert not looks_like_platform_layout(text)

    def test_single_empty_label_is_not_enough(self):
        """只有一个标签空着不算平台版式（普通票面也可能把值换行排）。"""
        text = "电子发票（普通发票）\n发票号码：\n开票日期: 2026年10月12日\n"
        assert not looks_like_platform_layout(text)

    def test_same_label_twice_is_not_enough(self):
        """同一个标签出现两次只算一种标签。"""
        text = "电子发票（普通发票）\n发票号码：\n发票号码：\n"
        assert not looks_like_platform_layout(text)

    def test_non_invoice_not_detected(self):
        text = "发票号码：\n开票日期：\n"
        assert not looks_like_platform_layout(text)

    def test_kangxi_title_still_detected(self):
        """原始文本里的康熙部首 ``⼦`` 不该影响版式识别。

        平台下载版的「电子发票」标题在 PDF 里写的是康熙部首 ``⼦``。调用方
        传的可能是原始文本（``classify_samples.py`` 就是），检测器必须自己
        归一化，不能依赖调用方先清洗过。
        """
        text = _BROKEN_TEXT.replace("电子发票（普通发票）", "电⼦发票（普通发票）")
        assert looks_like_platform_layout(text)

    def test_dispatch_routes_to_platform_parser(self):
        name, _ = dispatch(_BROKEN_TEXT)
        assert name == "platform"

    def test_dispatch_keeps_normal_invoice_on_default(self):
        name, _ = dispatch("电子发票（增值税专用发票） 发票号码: 25502000000129241041")
        assert name == "new"


class TestPlatformVisualText:
    """按词坐标重建阅读顺序。"""

    def test_reading_order_rebuilt(self):
        text = _visual_text(_platform_words())
        assert "发票号码： 26322000008364632971" in text
        assert "开票日期： 2026年10月12日" in text
        assert "合 计 ¥272.77 ¥2.73" in text
        assert "开票人： 邱惠燚" in text

    def test_vertical_headers_removed(self):
        """购买方 / 销售方的竖排表头字必须剔除，否则卖方名称会被截成「售」。"""
        text = _visual_text(_platform_words())
        assert "售 名称：" not in text
        assert "买 名称：" not in text
        assert text.count("名称：") == 2

    def test_vertical_watermark_excluded(self):
        """平台盖的竖排水印不是票面内容。"""
        words = _platform_words() + [_PLATFORM_WATERMARK]
        text = _visual_text(words)
        assert "下载次数" not in text

    def test_empty_word_list(self):
        assert _visual_text([]) == ""

    def test_kangxi_radical_normalised(self):
        """PDF 里出现的康熙部首 ``⼦`` 要还原成 ``子``，否则票种识别不到。"""
        words = [_w("电⼦发票（普通发票）", 22.7, 192.9)]
        text = _visual_text(words)
        assert "电子发票（普通发票）" in text


class TestPlatformParser:
    """平台下载版解析结果。"""

    def _ctx(self, words=None):
        words = _platform_words() if words is None else words
        return {"page_index": 0, "parser_name": "platform", "words": words}

    def test_all_fields_extracted(self):
        rec = parse_platform_invoice(_BROKEN_TEXT, self._ctx())
        assert rec.invoice_type == "电子发票（普通发票）"
        assert rec.invoice_number == "26322000008364632971"
        assert rec.issue_date == "2026-10-12"
        assert rec.buyer_name == "重庆市旺成科技股份有限公司"
        assert rec.buyer_tax_id == "91500106622015389N"
        assert rec.seller_name == "兴化市志诚五金制品有限公司"
        assert rec.seller_tax_id == "92321281MAC97HLQ8N"
        assert rec.amount_without_tax == "272.77"
        assert rec.tax_amount == "2.73"
        assert rec.total_amount == "275.50"
        assert rec.total_amount_upper == "贰佰柒拾伍圆伍角整"
        assert rec.drawer == "邱惠燚"

    def test_baseline_misses_the_broken_fields(self):
        """对照组：未重建的文本层顺序确实取不到号码 / 日期 / 金额。"""
        from fapiao_cn.parsers.invoice import parse_new_electronic_invoice

        rec = parse_new_electronic_invoice(_BROKEN_TEXT, self._ctx())
        assert rec.invoice_number == ""
        assert rec.issue_date == ""
        assert rec.total_amount == ""

    def test_falls_back_to_baseline_without_words(self):
        """拿不到坐标时退回原始文本，绝不返回空记录。"""
        rec = parse_platform_invoice(_BROKEN_TEXT, {"page_index": 0})
        assert rec.buyer_name == "重庆市旺成科技股份有限公司"

    def test_keeps_baseline_when_rebuild_is_worse(self):
        """重建结果明显更差时用原始文本（安全网）。"""

        class _BadWords:
            def __init__(self, words):
                self._words = words

            def words(self):
                return self._words

        # 只给一个没有价值的词：重建结果必然比原始文本差
        ctx = {"page_index": 0, "page": _BadWords([_w("毫无意义", 10.0, 10.0)])}
        rec = parse_platform_invoice(_BROKEN_TEXT, ctx)
        assert rec.buyer_name == "重庆市旺成科技股份有限公司"
        assert rec.invoice_number == ""
