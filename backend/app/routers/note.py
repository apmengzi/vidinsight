# app/routers/note.py
import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel, validator, field_validator
from dataclasses import asdict

from app.db.video_task_dao import get_task_by_video
from app.enmus.exception import NoteErrorEnum
from app.enmus.note_enums import DownloadQuality
from app.exceptions.biz_exception import BizException
from app.exceptions.codes import ErrorCode
from app.exceptions.note import NoteError
from app.services.note import NoteGenerator, logger
from app.services.task_cancel import TaskCancelled, cancel_task, clear_task, is_cancelled
from app.services.task_serial_executor import task_serial_executor
from app.utils.log_standard import log_event
from app.utils.response import ResponseWrapper as R
from app.utils.url_parser import extract_video_id
from app.utils.path_helper import get_runtime_path
from app.validators.video_url_validator import is_supported_video_url
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
import httpx
from app.enmus.task_status_enums import TaskStatus

# from app.services.downloader import download_raw_audio
# from app.services.whisperer import transcribe_audio

router = APIRouter()


class RecordRequest(BaseModel):
    video_id: str
    platform: str


class RecordRequestUrl(BaseModel):
    video_url: str


class VideoRequest(BaseModel):
    video_url: str
    platform: str
    quality: DownloadQuality
    screenshot: Optional[bool] = False
    link: Optional[bool] = False
    model_name: str
    provider_id: str
    task_id: Optional[str] = None
    format: Optional[list] = []
    style: str = None
    extras: Optional[str]=None
    video_understanding: Optional[bool] = False
    video_interval: Optional[int] = 0
    grid_size: Optional[list] = []
    # 客户端（如浏览器插件）已经在用户浏览器里抓到字幕，直接传给后端复用，
    # 跳过 download_subtitles 和音频转写。形如：
    #   {"language": "zh", "full_text": "...", "segments": [{"start","end","text"}, ...]}
    prefetched_transcript: Optional[dict] = None
    # hellotik 兜底拿到的无水印直链（仅 B 站）。主通道 yt-dlp 被 412 风控拦截时，
    # 后端改用它直接取片；为空就走常规通道，行为与以前完全一致。
    direct_url: Optional[str] = None
    # 兜底解析顺带拿到的标题/封面，直链通道无 yt-dlp 元数据，靠它补上
    direct_title: Optional[str] = None
    direct_cover: Optional[str] = None

    @field_validator("video_url")
    def validate_supported_url(cls, v):
        url = str(v)
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            # 是网络链接，继续用原有平台校验
            if not is_supported_video_url(url):
                raise NoteError(code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                                message=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.message)

        return v


NOTE_OUTPUT_DIR = os.getenv("NOTE_OUTPUT_DIR", get_runtime_path("note_results"))
UPLOAD_DIR = os.getenv("UPLOAD_DIR", get_runtime_path("uploads"))


def save_note_to_file(task_id: str, note):
    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json"), "w", encoding="utf-8") as f:
        json.dump(asdict(note), f, ensure_ascii=False, indent=2)


def _persist_prefetched_transcript(task_id: str, transcript: dict) -> None:
    """把客户端预取的字幕写到 NoteGenerator 期望的转写缓存文件里。

    NoteGenerator.generate 会优先读 <task_id>_transcript.json，命中即跳过 download_subtitles
    与音频转写流程。要求字段：language(可空)/full_text/segments[{start,end,text}]
    """
    segments = transcript.get("segments") or []
    cleaned_segments = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        cleaned_segments.append({
            "start": float(s.get("start", 0)),
            "end": float(s.get("end", 0)),
            "text": text,
        })
    if not cleaned_segments:
        raise ValueError("prefetched_transcript 没有可用的 segments")

    full_text = transcript.get("full_text") or " ".join(s["text"] for s in cleaned_segments)
    payload = {
        "language": transcript.get("language") or "zh",
        "full_text": full_text,
        "segments": cleaned_segments,
    }

    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    target = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}_transcript.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"已写入客户端预取字幕缓存: {target} ({len(cleaned_segments)} 段)")


def run_note_task(task_id: str, video_url: str, platform: str, quality: DownloadQuality,
                  link: bool = False, screenshot: bool = False, model_name: str = None, provider_id: str = None,
                  _format: list = None, style: str = None, extras: str = None, video_understanding: bool = False,
                  video_interval=0, grid_size=[]
                  ):

    if not model_name or not provider_id:
        raise HTTPException(status_code=400, detail="请选择模型和提供者")

    def _execute_note_task():
        return NoteGenerator().generate(
            video_url=video_url,
            platform=platform,
            quality=quality,
            task_id=task_id,
            model_name=model_name,
            provider_id=provider_id,
            link=link,
            _format=_format,
            style=style,
            extras=extras,
            screenshot=screenshot,
            video_understanding=video_understanding,
            video_interval=video_interval,
            grid_size=grid_size,
        )

    logger.info(f"任务进入执行队列 (task_id={task_id})")
    # executor 任务间检查：排队期间被取消的任务直接跳过，不进下载管线
    if is_cancelled(task_id):
        clear_task(task_id)
        logger.info(f"任务在排队期间被取消，跳过执行 (task_id={task_id})")
        return
    try:
        note = task_serial_executor.run(_execute_note_task)
    except TaskCancelled:
        # 已取消：状态文件已标记 CANCELLED，不保存、不索引
        clear_task(task_id)
        logger.info(f"任务已取消，跳过保存与索引 (task_id={task_id})")
        return
    logger.info(f"Note generated: {task_id}")
    if not note or not note.markdown:
        logger.warning(f"任务 {task_id} 执行失败，跳过保存")
        return
    save_note_to_file(task_id, note)

    # 自动建立向量索引（用于 AI 问答），失败不影响笔记生成
    # 1. 旧版按任务索引（保持兼容）
    try:
        from app.services.vector_store import VectorStoreManager
        VectorStoreManager().index_task(task_id)
    except Exception as e:
        logger.warning(f"旧版向量索引失败（不影响笔记）: {e}")

    # 2. 新版统一知识库索引（全局搜索 + ReAct Agent）
    try:
        from app.services.knowledge_base import UnifiedKnowledgeBase
        chunk_count = UnifiedKnowledgeBase().index_note(task_id)
        logger.info(f"统一知识库索引完成: task_id={task_id}, chunks={chunk_count}")
    except Exception as e:
        logger.warning(f"统一知识库索引失败（不影响笔记）: {e}")


