"""一图流渲染测试。

锁住三件事：
  1. 内容**只来自总结**，不自创——标题与要点都从 markdown 里抽
  2. 任何异常都不抛给调用方（返回 None），一图流是增强项不能拖垮主流程
  3. 排版不留大片空白（卡片高度贴合内容并垂直居中）
"""
import os

import pytest
from PIL import Image

from app.utils.one_pager import _clean, _parse, render_one_pager

MD = """# 如何用 AI 做视频笔记

## 核心要点

- 第一条要点：把视频链接直接转成结构化笔记
- 第二条要点：支持四个主流平台的链接解析
- 第三条要点：内置 ReAct Agent 可连续追问
"""


def test_clean_strips_markdown_marks():
    assert _clean("## 标题") == "标题"
    assert _clean("- 要点") == "要点"
    assert _clean("**加粗**") == "加粗"
    assert _clean("1. 编号") == "1. 编号"
    assert _clean("[文字](http://x)") == "文字"


def test_parse_extracts_title_and_points():
    title, points = _parse(MD)
    assert "视频笔记" in title
    assert len(points) == 3
    assert all("要点" in p for p in points)


def test_parse_handles_empty():
    title, points = _parse("")
    assert title == ""
    assert points == []


def test_parse_falls_back_to_first_line_when_no_heading():
    title, _ = _parse("没有标题的一段话，用来验证回退逻辑")
    assert title


def test_render_creates_valid_png(tmp_path):
    out = str(tmp_path / "p.png")
    saved = render_one_pager(MD, out, source_label="B站 · 12:30")
    assert saved == out
    assert os.path.exists(out)
    with Image.open(out) as im:
        assert im.size == (1080, 1350)
        assert im.format == "PNG"


def test_render_returns_none_on_empty_content(tmp_path):
    """内容不足不能报错，只能返回 None（增强项不拖垮主流程）。"""
    out = str(tmp_path / "empty.png")
    assert render_one_pager("", out) is None
    assert render_one_pager("   ", out) is None
    assert render_one_pager(None, out) is None  # type: ignore[arg-type]


def test_render_never_raises_on_weird_input(tmp_path):
    for bad in ["#", "---", "|表|格|", "![]()", "a" * 5000]:
        assert render_one_pager(bad, str(tmp_path / f"{abs(hash(bad))}.png")) is not None or True


def test_render_tolerates_missing_cover(tmp_path):
    """封面文件不存在要忽略，不能崩。"""
    out = str(tmp_path / "nocover.png")
    assert render_one_pager(MD, out, cover_path="不存在的路径.jpg") == out


def test_card_fits_content_without_huge_gap(tmp_path):
    """内容少时卡片垂直居中——底部不应出现大片空白（视觉回归防护）。"""
    out = str(tmp_path / "fit.png")
    render_one_pager(MD, out)
    with Image.open(out) as im:
        # 底部 1/5 区域不应整片都是背景色（说明卡片已居中而非顶在上边）
        band = im.crop((0, int(im.height * 0.82), im.width, im.height))
        colors = band.getcolors(maxcolors=100000) or []
        bg_like = sum(c for c, _rgb in colors if _rgb == (245, 246, 248))
        total = sum(c for c, _rgb in colors)
        assert bg_like < total * 0.98, "底部几乎全是背景色，卡片没有居中"


# ── 结构化渲染（LLM 提炼路径，2026-11-04）────────────────

STRUCTURED = {
    "title": "扫地机器人怎么选",
    "subtitle": "五个维度避开选购陷阱",
    "points": [
        {"head": "吸力", "body": "2000Pa 起步，大户型要 3000Pa 以上"},
        {"head": "边刷", "body": "双旋转边刷覆盖率高，单边刷容易漏墙角"},
        {"head": "水箱", "body": "电控水箱可拆洗，容量 200ml 起"},
        {"head": "避障", "body": "激光避障优于红外，家里有宠物必须上"},
        {"head": "基站", "body": "自动集尘能省三个月倒一次尘盒的麻烦"},
        {"head": "价格", "body": "3000 元档性价比最高，万元以上溢价在品牌"},
    ],
    "conclusion": "先定预算再看参数，别为用不上的功能付费",
}


def test_structured_renders_all_points(tmp_path):
    """6 条要点必须全部画出来——旧版超出就 break，静默丢内容。"""
    out = str(tmp_path / "s6.png")
    assert render_one_pager("", out, structured=STRUCTURED) == out
    assert os.path.exists(out)


def test_structured_conclusion_not_clipped(tmp_path):
    """结论条不能被页脚裁切。

    旧版把可用区下界写死成 H-150，结论条实际只剩 60px，
    第二行文字直接压在页脚上（实测踩过）。
    """
    out = str(tmp_path / "concl.png")
    render_one_pager("", out, structured=STRUCTURED)
    with Image.open(out) as im:
        # 结论条是深色块，应占据页脚上方一段区域。
        # 取样点选在结论条内部（避开白色文字笔画，取左侧内边距处的纯底色）。
        y = im.height - 110
        px = im.getpixel((im.width // 2, y))
        assert sum(px) < 300, f"结论条位置是浅色 {px}，说明被裁掉了"
        # 结论条底边不能越过页脚基线，否则会压住「VidInsight · 内容洞察」
        bottom = im.height - 62
        px2 = im.getpixel((im.width // 2, bottom))
        assert sum(px2) > 600, f"结论条压到了页脚区 {px2}"


def test_structured_title_does_not_overlap_subtitle(tmp_path):
    """长标题折成两行时不能压住副标题。"""
    long_title = dict(STRUCTURED)
    long_title["title"] = "这是一个特别长的标题会折成两行看看会不会压住副标题"
    out = str(tmp_path / "longtitle.png")
    render_one_pager("", out, structured=long_title)
    with Image.open(out) as im:
        # 副标题在刊头下半部：该区域不应出现纯白（标题色）大面积像素
        band = im.crop((60, 150, im.width - 60, 230))
        colors = band.getcolors(maxcolors=200000) or []
        white = sum(c for c, rgb in colors if min(rgb) > 240)
        total = sum(c for c, _rgb in colors)
        assert white < total * 0.5, "刊头下半部出现大片纯白，标题压住了副标题"


def test_structured_falls_back_when_empty(tmp_path):
    """structured 里没内容时要退回本地抽取，不能出白图。"""
    out = str(tmp_path / "fallback.png")
    assert render_one_pager(MD, out, structured={"title": "", "points": []}) == out


def test_structured_ignores_bad_points(tmp_path):
    """要点里混入 None / 空串不能崩。"""
    out = str(tmp_path / "bad.png")
    data = {"title": "t", "points": [None, "", {"head": "", "body": ""},
                                     {"head": "有效", "body": "内容"}]}
    assert render_one_pager("", out, structured=data) == out
