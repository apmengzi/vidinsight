"""一图流：把 Markdown 总结渲染成一张可分享的长图（本地 PIL，零外部依赖）。

为什么是 PIL 而不是调文生图 API：
  - 答辩要现场演示，**任何外部服务都可能当天不可用**（实测：WorkBuddy 反代
    的 /v1/images/generations 只路由 chat、图像模型 503 pool_empty；Miora 无
    公开 API 且需登录态）。一图流是核心演示项，不能赌外部服务活着。
  - PIL 纯本地：不联网、不耗 token、秒级出图、结果可复现。

设计约束：
  - 内容来自**已生成的总结**（绝不自创内容），只是重新排版
  - 尺寸 1080×1350（小红书/朋友圈竖版），深色刊头 + 浅色正文 = 双层结构
  - 中文用微软雅黑，缺字体时优雅降级不崩
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# 固化真 PIL 的引用：某些测试（test_video_reader_dedupe）会用 types.ModuleType
# 伪造 PIL 塞进 sys.modules 且不恢复，导致运行期 PIL.Image.open 触发的懒加载
# `from PIL import ImageFile` 失败（"unknown location"）。这里在导入期就把
# 真实函数抓到手，运行期不再经过被污染的 sys.modules。
_PIL_OPEN = Image.open
_PIL_NEW = Image.new

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 画布与配色（与前端「剪辑室」主题同源）
W, H = 1080, 1350
INK = (31, 35, 40)          # 深色刊头背景
INK_2 = (28, 31, 36)
BG = (245, 246, 248)        # 浅色正文底
CARD = (255, 255, 255)
TEXT = (31, 35, 40)
TEXT_2 = (90, 96, 104)
MUTED = (140, 146, 154)
ACCENT = (229, 72, 77)      # rec 红：只用于"正在发生"的语义，这里作序号强调
HAIRLINE = (227, 230, 234)

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",     # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


_FONT_CACHE: dict = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按优先级挑一个能用的中文字体；全都没有就用 PIL 默认（不崩）。

    字体对象按 (path, size) 缓存：PIL 加载 truetype 不便宜，且并发场景
    反复打开文件可能与其他 IO 测试（workbuddy 等）撞锁。
    """
    cands = _FONT_CANDIDATES
    if not bold:
        cands = [c for c in cands if "bd" not in c.lower()] + cands
    for path in cands:
        try:
            if os.path.exists(path):
                key = (path, size)
                if key in _FONT_CACHE:
                    return _FONT_CACHE[key]
                f = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = f
                return f
        except Exception:
            continue
    logger.warning("未找到中文字体，一图流将使用默认字体（中文可能显示为方框）")
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_width: int) -> List[str]:
    """按像素宽度折行，中英文混排都可用（逐字累加宽度）。"""
    lines: List[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _clean(line: str) -> str:
    """去掉 markdown 记号，只留纯文本。"""
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = re.sub(r"^>\s*", "", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\*(.+?)\*", r"\1", s)
    s = re.sub(r"`(.+?)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", s)
    s = re.sub(r"^\s*[-*+]\s*", "", s)
    s = re.sub(r"^\s*(\d+)[.)]\s*", r"\1. ", s)
    return s.strip()


def _parse(markdown: str, max_points: int = 6) -> Tuple[str, List[str]]:
    """从 markdown 里抽出标题（第一个 H1/H2）与要点列表。

    只做"提取"，不做任何内容创作——图上的每个字都来自用户已生成的总结。
    """
    if not markdown:
        return "", []

    title = ""
    for raw in markdown.splitlines():
        m = re.match(r"^#{1,2}\s+(.+)$", raw.strip())
        if m:
            t = _clean(m.group(1))
            # 跳过"总结/概述"这类无信息量的通用标题
            if t and len(t) >= 2:
                title = t
                break

    # 没标题就取第一行非空文本
    if not title:
        for raw in markdown.splitlines():
            c = _clean(raw)
            if c:
                title = c[:40]
                break

    points: List[str] = []
    for raw in markdown.splitlines():
        s = raw.strip()
        # 收 -/*/+ 与 1. 开头的条目
        if re.match(r"^\s*[-*+]\s+", s) or re.match(r"^\s*\d+[.)]\s+", s):
            c = _clean(s)
            if len(c) >= 4:
                points.append(c)
        if len(points) >= max_points:
            break

    # 没有列表条目 → 退化成按段落取前几句
    if not points:
        for raw in markdown.splitlines():
            c = _clean(raw)
            if len(c) >= 12 and not c.startswith("|"):
                points.append(c[:60])
            if len(points) >= 4:
                break

    return title[:40], points[:max_points]


def render_one_pager(
    markdown: str,
    output_path: str,
    *,
    source_label: str = "",
    cover_path: Optional[str] = None,
) -> Optional[str]:
    """把总结渲染成一图流 PNG。返回输出路径，失败返回 None（绝不抛给调用方）。"""
    try:
        title, points = _parse(markdown)
        if not title and not points:
            return None

        img = _PIL_NEW("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)

        f_title = _font(56, bold=True)
        f_meta = _font(24)
        f_sec = _font(26, bold=True)
        f_point = _font(32)
        f_num = _font(30, bold=True)

        # ── 刊头（深色块）──
        d.rectangle([0, 0, W, 300], fill=INK)
        d.rectangle([0, 0, 6, 300], fill=ACCENT)  # 左侧 rec 信号条

        ty = 70
        for line in _wrap(d, title or "内容洞察", f_title, W - 140)[:2]:
            d.text((64, ty), line, font=f_title, fill=(255, 255, 255))
            ty += 68
        if source_label:
            d.text((64, ty + 12), source_label[:46], font=f_meta, fill=(170, 176, 184))

        # ── 正文卡片 ──
        card_w = W - 128
        # 按内容算最小高度；内容少时**撑满可用区**（卡片内留白优于卡片外
        # 留白——后者会让图看起来"没做完"）。可用区 = 刊头底 → 页脚上。
        avail_top, avail_bottom = 340, H - 110
        avail_h = avail_bottom - avail_top
        est = 130
        wrapped: List[Tuple[str, List[str]]] = []
        for p in points:
            ls = _wrap(d, p, f_point, card_w - 120)
            wrapped.append((p, ls))
            est += 30 + len(ls) * 46
        card_h = min(max(est, avail_h), avail_h)
        cy = avail_top

        d.rounded_rectangle([64, cy, 64 + card_w, cy + card_h], radius=20, fill=CARD,
                            outline=HAIRLINE, width=1)

        d.text((100, cy + 40), "核心要点", font=f_sec, fill=TEXT_2)
        d.line([100, cy + 86, 100 + card_w - 72, cy + 86], fill=HAIRLINE, width=1)

        py = cy + 116
        for idx, (_p, lines) in enumerate(wrapped, start=1):
            if py + len(lines) * 46 > cy + card_h - 30:
                break
            num = f"{idx:02d}"
            d.text((100, py), num, font=f_num, fill=ACCENT)
            for li, ln in enumerate(lines):
                d.text((168, py + li * 46), ln, font=f_point, fill=TEXT)
            py += 30 + len(lines) * 46

        # ── 封面缩略（有就贴右下，纯装饰不抢内容）──
        if cover_path and os.path.exists(cover_path):
            try:
                with _PIL_OPEN(cover_path) as cv:
                    cv = cv.convert("RGB")
                    cw = 260
                    ch = int(cv.height * cw / cv.width)
                    cv = cv.resize((cw, ch))
                    img.paste(cv, (W - 64 - cw, H - 64 - ch))
            except Exception as e:
                logger.warning(f"一图流封面粘贴失败（忽略）: {e}")

        # ── 页脚 ──
        d.text((64, H - 54), "VidInsight · 内容洞察", font=f_meta, fill=MUTED)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "PNG")
        logger.info(f"一图流已生成: {output_path} ({W}x{H})")
        return output_path
    except Exception as e:
        logger.error(f"一图流渲染失败: {e}")
        return None
