"""
VidInsight Harness —— 生产侧（produce）工具集。

设计原则：
1. 不拆碎 NoteGenerator（风险高），而是给它一层"工具包装"——
   每个 produce 工具 = 一次能力调用 + 结构化结果（含 success / 失败判据 / artifacts）。
2. 结构化结果是 harness 的关键：ReAct 环据此决定 next step（重试/换路/结束），
   而不是像旧管线那样一路跑到黑、失败只能看日志。
3. 受限工具（涉及第三方平台交互）显式标注 restricted=True，
   harness 与前端都据此做提示与节流 —— 对应产品红线「回归真人行为」。

工具清单：
- validate_video_url    : 校验并识别平台（只读，安全）
- generate_note         : 完整生成一条笔记（下载→转写→LLM）受限/耗时
- ingest_board          : 摄入小红书收藏专辑（被动捕获结果）受限
- list_supported_platforms : 列出支持的平台（只读，安全）
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── 平台能力表（与 validators 保持一致，供 Agent 查询） ──────────
SUPPORTED_PLATFORMS = {
    "bilibili": {"name": "哔哩哔哩", "url_hint": "bilibili.com/video"},
    "youtube": {"name": "YouTube", "url_hint": "youtube.com / youtu.be"},
    "douyin": {"name": "抖音", "url_hint": "douyin.com"},
    "kuaishou": {"name": "快手", "url_hint": "kuaishou.com"},
    "xiaohongshu": {"name": "小红书", "url_hint": "xiaohongshu.com"},
    "local": {"name": "本地文件", "url_hint": "本地路径"},
}

# URL → 平台 的粗识别（与后端 validator 镜像；此处只做 Agent 侧预检）
_URL_PLATFORM_PATTERNS: list[tuple[str, str]] = [
    (r"bilibili\.com|b23\.tv", "bilibili"),
    (r"youtube\.com|youtu\.be", "youtube"),
    (r"douyin\.com", "douyin"),
    (r"kuaishou\.com|chenzhongtech\.com", "kuaishou"),
    (r"xiaohongshu\.com|xhslink\.(?:com|cn)", "xiaohongshu"),
]


def detect_platform(url: str) -> Optional[str]:
    """从 URL 粗识别平台；无法识别返回 None。"""
    for pattern, platform in _URL_PLATFORM_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return platform
    return None


def _ok(**kwargs: Any) -> str:
    """成功结果：统一带 success=True。"""
    payload = {"success": True}
    payload.update(kwargs)
    return json.dumps(payload, ensure_ascii=False)


def _fail(reason: str, **kwargs: Any) -> str:
    """失败结果：统一带 success=False + reason（失败判据，供 ReAct 决策）。"""
    payload = {"success": False, "reason": reason}
    payload.update(kwargs)
    return json.dumps(payload, ensure_ascii=False)


# ═══════════════════════════════════════════════════
# 工具实现
# ═══════════════════════════════════════════════════


def validate_video_url(args: dict) -> str:
    """校验视频链接并识别平台（只读、安全、无副作用）。"""
    url = (args.get("url") or "").strip()
    if not url:
        return _fail(reason="链接为空")

    platform = detect_platform(url)
    if platform is None:
        # 本地文件路径也算一种输入
        if os.path.exists(url):
            return _ok(
                url=url,
                platform="local",
                platform_name=SUPPORTED_PLATFORMS["local"]["name"],
                supported=True,
                note="识别为本地文件路径",
            )
        return _fail(
            reason="无法识别平台",
            url=url,
            supported_platforms=list(SUPPORTED_PLATFORMS.keys()),
            hint="请确认链接来自支持的平台，或提供本地文件路径",
        )

    return _ok(
        url=url,
        platform=platform,
        platform_name=SUPPORTED_PLATFORMS[platform]["name"],
        supported=True,
    )


def list_supported_platforms(args: dict) -> str:
    """列出所有支持的平台（只读）。"""
    return _ok(
        total=len(SUPPORTED_PLATFORMS),
        platforms=[
            {"id": k, "name": v["name"], "url_hint": v["url_hint"]}
            for k, v in SUPPORTED_PLATFORMS.items()
        ],
    )


def generate_note(args: dict) -> str:
    """
    生成一条视频笔记（完整管线：下载 → 转写 → LLM 生成）。

    受限：会访问第三方平台；耗时：分钟级。
    harness 调用前应已用 validate_video_url 预检过。
    """
    url = (args.get("url") or "").strip()
    if not url:
        return _fail(reason="链接为空")

    platform = args.get("platform") or detect_platform(url)
    if not platform:
        return _fail(reason="无法识别平台，请先用 validate_video_url 预检", url=url)

    task_id = args.get("task_id")
    model_name = args.get("model_name")
    provider_id = args.get("provider_id")
    quality = args.get("quality") or "medium"
    link = bool(args.get("link", False))
    screenshot = bool(args.get("screenshot", False))
    style = args.get("style")

    try:
        from app.services.note import NoteGenerator

        generator = NoteGenerator()
        result = generator.generate(
            video_url=url,
            platform=platform,
            quality=quality,
            task_id=task_id,
            model_name=model_name,
            provider_id=provider_id,
            link=link,
            screenshot=screenshot,
            style=style,
        )
    except Exception as e:  # noqa: BLE001 —— 工具层统一收敛异常，交给 ReAct 决策
        logger.error(f"[produce.generate_note] 失败: {e}", exc_info=True)
        return _fail(
            reason=f"生成失败: {str(e)[:200]}",
            url=url,
            platform=platform,
            suggestion="可检查链接有效性、登录态或模型配置后重试",
        )

    if result is None:
        return _fail(reason="生成返回空结果", url=url, platform=platform)

    # NoteResult 可能是 pydantic 模型或 dict，做兼容抽取
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    return _ok(
        task_id=_get(result, "task_id") or task_id,
        title=_get(result, "title", ""),
        platform=platform,
        url=url,
        markdown_preview=(_get(result, "markdown", "") or "")[:800],
        artifacts={"note_dir": "note_results"},
        next_step="笔记已入库，可用 search_kb 检索或 get_source 查看全文",
    )


def ingest_board(args: dict) -> str:
    """
    摄入小红书收藏专辑（board）内容到洞察库。

    受限：必须基于"被动捕获窗口"已拿到的数据，工具本身**不做任何自动化爬取**。
    这是产品红线：对第三方平台只回归真人行为。
    """
    board_url = (args.get("board_url") or "").strip()
    if not board_url:
        return _fail(reason="专辑链接为空")

    if "xiaohongshu.com" not in board_url and "xhslink." not in board_url:
        return _fail(reason="不是小红书链接", board_url=board_url)

    board_id = args.get("board_id")
    if not board_id:
        m = re.search(r"/board/([0-9a-zA-Z]{16,32})", board_url)
        board_id = m.group(1) if m else None
    if not board_id:
        return _fail(
            reason="无法从链接解析 board_id",
            board_url=board_url,
            hint="请提供形如 xiaohongshu.com/board/<24位id> 的链接",
        )

    try:
        from app.downloaders.xiaohongshu_downloader import (
            XiaohongshuDownloader,
        )

        downloader = XiaohongshuDownloader()
        if not hasattr(downloader, "expand_board"):
            return _fail(
                reason="当前下载器未实现 expand_board",
                board_id=board_id,
                hint="需先通过被动捕获窗口浏览专辑，再由前端提交已捕获内容",
            )

        data = downloader.expand_board(board_url)
        return _ok(
            board_id=board_id,
            board_name=(data or {}).get("board_name", ""),
            total=(data or {}).get("total", 0),
            notes_count=len((data or {}).get("notes", []) or []),
            next_step="专辑内容已可检索，可用 search_kb 查询",
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"[produce.ingest_board] 失败: {e}", exc_info=True)
        return _fail(
            reason=f"专辑摄入失败: {str(e)[:200]}",
            board_id=board_id,
            suggestion=(
                "若为登录态失效（设备登录超限），需用户在捕获窗口重新扫码登录后重试；"
                "工具不会自动登录或爬取"
            ),
        )


def parse_video_link(args: dict) -> str:
    """
    全平台「贴链接即预览」：任何支持平台链接 → 秒级结构化元数据（不下载不转写）。

    六平台一个入口：bilibili/youtube/douyin 走 yt-dlp（产品同款 cookie/代理），
    xiaohongshu 走自研 SSR（视频/图文富字段），kuaishou 诚实返回 limited，
    其他平台 yt-dlp generic 兜底。受限：只取用户主动粘贴的链接，绝不自动登录、
    绝不翻页、绝不批量抓取 —— 产品红线「回归真人行为」。
    失败同样结构化（success=false + reason + hint），供 ReAct 环决策。
    """
    url = (args.get("url") or "").strip()
    if not url:
        return _fail(reason="链接为空", hint="请提供视频/笔记分享链接")

    try:
        from app.services.universal_parser import parse_link

        result = parse_link(url)
    except Exception as e:  # noqa: BLE001 —— 双保险，解析层内部已收敛
        logger.error(f"[produce.parse_video_link] 失败: {e}", exc_info=True)
        return _fail(reason=f"解析异常: {str(e)[:180]}", url=url[:120],
                     suggestion="请确认链接有效后重试")

    if not result.get("success"):
        result.setdefault(
            "suggestion",
            "可直接提交生成（走平台专用下载通道）；或检查链接是否来自 App/网页分享")
    return json.dumps(result, ensure_ascii=False)


def parse_xhs_note(args: dict) -> str:
    """
    快速解析单条小红书笔记（视频/图文元数据，秒级，不下载不转写）。

    受限：只取用户主动粘贴分享的这一条链接（xsec_token 匿名通道或被动捕获缓存），
    绝不自动登录、绝不翻页、绝不批量抓取 —— 产品红线「回归真人行为」。
    返回结构化元数据（标题/正文/标签/视频直链/配图），失败同样结构化（success=false + reason），
    供 ReAct 环决策重试或换路。
    """
    url = (args.get("url") or "").strip()
    if not url:
        return _fail(reason="链接为空", hint="请提供小红书笔记分享链接")

    try:
        from app.downloaders.xiaohongshu_downloader import XiaoHongShuDownloader

        result = XiaoHongShuDownloader().get_note_structured(url)
    except Exception as e:  # noqa: BLE001 —— 双保险，下载器内部已收敛
        logger.error(f"[produce.parse_xhs_note] 失败: {e}", exc_info=True)
        return _fail(reason=f"解析异常: {str(e)[:180]}", url=url[:120],
                     suggestion="请确认链接来自 App 分享（带 xsec_token）后重试")

    if not result.get("success"):
        # get_note_structured 已返回契约化失败，补上可行动建议后原样透传
        result.setdefault("suggestion",
                          "缺少 token 时请从 App 分享面板复制链接；登录态失效需在桌面端重新登录（工具不会自动登录）")
        return json.dumps(result, ensure_ascii=False)

    return json.dumps(result, ensure_ascii=False)


# ═══════════════════════════════════════════════════
# 工具注册表：name -> (schema, handler, restricted)
# ═══════════════════════════════════════════════════

PRODUCE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "validate_video_url",
            "description": (
                "校验视频链接并识别所属平台。只读、无副作用、秒级返回。"
                "在调用 generate_note 之前应先用它预检。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "视频链接或本地文件路径"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_supported_platforms",
            "description": "列出所有支持解析的平台及其链接特征。只读。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_note",
            "description": (
                "对一条视频链接执行完整分析：下载 → 转写 → LLM 生成结构化洞察笔记。"
                "耗时较长（分钟级）且会访问第三方平台，属受限工具。"
                "调用前请先用 validate_video_url 确认平台可识别。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "视频链接"},
                    "platform": {
                        "type": "string",
                        "description": "平台标识；不传则自动识别",
                    },
                    "task_id": {"type": "string", "description": "任务 ID（可选）"},
                    "link": {"type": "boolean", "description": "是否插入视频片段链接"},
                    "screenshot": {
                        "type": "boolean",
                        "description": "是否插入截图",
                    },
                    "style": {"type": "string", "description": "笔记风格（可选）"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_board",
            "description": (
                "摄入小红书「我的收藏专辑」内容到洞察库。受限工具："
                "仅处理用户已在被动捕获窗口中浏览过的自己的收藏，"
                "绝不自动登录、绝不爬取他人内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "board_url": {
                        "type": "string",
                        "description": "收藏专辑链接（含 /board/<id>）",
                    },
                    "board_id": {"type": "string", "description": "专辑 ID（可选，自动解析）"},
                },
                "required": ["board_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_video_link",
            "description": (
                "全平台「贴链接即预览」：任何支持平台的链接秒级解析出结构化元数据"
                "（标题/时长/作者/正文/标签/清晰度直链/封面），不下载不转写。"
                "支持 bilibili / youtube / douyin / xiaohongshu（含图文富字段）/ kuaishou（limited）"
                "及 yt-dlp 认识的其他站点。受限工具：只取用户主动粘贴的这一条链接。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "视频/笔记链接（支持各平台分享短链，如 b23.tv / youtu.be / xhslink 等）",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_xhs_note",
            "description": (
                "快速解析单条小红书笔记（视频/图文通吃，秒级，不下载不转写）。"
                "返回标题/正文/话题标签/作者/互动数/视频直链/配图列表。"
                "需要带 xsec_token 的分享链接，或用户在桌面端浏览过的笔记（被动捕获缓存）。"
                "受限工具：只取用户主动粘贴的这一条链接，绝不自动登录、绝不批量抓取。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "小红书笔记链接：xiaohongshu.com/explore/{id}?xsec_token=… "
                            "或 xhslink.com / xhslink.cn 短链"
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
]

PRODUCE_HANDLERS: dict[str, Any] = {
    "validate_video_url": validate_video_url,
    "list_supported_platforms": list_supported_platforms,
    "generate_note": generate_note,
    "ingest_board": ingest_board,
    "parse_xhs_note": parse_xhs_note,
    "parse_video_link": parse_video_link,
}

# 受限工具集合：涉及第三方平台交互，需提示/节流，禁止自动化
RESTRICTED_TOOLS = {"generate_note", "ingest_board", "parse_xhs_note", "parse_video_link"}