@router.post('/delete_task')
def delete_task(data: RecordRequest):
    try:
        from app.db.video_task_dao import get_tasks_by_video
        from app.routers.chat import clear_index_status
        from app.services.knowledge_base import get_unified_knowledge_base
        from app.services.vector_store import VectorStoreManager

        task_ids = get_tasks_by_video(data.video_id, data.platform)
        deleted_files = 0
        for task_id in task_ids:
            try:
                get_unified_knowledge_base().delete_note(task_id)
            except Exception as e:
                logger.warning(f"统一知识库索引删除失败 (task_id={task_id}): {e}")
            try:
                VectorStoreManager().delete_index(task_id)
            except Exception as e:
                logger.warning(f"旧版向量索引删除失败 (task_id={task_id}): {e}")
            deleted_files += NoteGenerator.delete_note_files(task_id)

        clear_index_status(task_ids)

        deleted_count = NoteGenerator.delete_note(
            video_id=data.video_id,
            platform=data.platform,
        )
        return R.success(
            msg='删除成功',
            data={
                "deleted_tasks": deleted_count,
                "deleted_indexes": len(task_ids),
                "deleted_files": deleted_files,
            },
        )
    except Exception as e:
        return R.error(msg=str(e))


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_location = os.path.join(UPLOAD_DIR, file.filename)

    with open(file_location, "wb+") as f:
        f.write(await file.read())

    # 假设你静态目录挂载了 /uploads
    return R.success({"url": f"/uploads/{file.filename}"})


