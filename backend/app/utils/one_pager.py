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


def _render_structured(
    data: dict,
    output_path: str,
    *,
    source_label: str = "",
    cover_path: Optional[str] = None,
) -> Optional[str]:
    """按 LLM 提炼的结构渲染（带小标题的要点卡）。

    版式策略（2026-11-04 重写，修两个实测 bug）：
    1. **自下而上布局**：先定页脚位置 → 结论条高度（按行数算）→ 剩下才是卡片区。
       旧版把 avail_bottom 写死成 H-150，结论条实际只剩 60px 高，
       第二行文字直接压在页脚上（实测「进点方式没有标准答案…」被裁）。
    2. **自适应字号**：按要点总行数选档位（4 档），保证**所有要点都能画出来**。
       旧版超出就 break，LLM 给了 6 条只显示 5 条——静默丢内容。
    """
    title = str(data.get("title") or "").strip() or "内容洞察"
    subtitle = str(data.get("subtitle") or "").strip()
    conclusion = str(data.get("conclusion") or "").strip()
    points = data.get("points") or []
    if not isinstance(points, list):
        points = []

    # 预处理要点：过滤空项
    clean_points = []
    for p in points:
        if not isinstance(p, dict):
            continue
        head = str(p.get("head") or "").strip()
        body = str(p.get("body") or "").strip()
        if head or body:
            clean_points.append((head, body))
    if not clean_points:
        return None

    img = _PIL_NEW("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    card_x = 64
    card_w = W - 128
    inner_w = card_w - 130          # 卡片内文字可用宽度（左侧给序号）
    FOOTER_Y = H - 52               # 页脚文字基线
    # 页脚预留高度：文字在 H-52 且字号 24（下缘约 H-30），再加 24px 间隙
    # → 结论条底边必须止于 H-78。旧值 62 会让结论条压住页脚（实测踩过）。
    FOOTER_RESERVED = 96

    # ── ① 结论条：按文字行数算高度（自下而上）──
    f_meta = _font(24)
    f_concl = _font(29, bold=True)
    concl_h = 0
    concl_lines: List[str] = []
    if conclusion:
        concl_lines = _wrap(d, conclusion, f_concl, card_w - 60)[:3]
        concl_h = 18 + 26 + len(concl_lines) * 38 + 16   # 上下内边距 + 标签 + 文字行
    concl_top = (H - FOOTER_RESERVED - concl_h) if conclusion else H - FOOTER_RESERVED
    card_bottom = (concl_top - 16) if conclusion else concl_top

    # ── ② 刊头：按标题行数算高度 ──
    # 坑（2026-11-04 实测）：标题折成 2 行时，第 2 行会压住副标题。
    # 根因是 head_h 只按 title_lines 累加，没有把副标题行数一起算进来。
    f_title = _font(58, bold=True)
    f_sub = _font(27)
    title_lines = _wrap(d, title, f_title, W - 140)[:2]
    sub_lines = _wrap(d, subtitle, f_sub, W - 140)[:2] if subtitle else []

    head_h = 62 + len(title_lines) * 70           # 标题区
    if sub_lines:
        head_h += 14 + len(sub_lines) * 38        # 副标题区
    head_h += 56 if source_label else 34          # 来源行 + 底部留白
    head_h = max(head_h, 230)

    d.rectangle([0, 0, W, head_h], fill=INK)
    d.rectangle([0, 0, 6, head_h], fill=ACCENT)

    ty = 62
    for line in title_lines:
        d.text((64, ty), line, font=f_title, fill=(255, 255, 255))
        ty += 70
    if sub_lines:
        ty += 14
        for line in sub_lines:
            d.text((64, ty), line, font=f_sub, fill=(178, 184, 192))
            ty += 38
    if source_label:
        d.text((64, head_h - 44), source_label[:46], font=f_meta, fill=(150, 156, 164))

    # ── ③ 卡片区：自适应字号，保证所有要点都放得下 ──
    avail_top = head_h + 32
    avail_h = card_bottom - avail_top
    if avail_h < 220:
        return None

    # 档位：(head字号, body字号, head行高, body行高, 条目间距)
    TIERS = [
        (30, 28, 36, 42, 24),
        (28, 26, 34, 38, 20),
        (26, 24, 32, 35, 17),
        (24, 22, 29, 31, 14),
        (22, 21, 27, 29, 12),
    ]

    # 卡片内部布局常量（测量和绘制**必须共用**，否则会出现
    # 「算得下但画不下」——2026-11-04 实测：LLM 给 6 条只画出 5 条）
    CARD_PAD_TOP = 32        # 卡片顶 → 「核心要点」文字
    CARD_TITLE_H = 76        # 「核心要点」+ 分隔线占高
    CARD_PAD_BOTTOM = 22     # 最后一条 → 卡片底

    chosen = None
    for hsz, bsz, hh, bh, gap in TIERS:
        f_head_t = _font(hsz, bold=True)
        f_body_t = _font(bsz)
        blocks = []
        total = CARD_TITLE_H
        for head, body in clean_points:
            body_lines = _wrap(d, body, f_body_t, inner_w) if body else []
            blocks.append((head, body_lines))
            total += (hh if head else 0) + len(body_lines) * bh + gap
        total += CARD_PAD_BOTTOM
        # 卡片高度 = 内容总高；必须装得进可用区
        if total <= avail_h:
            chosen = (hsz, bsz, hh, bh, gap, f_head_t, f_body_t, blocks, total)
            break

    if chosen is None:
        # 全部档位都放不下：用最紧档，接受截断（记日志便于发现问题）
        hsz, bsz, hh, bh, gap = TIERS[-1]
        f_head_t = _font(hsz, bold=True)
        f_body_t = _font(bsz)
        blocks = []
        total = CARD_TITLE_H
        for head, body in clean_points:
            body_lines = _wrap(d, body, f_body_t, inner_w) if body else []
            blocks.append((head, body_lines))
            total += (hh if head else 0) + len(body_lines) * bh + gap
        total += CARD_PAD_BOTTOM
        chosen = (hsz, bsz, hh, bh, gap, f_head_t, f_body_t, blocks, total)
        logger.warning(
            f"[one_pager] 内容超出画布，将截断部分要点 "
            f"(avail_h={avail_h}, 需要={total}, 要点数={len(clean_points)})"
        )

    _hsz, _bsz, hh, bh, gap, f_head_t, f_body_t, blocks, card_h = chosen
    f_num = _font(min(_hsz, 28), bold=True)
    f_sec = _font(26, bold=True)

    card_h = min(card_h, avail_h)
    d.rounded_rectangle([card_x, avail_top, card_x + card_w, avail_top + card_h],
                        radius=20, fill=CARD, outline=HAIRLINE, width=1)
    d.text((card_x + 36, avail_top + CARD_PAD_TOP), "核心要点", font=f_sec, fill=TEXT_2)
    d.line([card_x + 36, avail_top + CARD_TITLE_H - 8,
            card_x + card_w - 36, avail_top + CARD_TITLE_H - 8],
           fill=HAIRLINE, width=1)

    # 绘制起点与测量基准完全一致（CARD_TITLE_H），不再有额外偏移
    py = avail_top + CARD_TITLE_H
    bottom_limit = avail_top + card_h - CARD_PAD_BOTTOM
    drawn = 0
    for idx, (head, body_lines) in enumerate(blocks, start=1):
        block_h = (hh if head else 0) + len(body_lines) * bh + gap
        if py + block_h > bottom_limit:
            break
        d.text((card_x + 36, py + (2 if head else 0)), f"{idx:02d}",
               font=f_num, fill=ACCENT)
        ty2 = py
        if head:
            d.text((card_x + 104, ty2), head, font=f_head_t, fill=TEXT)
            ty2 += hh
        for bl in body_lines:
            d.text((card_x + 104, ty2), bl, font=f_body_t, fill=TEXT_2)
            ty2 += bh
        py += block_h
        drawn += 1

    # ── ④ 结论条（位置已在上方算好，这里只画）──
    if conclusion and concl_lines:
        d.rounded_rectangle([card_x, concl_top, card_x + card_w, concl_top + concl_h],
                            radius=14, fill=INK)
        d.rectangle([card_x, concl_top, card_x + 5, concl_top + concl_h], fill=ACCENT)
        d.text((card_x + 28, concl_top + 16), "结论", font=f_meta, fill=(150, 156, 164))
        for i, line in enumerate(concl_lines):
            d.text((card_x + 28, concl_top + 44 + i * 38), line,
                   font=f_concl, fill=(255, 255, 255))

    # ── ⑤ 封面缩略（只在没有结论条时贴，避免重叠）──
    if cover_path and os.path.exists(cover_path) and not conclusion:
        try:
            with _PIL_OPEN(cover_path) as cv:
                cv = cv.convert("RGB")
                cw = 220
                ch = int(cv.height * cw / cv.width)
                cv = cv.resize((cw, ch))
                img.paste(cv, (W - 64 - cw, H - FOOTER_RESERVED - ch - 8))
        except Exception as e:
            logger.warning(f"一图流封面粘贴失败（忽略）: {e}")

    d.text((64, FOOTER_Y), "VidInsight · 内容洞察", font=f_meta, fill=MUTED)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    img.save(output_path, "PNG")
    logger.info(f"一图流（结构化）已生成: {output_path} "
                f"({W}x{H}, {drawn}/{len(clean_points)} 要点, 字号档={_hsz}/{_bsz})")
    return output_path


def render_one_pager(
    markdown: str,
    output_path: str,
    *,
    source_label: str = "",
    cover_path: Optional[str] = None,
    structured: Optional[dict] = None,
) -> Optional[str]:
    """把总结渲染成一图流 PNG。返回输出路径，失败返回 None（绝不抛给调用方）。

    :param structured: LLM 提炼后的结构（one_pager_distill.normalize 的产物）。
        传了就按它渲染（带小标题的要点卡）；没传就退回本地抽取（从 markdown
        里抓标题和列表项）——保证外部 LLM 不可用时功能仍然可用。
    """
    try:
        if structured and (structured.get("title") or structured.get("points")):
            return _render_structured(
                structured, output_path,
                source_label=source_label, cover_path=cover_path,
            )

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
