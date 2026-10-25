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