@router.post("/generate_note")
def generate_note(data: VideoRequest, background_tasks: BackgroundTasks):
    try:
        # 就绪门禁：本地转写引擎（fast-whisper / mlx-whisper）必须等模型下载完才能跑视频，
        # 否则任务会卡在首次下载（慢 / OOM / 截断），用户只看到一个静默失败的任务。
        # 客户端已抓好字幕（prefetched_transcript）则不需要转写，跳过检查。
        if not data.prefetched_transcript:
            from app.services.transcriber_config_manager import TranscriberConfigManager
            readiness = TranscriberConfigManager().is_model_ready()
            if not readiness["ready"]:
                logger.warning(f"拒绝 generate_note：{readiness['reason']}")
                return R.error(
                    msg=readiness["reason"],
                    code=300102,
                    data={
                        "reason": "transcriber_model_not_ready",
                        "transcriber_type": readiness["transcriber_type"],
                        "model_size": readiness["model_size"],
                        "downloading": readiness["downloading"],
                    },
                )

        video_id = extract_video_id(data.video_url, data.platform)
        # if not video_id:
        #     raise HTTPException(status_code=400, detail="无法提取视频 ID")
        # existing = get_task_by_video(video_id, data.platform)
        # if existing:
        #     return R.error(
        #         msg='笔记已生成，请勿重复发起',
        #
        #     )
        if data.task_id:
            # 如果传了task_id，说明是重试！
            task_id = data.task_id
            logger.info(f"重试模式，复用已有 task_id={task_id}")
        else:
            # 正常新建任务
            task_id = str(uuid.uuid4())

        # 统一先写入 PENDING，表示已进入队列等待串行执行
        NoteGenerator()._update_status(task_id, TaskStatus.PENDING)

        # 客户端已经抓好字幕的话，写到转写缓存文件，NoteGenerator 的 cache-hit 逻辑会直接用上
        if data.prefetched_transcript:
            try:
                _persist_prefetched_transcript(task_id, data.prefetched_transcript)
            except Exception as e:
                logger.warning(f"写入预取字幕失败 (task_id={task_id}): {e}")

        # 前端用第三方网页解析到无水印直链时登记，主通道被风控拦截才启用
        if data.direct_url:
            try:
                from app.services.note import put_direct_url

                put_direct_url(task_id, data.direct_url, data.direct_title, data.direct_cover)
                logger.info(f"已登记 hellotik 直链兜底 (task_id={task_id})")
            except Exception as e:
                logger.warning(f"登记直链兜底失败 (task_id={task_id}): {e}")

        background_tasks.add_task(run_note_task, task_id, data.video_url, data.platform, data.quality, data.link,
                                  data.screenshot, data.model_name, data.provider_id, data.format, data.style,
                                  data.extras, data.video_understanding, data.video_interval, data.grid_size)
        return R.success({"task_id": task_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/task_status/{task_id}")
def get_task_status(task_id: str):
    status_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.status.json")
    result_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")

    def _sanitize(obj):
        """递归清洗 inf/nan：FastAPI JSON 序列化遇非有限浮点直接 500，
        会把一份成功的报告伪装成"内容分析失败"。转写时间戳偶发 inf（VAD 边界），
        这里统一折为 None 保住报告本体。"""
        if isinstance(obj, float):
            return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    # 优先读状态文件
    if os.path.exists(status_path):
        with open(status_path, "r", encoding="utf-8") as f:
            status_content = json.load(f)

        status = status_content.get("status")
        message = status_content.get("message", "")

        if status == TaskStatus.SUCCESS.value:
            # 成功状态的话，继续读取最终笔记内容
            if os.path.exists(result_path):
                with open(result_path, "r", encoding="utf-8") as rf:
                    result_content = json.load(rf)
                return R.success({
                    "status": status,
                    "result": _sanitize(result_content),
                    "message": message,
                    "elapsed_s": status_content.get("elapsed_s"),
                    "stage_times": _sanitize(status_content.get("stage_times") or {}),
                    # 阶段内进度：0-100，null 表示该阶段不可量化（前端应隐藏进度条）
                    "stage_progress": _sanitize(status_content.get("stage_progress")),
                    "task_id": task_id
                })
            else:
                # 理论上不会出现，保险处理
                return R.success({
                    "status": TaskStatus.PENDING.value,
                    "message": "任务完成，但结果文件未找到",
                    "task_id": task_id
                })

        if status == TaskStatus.FAILED.value:
            return R.error(message or "任务失败", code=500)

        # 处理中状态
        return R.success({
            "status": status,
            "message": message,
            "progress": status_content.get("progress"),
            # 阶段内进度：0-100，null 表示该阶段不可量化（前端应隐藏进度条）
            "stage_progress": _sanitize(status_content.get("stage_progress")),
            # EVT Must Fix 状态可见性：已用时长/阶段耗时明细（前端算粗估剩余）
            "elapsed_s": status_content.get("elapsed_s"),
            "stage_times": _sanitize(status_content.get("stage_times") or {}),
            "task_id": task_id
        })

    # 没有状态文件，但有结果
    if os.path.exists(result_path):
        with open(result_path, "r", encoding="utf-8") as f:
            result_content = json.load(f)
        return R.success({
            "status": TaskStatus.SUCCESS.value,
            "result": _sanitize(result_content),
            "task_id": task_id
        })

    # 什么都没有，默认PENDING
    return R.success({
        "status": TaskStatus.PENDING.value,
        "message": "任务排队中",
        "task_id": task_id
    })


@router.get("/tasks/list")
def list_tasks(limit: int = 100, offset: int = 0):
    """历史页后端兜底：列出 note_results 里的全部任务（含成功产物 markdown）。

    前端任务列表只存浏览器 IndexedDB（taskStore persist），换浏览器/清缓存/
    重装后历史就空了，但后端产物文件还在。历史页挂载时调此端点补齐，
    以 task_id 去重合并，本地记录优先不覆盖。失败任务（无结果 json）也列出，
    标题未知时用视频 ID 兜底。
    """
    try:
        items = []
        for name in os.listdir(NOTE_OUTPUT_DIR):
            if not name.endswith(".status.json"):
                continue
            if "_audio" in name or "_markdown" in name:
                continue
            tid = name[:-len(".status.json")]
            status_path = os.path.join(NOTE_OUTPUT_DIR, name)
            try:
                with open(status_path, "r", encoding="utf-8") as f:
                    sc = json.load(f)
            except Exception:
                continue
            status = sc.get("status", "PENDING")
            started_at = sc.get("started_at") or os.path.getmtime(status_path)
            item = {
                "task_id": tid,
                "status": status,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started_at)),
                "title": "",
                "platform": "",
                "video_id": "",
                "cover_url": "",
                "duration": 0,
                "video_url": "",
                "markdown": "",
            }
            result_path = os.path.join(NOTE_OUTPUT_DIR, f"{tid}.json")
            if os.path.exists(result_path):
                try:
                    with open(result_path, "r", encoding="utf-8") as f:
                        result = json.load(f)
                    am = result.get("audio_meta") or {}
                    md = result.get("markdown") or ""
                    item.update({
                        "title": am.get("title") or "",
                        "platform": am.get("platform") or "",
                        "video_id": am.get("video_id") or "",
                        "cover_url": am.get("cover_url") or "",
                        "duration": am.get("duration") or 0,
                        "markdown": md,
                    })
                    # 来源链接在 markdown 首行（"来源链接：URL"），重试功能需要
                    m = re.search(r"来源链接[:：]\s*(\S+)", md[:2000])
                    if m:
                        item["video_url"] = m.group(1)
                except Exception:
                    pass
            items.append(item)
        items.sort(key=lambda x: x["created_at"], reverse=True)
        total = len(items)
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        return R.success({"total": total, "items": items[offset:offset + limit]})
    except Exception as e:
        log_event(logger, "tasks.list.fail", level=logging.ERROR, error=str(e)[:200])
        return R.error(msg=f"获取任务列表失败: {str(e)[:150]}")


@router.post("/tasks/{task_id}/cancel")
def cancel_note_task(task_id: str):
    """取消长任务。

    协作式取消：设置取消标志后，任务在下一个阶段边界（下载/转写/每轮 LLM 调用之间）
    响应并抛出 TaskCancelled，状态流转为 CANCELLED。
    无法打断正在飞行中的单次网络/LLM 调用——这是该机制的延迟上界。
    """
    try:
        status_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.status.json")
        if os.path.exists(status_path):
            with open(status_path, "r", encoding="utf-8") as f:
                current = json.load(f).get("status")
            if current in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value,
                           TaskStatus.CANCELLED.value):
                return R.error(msg=f"任务已是终态（{current}），无法取消")

        already_done = not os.path.exists(status_path) and os.path.exists(
            os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json"))
        if already_done:
            return R.error(msg="任务已完成，无法取消")

        cancel_task(task_id)
        # 立即写 CANCELLED 状态：排队中的任务由 executor 跳过；
        # 执行中的任务由阶段边界检查保持/确认该状态
        NoteGenerator()._update_status(
            task_id, TaskStatus.CANCELLED, message="用户已取消")
        logger.info(f"收到取消请求 (task_id={task_id})")
        return R.success({"task_id": task_id, "status": TaskStatus.CANCELLED.value})
    except TaskCancelled:
        return R.success({"task_id": task_id, "status": TaskStatus.CANCELLED.value})
    except Exception as e:
        return R.error(msg=f"取消失败: {str(e)}")


@router.post("/xhs/expand_collection")
def expand_xhs_collection(data: RecordRequestUrl):
    """展开小红书合集/专辑链接为逐条笔记清单（批量总结的输入）。

    建议先在设置页「下载配置 → 小红书」配置网页版 Cookie（登录态）：
    登录态下合集内笔记可免 xsec_token 抓取，且不再受 token 时效影响。
    """
    try:
        from app.downloaders.xiaohongshu_downloader import XiaoHongShuDownloader
        data_obj = XiaoHongShuDownloader().expand_collection(data.video_url)
        log_event(logger, "collection.expand.done", platform="xiaohongshu",
                  total=data_obj.get("total"),
                  tokened=sum(1 for n in data_obj.get("notes", []) if n.get("has_token")))
        return R.success(data_obj)
    except BizException as e:
        return R.error(msg=e.message, code=e.code, err=e.err, detail=e.detail)
    except Exception as e:
        spec = ErrorCode.COLLECTION_EXPAND_FAILED
        log_event(logger, "collection.expand.fail", level=logging.ERROR,
                  platform="xiaohongshu", err=spec.err, error=str(e), exc_info=e)
        return R.error(msg=str(e) or spec.message, code=spec.code, err=spec.err)


# ── WebView 兜底元数据暂存（真人浏览器内核取数回传）──
# Rust 侧注入脚本从 B 站页面 __INITIAL_STATE__ 取 videoData 后 POST 到这里，
# 前端轮询 GET 取回。内存暂存（预览用途，无需持久化），5 分钟自动过期。
_webview_meta_store: dict = {}  # {task_id: {"data": {...}, "ts": monotonic}}


@router.post("/parse/webview_meta")
def webview_meta_ingest(payload: dict):
    import time as _t
    task = str(payload.get("task") or "")
    data = payload.get("data")
    if not task or not isinstance(data, dict):
        return R.error("task 与 data 必填")
    # 上限清理：过期（>300s）或超量（>100）条目
    now = _t.time()
    for k in [k for k, v in _webview_meta_store.items() if now - v["ts"] > 300]:
        _webview_meta_store.pop(k, None)
    if len(_webview_meta_store) > 100:
        _webview_meta_store.clear()
    _webview_meta_store[task] = {"data": data, "ts": now}
    return R.success({"received": True})


@router.get("/parse/webview_meta/{task_id}")
def webview_meta_fetch(task_id: str):
    entry = _webview_meta_store.get(task_id)
    if not entry:
        return R.success({"ready": False})
    _webview_meta_store.pop(task_id, None)
    return R.success({"ready": True, "data": entry["data"]})


# ── hellotik 兜底直链暂存（B 站专用）──────────────────────
# Rust 侧在 hellotik 网页里照真人操作（贴链接 → 点解析）后，从结果页读到的
# 平台 CDN 无水印直链 POST 到这里，前端轮询取回。与 webview_meta 同款内存暂存。
_hellotik_store: dict = {}  # {task_id: {"data": {...}, "ts": monotonic}}


@router.post("/parse/hellotik")
def hellotik_ingest(payload: dict):
    import time as _t

    from app.services.hellotik import parse_result_payload

    task = str(payload.get("task") or "")
    data = payload.get("data")
    if not task or not isinstance(data, dict):
        return R.error("task 与 data 必填")

    # 直链必须在后端校验（CDN 白名单 + 形态检查），注入脚本不可信
    result = parse_result_payload(data, source_url=str(data.get("source_url") or ""))
    now = _t.time()
    for k in [k for k, v in _hellotik_store.items() if now - v["ts"] > 300]:
        _hellotik_store.pop(k, None)
    if len(_hellotik_store) > 100:
        _hellotik_store.clear()
    _hellotik_store[task] = {"data": result, "ts": now}

    if result.get("success"):
        log_event(logger, "parse.hellotik_ok", task=task,
                  title=(result.get("title") or "")[:40])
    else:
        log_event(logger, "parse.hellotik_fail", level=logging.INFO,
                  task=task, reason=result.get("reason"))
    return R.success({"received": True, "success": result.get("success", False)})


@router.get("/parse/hellotik/{task_id}")
def hellotik_fetch(task_id: str):
    entry = _hellotik_store.get(task_id)
    if not entry:
        return R.success({"ready": False})
    _hellotik_store.pop(task_id, None)
    return R.success({"ready": True, "data": entry["data"]})


@router.post("/parse")
def universal_parse(data: RecordRequestUrl):
    """全平台「贴链接即预览」：任何支持平台的链接 → 秒级结构化元数据。

    六平台一个入口（hellotik 式体验，but 全平台）：
    - bilibili / youtube / douyin → yt-dlp 引擎（产品同款 cookie/代理）
    - xiaohongshu → 自研 SSR（含图文富字段）
    - kuaishou → 诚实返回 limited（无快速通道，生成管线不受影响）
    - 其他 → yt-dlp generic 兜底（1000+ 站点）
    失败契约与 /xhs/parse 一致：结构化失败也 200 + success=false。
    """
    try:
        from app.services.universal_parser import parse_link
        result = parse_link(data.video_url)
    except Exception as e:  # noqa: BLE001 —— 解析层内部已收敛，这里只兜不可预期异常
        log_event(logger, "parse.fail", level=logging.ERROR,
                  err="PARSE_UNEXPECTED", error=str(e), exc_info=e)
        return R.error(msg=f"解析失败: {str(e)[:150]}", err="PARSE_UNEXPECTED")
    if not result.get("success"):
        log_event(logger, "parse.structured_fail", level=logging.INFO,
                  platform=str(result.get("platform", "")),
                  reason=str(result.get("reason", ""))[:80])
    else:
        log_event(logger, "parse.done", platform=result.get("platform"),
                  engine=result.get("engine"), title=result.get("title", "")[:40])
    return R.success(result)


@router.post("/xhs/parse")
def xhs_parse(data: RecordRequestUrl):
    """贴链接即解析：单条小红书笔记的结构化元数据（视频/图文通吃，秒级，不下载）。

    返回标题/正文/话题标签/作者/互动数/视频直链/配图列表，供前端预览
    与 Agent「先看一眼再决定是否完整生成」。失败同样结构化返回
    （success=false + reason + hint），前端据此给可行动提示。

    红线边界：只取用户主动粘贴分享的这一条链接（xsec_token 匿名通道
    或被动捕获缓存），不自动登录、不翻页、不批量抓取。
    """
    try:
        from app.downloaders.xiaohongshu_downloader import XiaoHongShuDownloader
        result = XiaoHongShuDownloader().get_note_structured(data.video_url)
    except Exception as e:  # noqa: BLE001 —— 下载器内部已收敛，这里只兜不可预期异常
        log_event(logger, "xhs.parse.fail", level=logging.ERROR,
                  err="XHS_PARSE_UNEXPECTED", error=str(e), exc_info=e)
        return R.error(msg=f"解析失败: {str(e)[:150]}", err="XHS_PARSE_UNEXPECTED")
    if not result.get("success"):
        log_event(logger, "xhs.parse.structured_fail", level=logging.INFO,
                  reason=str(result.get("reason", ""))[:80])
    else:
        log_event(logger, "xhs.parse.done", note_id=result.get("note_id"),
                  note_type=result.get("note_type"),
                  images=len(result.get("images") or []))
    # 结构化失败也走 200 + success 字段（harness 失败契约），前端按 success 分支
    return R.success(result)


@router.post("/xhs/ingest_state")
async def ingest_xhs_state(request: Request):
    """接收桌面端隐藏 WebView 回传的小红书笔记节点，写入进程内缓存。

    页面 JS 以 text/plain 发送（简单请求，免 CORS 预检），所以这里
    手动解析 body 而不是用 Pydantic 模型（后者会拒收 text/plain）。
    """
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8", "ignore"))
        note_id = str(payload.get("note_id") or "")
        note = payload.get("note")
        # WebView 页面 JS 异常时可能回传字面量 "undefined"；只收合法的小红书笔记 ID
        if not re.fullmatch(r"[0-9a-f]{24}", note_id) or not isinstance(note, dict):
            log_event(logger, "xhs.ingest.rejected", level=logging.WARNING,
                      note_id=note_id[:40])
            return R.error(msg="无效的笔记数据", err="XHS_INGEST_INVALID")
        from app.services.xhs_note_cache import XHSNoteCache
        XHSNoteCache.put(note_id, note)
        log_event(logger, "xhs.ingest.note_cached", note_id=note_id,
                  desc_len=len(note.get("desc") or ""),
                  cache_size=XHSNoteCache.size())
        return R.success({"cached": note_id})
    except Exception as e:
        log_event(logger, "xhs.ingest.fail", level=logging.ERROR, error=str(e))
        return R.error(msg=str(e))


@router.post("/xhs/ingest_tokens")
async def ingest_xhs_tokens(request: Request):
    """接收桌面端 WebView 从合集/主页页面收割的 xsec_token 卡片链接。

    页面 JS 以 text/plain 发送（简单请求，免 CORS 预检），手动解析 body。
    token 与 xsec_source 配套，存完整 URL，expand_collection 合并时原样使用。
    """
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8", "ignore"))
        links = payload.get("links") or []
        if not isinstance(links, list) or not links:
            log_event(logger, "xhs.token.ingest_rejected", level=logging.WARNING)
            return R.error(msg="没有可用的链接数据", err="XHS_INGEST_INVALID")
        from app.downloaders.xiaohongshu_downloader import XiaoHongShuDownloader
        from app.services.xhs_note_cache import XHSTokenStore
        parsed = XiaoHongShuDownloader.parse_token_links(links)
        for nid, url in parsed.items():
            XHSTokenStore.put(nid, url)
        log_event(logger, "xhs.token.harvest", tokens=len(parsed),
                  raw=len(links))
        return R.success({"tokens": len(parsed)})
    except Exception as e:
        log_event(logger, "xhs.token.ingest_fail", level=logging.ERROR,
                  error=str(e))
        return R.error(msg=str(e))


@router.post("/xhs/ingest_board_notes")
async def ingest_xhs_board_notes(request: Request):
    """接收桌面端被动捕获窗口从用户收藏专辑页顺路记录的笔记条目。

    页面 JS 以 text/plain 发送（简单请求，免 CORS 预检），手动解析 body。
    每条只存轻量元数据（note_id/标题/类型/可选 token），正文详情仍走
    XHSNoteCache（用户点开过）或登录态 SSR。数据是「勾选工作台」的输入。
    """
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8", "ignore"))
        board_id = str(payload.get("board_id") or "")
        entries = payload.get("notes") or []
        if not re.fullmatch(r"[0-9a-f]{24}", board_id) or not isinstance(entries, list):
            log_event(logger, "xhs.board.ingest_rejected", level=logging.WARNING,
                      board_id=board_id[:40])
            return R.error(msg="无效的专辑数据", err="XHS_INGEST_INVALID")
        from app.services.board_note_store import BoardNoteStore
        from app.services.cover_persist import persist_covers
        accepted = 0
        covers: list[tuple[str, str]] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            nid = str(e.get("note_id") or "")
            if not re.fullmatch(r"[0-9a-f]{24}", nid):
                continue
            cover = str(e.get("cover") or "")[:500]
            BoardNoteStore.put(board_id, {
                "note_id": nid,
                "title": str(e.get("title") or "未命名")[:120],
                "type": str(e.get("type") or ""),
                "cover": cover,
                "xsec_token": str(e.get("xsec_token") or ""),
                "xsec_source": str(e.get("xsec_source") or ""),
            })
            if cover:
                covers.append((nid, cover))
            accepted += 1
        # 封面只有「捕获那一刻」是新鲜的：签名 URL 的路径段过期后一律 403。
        # 所以 ingest 时就落盘（线程池 4，已存在则跳过），失败保留原 URL 作 fallback。
        # 走 to_thread：同步下载绝不能占住事件循环（否则 harvest_signal 轮询、
        # 前端其它请求全被卡住）；页面 JS 是每条笔记一发，单请求通常只下 1 张。
        cover_stats = {"downloaded": 0, "skipped": 0, "failed": 0}
        if covers:
            try:
                cover_stats = await asyncio.to_thread(persist_covers, board_id, covers)
                BoardNoteStore.set_cover_locals(board_id, cover_stats.get("items") or {})
            except Exception as ce:  # noqa: BLE001 —— 封面失败不能丢捕获数据
                log_event(logger, "xhs.board.cover_fail", level=logging.WARNING,
                          board_id=board_id, error=str(ce)[:200])
        log_event(logger, "xhs.board.ingest", board_id=board_id,
                  accepted=accepted, total=BoardNoteStore.size(board_id),
                  cover_downloaded=cover_stats.get("downloaded", 0),
                  cover_skipped=cover_stats.get("skipped", 0),
                  cover_failed=cover_stats.get("failed", 0))
        return R.success({"accepted": accepted, "total": BoardNoteStore.size(board_id),
                          "covers": {k: cover_stats.get(k, 0)
                                     for k in ("downloaded", "skipped", "failed")}})
    except Exception as e:
        log_event(logger, "xhs.board.ingest_fail", level=logging.ERROR, error=str(e))
        return R.error(msg=str(e))


@router.get("/xhs/board/{board_id}/notes")
def get_xhs_board_notes(board_id: str):
    """收藏专辑勾选工作台的数据源：被动捕获到的专辑笔记清单（倒序）。"""
    if not re.fullmatch(r"[0-9a-f]{24}", board_id):
        return R.error(msg="无效的专辑 ID", err="XHS_INGEST_INVALID")
    from app.services.board_note_store import BoardNoteStore
    from app.services.xhs_note_cache import XHSNoteCache
    notes = BoardNoteStore.list_notes(board_id)
    # 详情可得性标注：用户点开过的笔记有正文缓存，勾选后直接可生成
    for n in notes:
        n["detail_cached"] = XHSNoteCache.get(n["note_id"]) is not None
    return R.success({"board_id": board_id, "total": len(notes), "notes": notes})


@router.post("/xhs/ingest_cookies")
async def ingest_xhs_cookies(request: Request):
    """WebView 登录 cookie 回写 downloader.json（收割成功后 Rust 自动调用）。

    登录态冗余：让后端 curl 通道拿到与 webview 同步的新鲜 session，
    避免存量 cookie 过期毒化请求（2026-09-15 实测：死 cookie 会把带
    token 的详情页顶成登录墙壳页）。
    """
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8", "ignore"))
        cookie = str(payload.get("cookie") or "")
        platform = str(payload.get("platform") or "xiaohongshu")
        if not cookie or "web_session" not in cookie:
            return R.error(msg="cookie 缺失或无 web_session", err="XHS_COOKIE_INVALID")
        from app.services.cookie_manager import CookieConfigManager
        CookieConfigManager().set(platform, cookie)
        log_event(logger, "xhs.cookie.synced", platform=platform,
                  cookie_len=len(cookie))
        return R.success({"platform": platform, "cookie_len": len(cookie)})
    except Exception as e:
        log_event(logger, "xhs.cookie.sync_fail", level=logging.ERROR, error=str(e))
        return R.error(msg=str(e))


@router.get("/xhs/cookie_status")
def xhs_cookie_status():
    """登录态预检（2026-10-19）：用户点「读取专辑 / 刷新清单」前先打一发，
    失效就明确提示「登录态已过期，请打开捕获窗口重新登录」，
    而不是让用户在白板页里看着空清单自己猜。

    返回（前端契约）：
        {"xhs": {"valid": bool, "hint": str, "status": str, "detail": str},
         "bilibili": {...}}
    valid=true 表示可直接走 HTTP 通道；unknown（网络/风控探测失败）按 true 放行，
    不因探测本身失败挡住用户。
    """
    from app.services.platform_guard import check_platform_cookie

    def _item(platform: str) -> dict:
        r = check_platform_cookie(platform)
        status = r.get("status") or "unknown"
        valid = status in ("logged_in", "anonymous", "unknown")
        hint = "" if valid else (r.get("action") or r.get("detail") or "登录态不可用")
        return {"valid": valid, "hint": hint, "status": status,
                "detail": r.get("detail") or ""}

    # 两个平台并行探测（各自 8~10s 超时；串行最坏 18s，前端页面加载会明显卡）
    with ThreadPoolExecutor(max_workers=2) as pool:
        fx = pool.submit(_item, "xiaohongshu")
        fb = pool.submit(_item, "bilibili")
        return R.success({"xhs": fx.result(), "bilibili": fb.result()})


class CollectionExpandRequest(BaseModel):
    video_url: str
    platform: str = ""


# ── 收割窗口信号中转（WebView JS → 这里 → Rust 轮询）────────────
# 外部源（xiaohongshu.com）用不了 Tauri IPC；document.title 也不会同步到原生
# 窗口标题——v3 用标题传信号的通道实测断裂，Rust 永远收不到终态，窗口才会
# 卡住直到 6 分钟看门狗。这里复用已验证可行的「页面 fetch 127.0.0.1:8483」
# 通路（text/plain 简单请求，无 CORS 预检）做信号中转。
_HARVEST_SIGNALS: dict[str, dict] = {}


def _prune_harvest_signals() -> None:
    now = time.time()
    for k in [k for k, v in _HARVEST_SIGNALS.items() if now - v.get("ts", 0) > 600]:
        _HARVEST_SIGNALS.pop(k, None)


@router.post("/xhs/harvest_signal")
async def post_xhs_harvest_signal(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return R.error(msg="bad json", err="HARVEST_SIGNAL_INVALID")
    label = str(payload.get("label") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label):
        return R.error(msg="bad label", err="HARVEST_SIGNAL_INVALID")
    _prune_harvest_signals()
    event = str(payload.get("event") or "")[:32]
    _HARVEST_SIGNALS[label] = {"event": event, "data": payload.get("data"),
                               "ts": time.time()}
    if event not in ("phase", "login"):  # 高频事件不刷屏
        if event == "diag":
            # 诊断探针（收割 0 token 根因定位）：payload 原样进结构化日志
            log_event(logger, "xhs.harvest.diag", label=label,
                      **(payload.get("data") or {}))
        elif event == "fail" and isinstance(payload.get("data"), dict) \
                and payload["data"].get("diag"):
            # 被动模式空页面诊断：fail 携带页面实况（path/ready/bodyLen/ua）
            log_event(logger, "xhs.harvest.signal", label=label, signal=event,
                      diag=str(payload["data"]["diag"])[:400])
        else:
            log_event(logger, "xhs.harvest.signal", label=label, signal=event)
    return R.success({"ok": True})


@router.get("/xhs/harvest_signal")
def get_xhs_harvest_signal(label: str):
    return R.success(_HARVEST_SIGNALS.get(label))


def _expand_yt_dlp_collection(url: str) -> dict:
    """YouTube 播放列表 / B站合集：yt-dlp 平铺枚举成员视频。"""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist"}
    # YouTube 需要代理时复用系统代理配置（与 OpenAI 客户端同一来源）
    try:
        from app.services.proxy_config_manager import ProxyConfigManager
        proxy_url = ProxyConfigManager().get_proxy_url()
        if proxy_url:
            opts["proxy"] = proxy_url
    except Exception:
        pass
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    notes = []
    for e in info.get("entries") or []:
        if not e:
            continue
        eid = e.get("id") or ""
        eu = e.get("url") or ""
        if "youtube.com/watch" in eu or e.get("ie_key") == "Youtube":
            watch = eu if eu.startswith("http") else f"https://www.youtube.com/watch?v={eid}"
        elif "bilibili.com/video" in eu or eid.startswith("BV"):
            watch = eu if eu.startswith("http") else f"https://www.bilibili.com/video/{eid}"
        else:
            watch = eu if eu.startswith("http") else ""
        if not watch:
            continue
        notes.append({
            "note_id": eid,
            "title": (e.get("title") or "未命名")[:80],
            "type": "video",
            "url": watch,
        })
    return {
        "name": info.get("title") or "合集",
        "total": len(notes),
        "logged_in": True,
        "notes": notes,
    }


@router.post("/collections/expand")
def expand_collection_generic(data: CollectionExpandRequest):
    """通用合集展开：小红书合集 / YouTube 播放列表 / B站合集（yt-dlp 平铺）。

    抖音、快手的合集依赖其签名 API（a_bogus / __NS_sig3），无公开
    免签名通道，暂不支持——返回明确的错误说明而不是静默失败。
    """
    url = data.video_url
    platform = data.platform
    if not platform:
        if "xiaohongshu.com" in url:
            platform = "xiaohongshu"
        elif "youtube.com" in url or "youtu.be" in url:
            platform = "youtube"
        elif "bilibili.com" in url or "b23.tv" in url:
            platform = "bilibili"
        elif "douyin.com" in url:
            platform = "douyin"
        elif "kuaishou.com" in url:
            platform = "kuaishou"
    try:
        if platform == "xiaohongshu":
            from app.downloaders.xiaohongshu_downloader import XiaoHongShuDownloader
            d = XiaoHongShuDownloader()
            # /board/{id} = 收藏专辑：壳解析 + 被动捕获缓存合并（勾选工作台数据）
            if re.search(r"/board/[0-9a-f]{24}", url):
                result = d.expand_board(url)
                log_event(logger, "collection.expand.done", platform="xiaohongshu-board",
                          board_id=result.get("board_id"), captured=result.get("captured"))
                return R.success(result)
            result = d.expand_collection(url)
            log_event(logger, "collection.expand.done", platform="xiaohongshu",
                      total=result.get("total"),
                      tokened=sum(1 for n in result.get("notes", []) if n.get("has_token")))
            return R.success(result)
        if platform in ("youtube", "bilibili"):
            result = _expand_yt_dlp_collection(url)
            log_event(logger, "collection.expand.done", platform=platform,
                      total=result.get("total"))
            return R.success(result)
        spec = ErrorCode.COLLECTION_PLATFORM_UNSUPPORTED
        log_event(logger, "collection.expand.unsupported", level=logging.WARNING,
                  platform=platform or "unknown", err=spec.err)
        return R.error(
            msg=f"{platform or '该平台'}的合集需要平台签名接口（a_bogus / __NS_sig3），"
                "目前不支持合集批量解析；可以逐条分享笔记链接解析。",
            code=spec.code, err=spec.err, detail={"platform": platform})
    except BizException as e:
        return R.error(msg=e.message, code=e.code, err=e.err, detail=e.detail)
    except Exception as e:
        spec = ErrorCode.COLLECTION_EXPAND_FAILED
        log_event(logger, "collection.expand.fail", level=logging.ERROR,
                  platform=platform or "unknown", err=spec.err,
                  error=str(e), exc_info=e)
        return R.error(msg=str(e) or spec.message, code=spec.code, err=spec.err)


@router.get("/image_proxy")
async def image_proxy(request: Request, url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="unsupported image url")

    # 根据图片 URL 自动检测平台，设置对应 Referer 避免防盗链拦截
    referer = "https://www.bilibili.com/"  # 默认
    url_lower = url.lower()
    if "douyin.com" in url_lower or "douyinvod.com" in url_lower or "tiktok.com" in url_lower:
        referer = "https://www.douyin.com/"
    elif "kuaishou.com" in url_lower:
        referer = "https://www.kuaishou.com/"
    elif "xiaohongshu.com" in url_lower or "xhscdn.com" in url_lower:
        referer = "https://www.xiaohongshu.com/"
    elif "youtube.com" in url_lower or "ytimg.com" in url_lower:
        referer = "https://www.youtube.com/"
    elif "ixigua.com" in url_lower:
        referer = "https://www.ixigua.com/"

    headers = {
        "Referer": referer,
        "User-Agent": request.headers.get("User-Agent", ""),
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)

            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="图片获取失败")

            content_type = resp.headers.get("Content-Type", "image/jpeg")
            if not content_type.startswith("image/"):
                raise HTTPException(status_code=415, detail="unsupported image content type")

            return Response(
                content=resp.content,
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",  #  缓存一天
                    "Content-Type": content_type,
                }
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 一图流 / 配图（总结的可视化出口）────────────────────
# 两条路并存：
#  A. 本地 PIL 渲染（保底，零外部依赖、永不失效）—— /note/one_pager
#  B. 外部文生图（Miora 等）：我们只生成**提示词**并打开用户已登录的窗口，
#     由用户自己点生成，结果回收 —— /note/image_prompt
# 红线：B 路绝不代登录、绝不自动点击生成按钮，只做"提示词 + 结果回收"。


class OnePagerRequest(BaseModel):
    markdown: str
    title: Optional[str] = None
    source_label: Optional[str] = None
    cover_url: Optional[str] = None


@router.post("/note/one_pager")
def build_one_pager(data: OnePagerRequest):
    """把总结渲染成一图流 PNG（本地渲染，不联网）。返回 /static 可访问地址。"""
    try:
        from app.utils.one_pager import render_one_pager
        from app.utils.path_helper import get_runtime_path

        out_dir = os.path.join(get_runtime_path("static"), "one_pagers")
        os.makedirs(out_dir, exist_ok=True)
        name = f"onepager_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        path = os.path.join(out_dir, name)

        cover_path = None
        if data.cover_url:
            # 封面只做装饰：本地已有文件直接用，远程则跳过（避免为装饰阻塞）
            try:
                if data.cover_url.startswith("/static/"):
                    cand = os.path.join(get_runtime_path("static"),
                                        data.cover_url[len("/static/"):])
                    if os.path.exists(cand):
                        cover_path = cand
            except Exception:
                cover_path = None

        saved = render_one_pager(
            data.markdown or "",
            path,
            source_label=data.source_label or "",
            cover_path=cover_path,
        )
        if not saved:
            return R.error(msg="内容不足，无法生成一图流", err="ONE_PAGER_EMPTY")
        return R.success(data={"url": f"/static/one_pagers/{name}", "path": saved})
    except Exception as e:
        logger.error(f"一图流生成失败: {e}", exc_info=True)
        return R.error(msg=f"一图流生成失败: {str(e)[:120]}", err="ONE_PAGER_FAILED")


class ImagePromptRequest(BaseModel):
    markdown: str
    style: Optional[str] = None


IMAGE_PROMPT_TEMPLATE = """你是配图提示词设计师。下面是依据视频内容生成的总结，请据此设计**文生图提示词**。

要求：
1. 只依据给定内容构思画面，**不要加入内容里没有的事实**
2. 输出 3 个提示词，各自一行，格式为：`1. <提示词>`
3. 每个提示词：中文，30-60 字，含主体 + 场景 + 风格 + 构图
4. 风格偏好：{style}
5. 不要输出解释、不要编号以外的内容

总结内容：
{content}
"""


@router.post("/note/image_prompt")
def build_image_prompt(data: ImagePromptRequest):
    """把总结转成文生图提示词（供用户在 Miora 等工具里手动生成配图）。

    为什么只生成提示词而不自动调文生图：Miora 无公开 API 且需登录态，
    自动操作会违反「真人行为」红线。这里只做**提示词**，用户自己在已登录
    的窗口里粘贴生成——与小红书 WebView 捕获同模式。
    """
    try:
        from app.services.provider import ProviderService
        from app.gpt.gpt_factory import GPTFactory
        from app.models.model_config import ModelConfig

        content = (data.markdown or "").strip()
        if len(content) < 20:
            return R.error(msg="内容过短，无法生成提示词", err="PROMPT_CONTENT_TOO_SHORT")

        # 挑一个**真正可用**的模型写提示词：必须有 api_key，否则 openai 客户端
        # 抛"API Key 未配置"。2026-10-11 实测踩坑：zhipu 有模型但没 key，按
        # enabled 顺序被优先选中而报错。这里按"有 key + 有模型"筛，并优先
        # workbuddy 内置免费池（免用户配 key）。
        from app.db.provider_dao import get_all_providers as _dao_providers
        from app.db import model_dao

        candidates = [
            p for p in _dao_providers()
            if p.enabled and (p.api_key or "").strip()
            and model_dao.get_models_by_provider(p.id)
        ]
        if not candidates:
            return R.error(msg="未配置可用的模型供应商（请先到「设置」填写 API Key）",
                           err="PROVIDER_MISSING")
        candidates.sort(key=lambda p: 0 if p.id == "workbuddy" else 1)

        prov = candidates[0]
        # get_models_by_provider 返回 dict 列表（实测：{'id':…, 'model_name':…}）
        model = model_dao.get_models_by_provider(prov.id)[0]
        model_name = model["model_name"] if isinstance(model, dict) else str(model)
        cfg = ModelConfig(
            api_key=prov.api_key,
            base_url=prov.base_url,
            model_name=model_name,
            provider=prov.type,
            name=prov.name,
        )
        gpt = GPTFactory().from_config(cfg)

        prompt = IMAGE_PROMPT_TEMPLATE.format(
            style=data.style or "简洁扁平插画、低饱和、留白充足",
            content=content[:3000],
        )
        # 走 gpt 的 _do_create 而不是直接 client.create：这样自动继承
        # "运行时任务关闭思考"的策略（写提示词不需要深度思考，开着会白耗预算）
        resp = gpt._do_create([{"role": "user", "content": prompt}])
        text = (resp.choices[0].message.content or "").strip()

        prompts = []
        for line in text.splitlines():
            m = re.match(r"^\s*\d+[.)、]\s*(.+)$", line.strip())
            if m:
                prompts.append(m.group(1).strip())
        if not prompts:
            prompts = [l.strip() for l in text.splitlines() if len(l.strip()) > 8][:3]

        return R.success(data={"prompts": prompts[:3], "raw": text[:500]})
    except Exception as e:
        logger.error(f"配图提示词生成失败: {e}", exc_info=True)
        return R.error(msg=f"提示词生成失败: {str(e)[:120]}", err="IMAGE_PROMPT_FAILED")
