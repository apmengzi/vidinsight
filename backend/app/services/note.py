import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple, Union, Any

from fastapi import HTTPException
from pydantic import HttpUrl
from dotenv import load_dotenv

from app.downloaders.base import Downloader
from app.downloaders.bilibili_downloader import BilibiliDownloader
from app.downloaders.douyin_downloader import DouyinDownloader
from app.downloaders.local_downloader import LocalDownloader
from app.downloaders.youtube_downloader import YoutubeDownloader
from app.db.video_task_dao import delete_task_by_video, insert_video_task
from app.enmus.exception import NoteErrorEnum, ProviderErrorEnum
from app.enmus.task_status_enums import TaskStatus
from app.exceptions.biz_exception import BizException
from app.enmus.note_enums import DownloadQuality
from app.exceptions.note import NoteError
from app.exceptions.provider import ProviderError
from app.gpt.base import GPT
from app.gpt.gpt_factory import GPTFactory
from app.models.audio_model import AudioDownloadResult
from app.models.gpt_model import GPTSource
from app.models.model_config import ModelConfig
from app.models.notes_model import AudioDownloadResult, NoteResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.constant import SUPPORT_PLATFORM_MAP
from app.services.task_cancel import TaskCancelled, check_cancelled
from app.services.provider import ProviderService
from app.transcriber.base import Transcriber
from app.transcriber.transcriber_provider import get_transcriber, _transcribers
from app.utils.note_helper import replace_content_markers, prepend_source_link
from app.utils.path_helper import get_runtime_path
from app.utils.progress import clamp_pct
from app.utils.screenshot_marker import extract_screenshot_timestamps
from app.utils.status_code import StatusCode
from app.utils.video_helper import generate_screenshot
from app.utils.video_reader import VideoReader

# ------------------ 环境变量与全局配置 ------------------

# 从 .env 文件中加载环境变量
load_dotenv()

# 后端 API 地址与端口（若有需要可以在代码其他部分使用 BACKEND_BASE_URL）
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost")
BACKEND_PORT = os.getenv("BACKEND_PORT", "8483")
BACKEND_BASE_URL = f"{API_BASE_URL}:{BACKEND_PORT}"

# 输出目录（用于缓存音频、转写、Markdown 文件，以及存储截图）
NOTE_OUTPUT_DIR = Path(os.getenv("NOTE_OUTPUT_DIR", get_runtime_path("note_results")))
NOTE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_OUTPUT_DIR = os.getenv("OUT_DIR", get_runtime_path("static", "screenshots"))
# 图片基础 URL（用于生成 Markdown 中的图片链接，需前端静态目录对应）
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "/static/screenshots")

# 日志配置
from app.utils.logger import get_logger
logger = get_logger(__name__)

# hellotik 兜底直链暂存：{task_id: {"url","title","cover"}}。
# 走这里而不是加函数参数，是为了不动 generate() 那串已经很长的调用链；
# 直链带签名有时效，任务取用即清。
_DIRECT_URL_STORE: dict = {}

# 连接类失败的判定词：本机反代隧道断了、网关 5xx、DNS 解析不了。
# 必须同时认中英文——Windows 报的是「由于目标计算机积极拒绝，无法连接」，
# 不含 "refused" 这个词（2026-11-03 实测踩过）。
_CONN_ERR_MARKERS = (
    "refused", "connection", "connect", "unreachable", "timed out", "timeout",
    "name or service not known", "getaddrinfo", "max retries",
    "502", "503", "504", "bad gateway", "gateway time-out",
    "拒绝", "无法连接", "连接失败", "连接被", "超时", "网络",
)


def _is_connection_failure(exc: BaseException) -> bool:
    """判断异常是不是「供应商连不上」这类可换备用通道重试的失败。

    只认连接/网关类，不认 4xx 参数错误（换模型也没用，还会浪费一次调用）。
    """
    text = f"{type(exc).__name__} {exc}".lower()
    # 认证/参数类错误优先排除，避免被 "connection" 之类的宽泛词误伤
    for deny in ("401", "403", "invalid api key", "authentication", "unauthorized",
                 "invalid_request", "model not found"):
        if deny in text:
            return False
    return any(m in text for m in _CONN_ERR_MARKERS)


def put_direct_url(task_id: Optional[str], url: Optional[str],
                   title: Optional[str] = None, cover: Optional[str] = None) -> None:
    if not task_id or not url:
        return
    _DIRECT_URL_STORE[task_id] = {"url": url, "title": title, "cover": cover}
    if len(_DIRECT_URL_STORE) > 200:
        _DIRECT_URL_STORE.clear()


def take_direct_url(task_id: Optional[str]) -> dict | None:
    if not task_id:
        return None
    return _DIRECT_URL_STORE.pop(task_id, None)


class NoteGenerator:
    """
    NoteGenerator 用于执行视频/音频下载、转写、GPT 生成笔记、插入截图/链接、
    以及将任务信息写入状态文件与数据库等功能。
    """

    def __init__(self):
        from app.services.transcriber_config_manager import TranscriberConfigManager
        config_manager = TranscriberConfigManager()
        self.model_size: str = config_manager.get_whisper_model_size()
        self.device: Optional[str] = None
        self.transcriber_type: str = config_manager.get_transcriber_type()
        # 2026-10-20 惰性加载：模型加载属于【转写环节】而非提交环节——此前构造
        # 即加载（generate_note handler 里 NoteGenerator()._update_status 也会
        # 触发），首单提交要等 20s+。改为首次转写时才加载，耗时自然落在
        # TRANSCRIBING 阶段的进度里；启动预热线程会尽量抢在首次转写前完成。
        self._transcriber_instance: Optional[Transcriber] = None
        self.video_path: Optional[Path] = None
        self.video_img_urls=[]
        logger.info("NoteGenerator 初始化完成")


    # ---------------- 公有方法 ----------------

    def generate(
        self,
        video_url: Union[str, HttpUrl],
        platform: str,
        quality: DownloadQuality = DownloadQuality.medium,
        task_id: Optional[str] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        link: bool = False,
        screenshot: bool = False,
        _format: Optional[List[str]] = None,
        style: Optional[str] = None,
        extras: Optional[str] = None,
        output_path: Optional[str] = None,
        video_understanding: bool = False,
        keyframe_extract: bool = False,
        archive_video: bool = False,
        video_interval: int = 0,
        grid_size: Optional[List[int]] = None,
    ) -> NoteResult | None:
        """
        主流程：按步骤依次下载、转写、GPT 总结、截图/链接处理、存库、返回 NoteResult。

        :param video_url: 视频或音频链接
        :param platform: 平台名称，对应 SUPPORT_PLATFORM_MAP 中的键
        :param quality: 下载音频的质量枚举
        :param task_id: 用于标识本次任务的唯一 ID，亦用于状态文件和缓存文件命名
        :param model_name: GPT 模型名称
        :param provider_id: 模型供应商 ID
        :param link: 是否在笔记中插入视频片段链接
        :param screenshot: 是否在笔记中替换 Screenshot 标记为图片
        :param _format: 包含 'link' 或 'screenshot' 等字符串的列表，决定后续处理
        :param style: GPT 生成笔记的风格
        :param extras: 额外参数，传递给 GPT
        :param output_path: 下载输出目录（可选）
        :param video_understanding: 是否需要视频拼图理解（生成缩略图）
        :param video_interval: 视频帧截取间隔（秒），仅在 video_understanding 为 True 时生效
        :param grid_size: 生成缩略图时的网格大小，如 [3, 3]
        :return: NoteResult 对象，包含 markdown 文本、转写结果和音频元信息
        """
        if grid_size is None:
            grid_size = []

        try:
            logger.info(f"开始生成笔记 (task_id={task_id})")
            self._update_status(task_id, TaskStatus.PARSING)

            # 获取下载器与 GPT 实例

            downloader = self._get_downloader(platform)
            gpt = self._get_gpt(model_name, provider_id)

            # 挂上 hellotik 兜底直链（若前端解析过）：主通道失败时下载器自动接管
            _direct = take_direct_url(task_id)
            if _direct and hasattr(downloader, "download_direct"):
                downloader.direct_url = _direct.get("url")
                downloader.direct_title = _direct.get("title")
                downloader.direct_cover = _direct.get("cover")
                logger.info(f"已挂载 hellotik 兜底直链 (task_id={task_id})")

            # 缓存文件路径
            audio_cache_file = NOTE_OUTPUT_DIR / f"{task_id}_audio.json"
            transcript_cache_file = NOTE_OUTPUT_DIR / f"{task_id}_transcript.json"
            markdown_cache_file = NOTE_OUTPUT_DIR / f"{task_id}_markdown.md"
            # 1. 获取字幕/转写：优先缓存 → 平台字幕 → 音频转写
            transcript = None

            # 尝试读取缓存
            if transcript_cache_file.exists():
                logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
                try:
                    data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                    segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                    transcript = TranscriptResult(
                        language=data.get("language"),
                        full_text=data["full_text"],
                        segments=segments,
                    )
                    logger.info(f"已从缓存加载转写结果，共 {len(segments)} 段")
                except Exception as e:
                    logger.warning(f"加载转写缓存失败: {e}")

            # 缓存没有，尝试获取平台字幕
            if transcript is None:
                logger.info("尝试获取平台字幕（优先于音频下载）...")
                try:
                    transcript = downloader.download_subtitles(video_url)
                    if transcript and transcript.segments:
                        logger.info(f"成功获取平台字幕，共 {len(transcript.segments)} 段")
                        transcript_cache_file.write_text(
                            json.dumps(asdict(transcript), ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    else:
                        transcript = None
                        logger.info("平台无可用字幕，将下载音频后转写")
                except Exception as e:
                    logger.warning(f"获取平台字幕失败: {e}，将下载音频后转写")
                    transcript = None

            # 2. 下载音频/视频
            # 有字幕时只提取元信息，不下载音视频文件（除非需要截图/视频理解）
            has_transcript = transcript is not None
            need_full_download = (not has_transcript or screenshot or video_understanding
                                  or keyframe_extract or archive_video)
            audio_meta = self._download_media(
                downloader=downloader,
                video_url=video_url,
                quality=quality,
                audio_cache_file=audio_cache_file,
                status_phase=TaskStatus.DOWNLOADING,
                platform=platform,
                output_path=output_path,
                screenshot=screenshot,
                video_understanding=video_understanding,
                keyframe_extract=keyframe_extract,
                archive_video=archive_video,
                video_interval=video_interval,
                grid_size=grid_size,
                skip_download=not need_full_download,
            )

            # 3. 如果前面没拿到字幕，走转写流程
            if transcript is None:
                transcript = self._get_transcript(
                    downloader=downloader,
                    video_url=video_url,
                    audio_file=audio_meta.file_path,
                    transcript_cache_file=transcript_cache_file,
                    status_phase=TaskStatus.TRANSCRIBING,
                    task_id=task_id,
                )

            # 2.5 长正文前置注入：小红书等平台笔记常带长正文，
            # 与音频转写一并作为总结材料（图文笔记的正文已在字幕合成阶段处理）
            desc = ""
            raw_info = getattr(audio_meta, "raw_info", None) or {}
            desc = raw_info.get("description") or ""
            if transcript is not None and desc and len(desc.strip()) > 80:
                transcript.segments.insert(0, TranscriptSegment(
                    start=0.0, end=0.0,
                    text="【作者正文】\n" + desc.strip()[:2000]))
                transcript.full_text = (
                    "【作者正文】\n" + desc.strip()[:2000] + "\n" + transcript.full_text)
                logger.info(f"已将作者正文（{len(desc)} 字）注入总结材料")

            # 2.8 关键画面抽取（2026-10-31）：视觉模型选帧并提取结构化信息
            keyframe_md = ""
            logger.info(f"[keyframe] 开关={keyframe_extract} video_path={self.video_path} exists={self.video_path.exists() if self.video_path else None}")
            if keyframe_extract and self.video_path and self.video_path.exists():
                try:
                    from app.services.keyframe_service import (
                        extract_keyframes, analyze_keyframes,
                        get_bili_chapters, frames_to_markdown, dedup_analyzed,
                    )
                    self._update_status(task_id, TaskStatus.KEYFRAME,
                        message="正在抽取关键画面…", stage_progress=0)
                    # 视觉模型：优先用用户当前选的模型（需标记支持读图）。
                    # 之前调 gpt._get_provider_info() 是空实现，一直静默走
                    # VISION_* 环境变量默认值——用户换模型也不生效。
                    # 先判能力再抽帧：没视觉能力就直接跳过，不白跑十几秒抽帧。
                    provider_info, model_nm = self._resolve_vision_model(
                        provider_id, model_name, gpt)
                    if not provider_info:
                        logger.warning(
                            f"[keyframe] 模型 {model_name} 未标记视觉能力，跳过画面分析"
                            f"（可在设置页勾选「支持读图」）")
                        raise RuntimeError("__no_vision__")
                    # 帧目录必须落在静态托管目录下，否则前端拿不到图（破图）。
                    # 与截图同样走 /static 前缀 → /static/frames_<task>
                    # 按 task_id 隔离：不同任务共用同一目录会互相覆盖，
                    # 导致笔记里引用的图片张冠李戴（实测 u_0007 被后来的任务覆盖）
                    _frames_root = Path(IMAGE_OUTPUT_DIR).parent
                    frames_dir = str(_frames_root / f"frames_{task_id[:8]}")
                    # 章节先取（零成本 API）：按章节时长分配名额、章内均匀抽帧，
                    # 章节中段不再是盲区（实测只抽章节起点漏掉 8 帧全在中段）
                    chapters = get_bili_chapters(video_url) if "bilibili.com" in video_url else []
                    candidate = extract_keyframes(
                        str(self.video_path), frames_dir,
                        chapters=chapters)
                    # 转录上下文给视觉模型做判断参考
                    ctx_hint = transcript.full_text[:1500] if transcript else ""

                    # 逐帧进度上报（前端进度条「画面解析」那一步的百分比）
                    def _kf_progress(done_n: int, total_n: int) -> None:
                        pct = int(done_n * 100 / max(total_n, 1))
                        self._update_status(
                            task_id, TaskStatus.KEYFRAME,
                            message=f"画面解析中 {done_n}/{total_n} 帧",
                            stage_progress=pct,
                        )

                    analyzed = analyze_keyframes(
                        candidate, provider_info, model_nm, ctx_hint,
                        on_progress=_kf_progress)
                    analyzed = dedup_analyzed(analyzed)
                    keyframe_md = frames_to_markdown(
                        analyzed, url_prefix=f"/static/frames_{task_id[:8]}")
                    if chapters:
                        ch_lines = "\n".join(
                            f"- {c['title']} ({int(c['from_s']//60):02d}:{int(c['from_s']%60):02d})"
                            for c in chapters)
                        keyframe_md = f"\n## 视频章节\n\n{ch_lines}\n" + keyframe_md
                    if keyframe_md:
                        logger.info(f"关键帧抽取完成: {len(analyzed)} 帧留存, {len(chapters)} 章节")
                except Exception as kf_exc:
                    logger.warning(f"关键帧抽取失败（不影响主流程）: {kf_exc}")

            # 2.9 视频归档（2026-11-03）：把下载的视频留到笔记目录，
            # 供用户在笔记页直接回看（离线也能看）。必须在关键帧之后——
            # 关键帧还要读视频文件。归档后原文件由 cleanup 事件删除。
            if self.video_path and Path(self.video_path).exists():
                try:
                    from events.handlers import archive_video
                    archived = archive_video(str(self.video_path), task_id)
                    if archived:
                        logger.info(f"视频已归档，可在笔记页直接播放: {archived}")
                except Exception as _e:
                    logger.warning(f"视频归档失败（不影响主流程）: {_e}")

            # 3. GPT 总结
            markdown = self._summarize_text(
                audio_meta=audio_meta,
                transcript=transcript,
                gpt=gpt,
                markdown_cache_file=markdown_cache_file,
                link=link,
                screenshot=screenshot,
                formats=_format or [],
                style=style,
                extras=extras,
                video_img_urls=self.video_img_urls,
                task_id=task_id,
            )

            # 关键帧图文追加到正文末尾
            if keyframe_md:
                markdown = markdown + "\n" + keyframe_md

            # 4. 截图 & 链接替换
            if _format:
                markdown = self._post_process_markdown(
                    markdown=markdown,
                    video_path=self.video_path,
                    formats=_format,
                    audio_meta=audio_meta,
                    platform=platform,
                )

            markdown = prepend_source_link(markdown, str(video_url))

            # 5. 保存记录到数据库
            self._update_status(task_id, TaskStatus.SAVING)
            self._save_metadata(video_id=audio_meta.video_id, platform=platform, task_id=task_id)

            # 6. 完成
            self._update_status(task_id, TaskStatus.SUCCESS, stage_progress=100.0)
            logger.info(f"笔记生成成功 (task_id={task_id})")
            return NoteResult(markdown=markdown, transcript=transcript, audio_meta=audio_meta)

        except TaskCancelled:
            logger.info(f"任务已被用户取消 (task_id={task_id})")
            try:
                self._update_status(task_id, TaskStatus.CANCELLED, message="用户已取消")
            except TaskCancelled:
                pass
            return None
        except Exception as exc:
            logger.error(f"生成笔记流程异常 (task_id={task_id})：{exc}", exc_info=True)
            self._update_status(task_id, TaskStatus.FAILED, message=str(exc))
            return None

    @staticmethod
    def delete_note(video_id: str, platform: str) -> int:
        """
        删除数据库中对应 video_id 与 platform 的任务记录

        :param video_id: 视频 ID
        :param platform: 平台标识
        :return: 删除的记录数
        """
        logger.info(f"删除笔记记录 (video_id={video_id}, platform={platform})")
        return delete_task_by_video(video_id, platform)

    @staticmethod
    def delete_note_files(task_id: str) -> int:
        """
        删除单条任务对应的本地结果缓存文件，避免后续重建知识库时把已删除笔记重新索引回来。
        """
        deleted_count = 0
        output_root = NOTE_OUTPUT_DIR.resolve()

        for path in NOTE_OUTPUT_DIR.glob(f"{task_id}*"):
            try:
                resolved_path = path.resolve()
                if output_root not in resolved_path.parents and resolved_path != output_root:
                    logger.warning(f"跳过异常缓存路径: {resolved_path}")
                    continue
                if path.is_file():
                    path.unlink()
                    deleted_count += 1
            except Exception as exc:
                logger.warning(f"删除笔记缓存文件失败 (task_id={task_id}, path={path}): {exc}")

        return deleted_count

    # ---------------- 私有方法 ----------------

    @property
    def transcriber(self) -> Transcriber:
        """首次访问才创建转写器（模型加载算转写环节的等待，见 __init__ 注释）。"""
        if self._transcriber_instance is None:
            self._transcriber_instance = self._init_transcriber()
        return self._transcriber_instance

    def _init_transcriber(self) -> Transcriber:
        """
        根据环境变量 TRANSCRIBER_TYPE 动态获取并实例化转写器
        """
        if self.transcriber_type not in _transcribers:
            logger.error(f"未找到支持的转写器：{self.transcriber_type}")
            raise Exception(f"不支持的转写器：{self.transcriber_type}")

        logger.info(f"使用转写器：{self.transcriber_type}")
        return get_transcriber(
            transcriber_type=self.transcriber_type,
            model_size=self.model_size,
            device=os.getenv("WHISPER_DEVICE", "cuda"),
        )

    def _get_gpt(self, model_name: Optional[str], provider_id: Optional[str]) -> GPT:
        """
        根据 provider_id 获取对应的 GPT 实例
        :param model_name: GPT 模型名称
        :param provider_id: 供应商 ID
        :return: GPT 实例
        """
        provider = ProviderService.get_provider_by_id(provider_id)
        if not provider:
            logger.error(f"[get_gpt] 未找到模型供应商: provider_id={provider_id}")
            raise ProviderError(code=ProviderErrorEnum.NOT_FOUND,message=ProviderErrorEnum.NOT_FOUND.message)
        logger.info(f"创建 GPT 实例 {provider_id}")
        config = ModelConfig(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            model_name=model_name,
            provider=provider["type"],
            name=provider["name"],
        )
        return GPTFactory().from_config(config)

    def _get_downloader(self, platform: str) -> Downloader:
        """
        根据平台名称获取对应的下载器实例

        :param platform: 平台标识，需在 SUPPORT_PLATFORM_MAP 中
        :return: 对应的 Downloader 子类实例
        """
        downloader_cls = SUPPORT_PLATFORM_MAP.get(platform)
        logger.debug(f"实例化下载器 -  {platform}")
        instance = None
        if not downloader_cls:
            logger.error(f"不支持的平台：{platform}")
            raise NoteError(code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                            message=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.message)
        try:
            instance = downloader_cls
        except Exception as e:
            logger.error(f"实例化下载器失败：{e}")


        logger.info(f"使用下载器：{downloader_cls.__class__}")
        return instance

    def _update_status(self, task_id: Optional[str], status: Union[str, TaskStatus], message: Optional[str] = None,
                       progress: Optional[int] = None, stage_progress: Optional[float] = None):
        # 阶段边界取消检查：任务被用户取消后，任何状态推进都会在这里被打断
        # （单次 LLM 调用进行中无法中断，只能在阶段/轮次边界响应——延迟量级为单轮 LLM 耗时）
        # 例外：写 CANCELLED 终态本身要放行，否则状态文件永远标不上
        if status != TaskStatus.CANCELLED:
            check_cancelled(task_id)
        """
        创建或更新 {task_id}.status.json，记录当前任务状态

        :param task_id: 任务唯一 ID
        :param status: TaskStatus 枚举或自定义状态字符串
        :param message: 可选消息，用于记录失败原因等
        :param progress: 全局进度 0-100（兼容保留）
        :param stage_progress: 当前阶段内进度 0-100；None 表示该阶段不可量化。
            阶段切换时自动归 null，同一阶段内未传则沿用上一次的值。
        """
        if not task_id:
            return

        NOTE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        status_file = NOTE_OUTPUT_DIR / f"{task_id}.status.json"
        print(f"写入状态文件: {status_file} 当前状态: {status}")
        new_status = status.value if isinstance(status, TaskStatus) else status

        # ── 状态可见性计时（2026-10-18 EVT Must Fix）──
        # 用户实测痛点：长任务里"不知道还要多久"。写任务起止时间、
        # 阶段进入时间、已完成阶段耗时累计，供 task_status 透传给前端
        # 显示"已用 Xs / 约还需 Ys / 完成后的阶段耗时明细"。
        now = time.time()
        prev: dict = {}
        if status_file.exists():
            try:
                prev = json.loads(status_file.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
        # PARSING 是每次生成管线的第一个状态：重试任务复用同一 task_id，
        # 不重置的话 started_at 会继承上次失败的值，elapsed_s 把两次运行加
        # 在一起（审查抓到的真 bug）。每轮从 PARSING 开始都归零重计。
        prev_status = prev.get("status") or ""
        prev_stage_progress = prev.get("stage_progress")
        if new_status == TaskStatus.PARSING.value:
            started_at = now
            stage_entered_at = now
            stage_times = {}
            prev_status = ""  # 重试开局：不把上一轮终态→PARSING 的间隙累计进来
        else:
            started_at = float(prev.get("started_at") or now)
            stage_entered_at = float(prev.get("_stage_entered_at") or now)
            stage_times = prev.get("stage_times") or {}
        stage_changed = bool(prev_status and prev_status != new_status)
        if stage_changed:
            # 上一阶段实际耗时（截断防脏数据），累计进 stage_times
            stage_times[prev_status] = round(stage_times.get(prev_status, 0.0) + max(0.0, now - stage_entered_at), 1)
            stage_entered_at = now

        # 阶段内百分比语义（前端据此决定显示进度条还是隐藏）：
        # - 显式传了值 → 用它
        # - 进入新的中间阶段且没传 → null（该阶段还不可量化，绝不写 0 假装开始）
        # - 终态（SUCCESS/FAILED/CANCELLED）没传 → 保留结束前最后已知值
        # - 同一阶段内没传 → 沿用上一次的值
        terminal = new_status in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value,
                                  TaskStatus.CANCELLED.value)
        if stage_progress is not None:
            current_stage_progress = clamp_pct(stage_progress)
        elif terminal:
            current_stage_progress = clamp_pct(prev_stage_progress)
        elif stage_changed or new_status == TaskStatus.PARSING.value:
            current_stage_progress = None
        else:
            current_stage_progress = clamp_pct(prev_stage_progress)

        data = {"status": new_status, "started_at": started_at, "elapsed_s": round(now - started_at, 1)}
        if stage_times:
            data["stage_times"] = stage_times
        # 阶段内进度：不可得时显式写 null（前端据此隐藏进度条而不是显示 0%）
        data["stage_progress"] = current_stage_progress
        # 终态不再覆盖 _stage_entered_at（保留最后阶段耗时统计入口）
        if new_status not in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
            data["_stage_entered_at"] = stage_entered_at
        if message:
            data["message"] = message
        if progress is not None:
            data["progress"] = max(0, min(100, int(progress)))

        try:
            # First create a temporary file
            temp_file = status_file.with_suffix('.tmp')

            # Write to temporary file
            with temp_file.open('w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            # Atomic rename operation
            temp_file.replace(status_file)

            print(f"状态文件写入成功: {status_file}")
        except Exception as e:
            logger.error(f"写入状态文件失败 (task_id={task_id})：{e}")
            # Try to write error to file directly as fallback
            try:
                with status_file.open('w', encoding='utf-8') as f:
                    f.write(f"Error writing status: {str(e)}")
            except:
                logger.error(f"写入错误  {e}")

    def _handle_exception(self, task_id, exc):
        logger.error(f"任务异常 (task_id={task_id})", exc_info=True)
        error_message = getattr(exc, 'detail', str(exc))
        if isinstance(error_message, dict):
            try:
                error_message = json.dumps(error_message, ensure_ascii=False)
            except:
                error_message = str(error_message)
        self._update_status(task_id, TaskStatus.FAILED, message=error_message)

    def _download_media(
        self,
        downloader: Downloader,
        video_url: Union[str, HttpUrl],
        quality: DownloadQuality,
        audio_cache_file: Path,
        status_phase: TaskStatus,
        platform: str,
        output_path: Optional[str],
        screenshot: bool,
        video_understanding: bool,
        video_interval: int,
        grid_size: List[int],
        skip_download: bool = False,
        keyframe_extract: bool = False,
        archive_video: bool = False,
    ) -> AudioDownloadResult | None:
        """
        1. 检查音频缓存；若不存在，则根据需要下载音频或视频（若需截图/可视化）。
        2. 如果需要视频，则先下载视频并生成缩略图集，再下载音频。
        3. 返回 AudioDownloadResult

        :param downloader: Downloader 实例
        :param video_url: 视频/音频链接
        :param quality: 音频下载质量
        :param audio_cache_file: 本地缓存 JSON 文件路径
        :param status_phase: 对应的状态枚举，如 TaskStatus.DOWNLOADING
        :param platform: 平台标识
        :param output_path: 下载输出目录（可为 None）
        :param screenshot: 是否需要在笔记中插入截图
        :param video_understanding: 是否需要生成缩略图
        :param video_interval: 视频截帧间隔
        :param grid_size: 缩略图网格尺寸
        :return: AudioDownloadResult 对象
        """
        task_id = audio_cache_file.stem.split("_")[0]
        self._update_status(task_id, status_phase)

        # 阶段内下载进度：下载器只吐 0-100 百分比，写盘在这里做。
        # 节流由 ProgressThrottle 保证 ≤1 次/秒（yt-dlp 回调可达数十次/秒，
        # 直接写状态文件会成 IO 瓶颈）。
        def _report_download_progress(pct: float) -> None:
            self._update_status(task_id, status_phase, stage_progress=pct)

        @contextmanager
        def _download_scope():
            scope = getattr(downloader, "progress_scope", None)
            if scope is None:
                yield
                return
            with scope(_report_download_progress):
                yield

        # 已有缓存，尝试加载
        # 注意：需要视频（截图/缩略图/关键帧）时不能走缓存短路——
        # 缓存只记音频路径，video_path 是 None，抽帧会全部跳过。
        _needs_video = screenshot or video_understanding or keyframe_extract
        if audio_cache_file.exists() and not _needs_video:
            logger.info(f"检测到音频缓存 ({audio_cache_file})，直接读取")
            try:
                data = json.loads(audio_cache_file.read_text(encoding="utf-8"))
                return AudioDownloadResult(**data)
            except Exception as e:
                logger.warning(f"读取音频缓存失败，将重新下载：{e}")
        elif audio_cache_file.exists() and _needs_video:
            logger.info("检测到音频缓存，但本次需要视频（截图/缩略图/关键帧），重新下载")

        # 有字幕且不需要截图/视频理解时，只提取元信息不下载文件
        if skip_download:
            logger.info("已有字幕，仅提取视频元信息（不下载音视频）")
            try:
                audio = downloader.download(
                    video_url=video_url,
                    quality=quality,
                    output_dir=output_path,
                    need_video=False,
                    skip_download=True,
                )
                audio_cache_file.write_text(
                    json.dumps(asdict(audio), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(f"元信息提取完成 ({audio_cache_file})")
                return audio
            except Exception as exc:
                logger.warning(f"元信息提取失败，将尝试完整下载: {exc}")

        # 判断是否需要下载视频
        need_video = screenshot or video_understanding or keyframe_extract or archive_video
        if screenshot and not grid_size:
            grid_size = [2, 2]

        frame_interval = video_interval if video_interval and video_interval > 0 else 6
        if need_video:
            try:
                logger.info("开始下载视频")
                video_path_str = None
                try:
                    with _download_scope():
                        video_path_str = downloader.download_video(video_url)
                except Exception as dl_exc:
                    # B站 412 风控会把 yt-dlp 拦死 → 直接走 hellotik 直链
                    # （playwright 驱动 hellotik.app，真人操作拿 CDN 直链）
                    if "bilibili.com" in str(video_url) or "b23.tv" in str(video_url):
                        logger.warning(f"yt-dlp 下载视频失败，改用 hellotik 直链：{str(dl_exc)[:100]}")
                        from app.services.hellotik_local import parse_direct_url
                        hk = parse_direct_url(str(video_url))
                        if not hk.get("success"):
                            raise Exception(f"hellotik 解析失败: {hk.get('reason')}") from dl_exc
                        direct = hk["video_url"]
                        # 用 download_direct 落盘（它保留 mp4 并抽 mp3）
                        import hashlib
                        from app.utils.url_parser import extract_video_id
                        vid = extract_video_id(str(video_url), "bilibili") or hashlib.md5(
                            direct.encode()).hexdigest()[:12]
                        os.makedirs(output_path or ".", exist_ok=True)
                        vpath = os.path.join(output_path or ".", f"{vid}.mp4")
                        if not os.path.exists(vpath):
                            import requests as _rq
                            with _rq.get(direct, headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                                "Referer": "https://www.bilibili.com",
                            }, stream=True, timeout=120) as _r:
                                _r.raise_for_status()
                                with open(vpath, "wb") as _f:
                                    for _c in _r.iter_content(1024 * 256):
                                        if _c:
                                            _f.write(_c)
                        video_path_str = vpath
                        logger.info(f"hellotik 直链下载成功：{vpath}")
                # 非 B 站（小红书/抖音等）下载失败时 video_path_str 仍是 None，
                # 直接 Path(None) 会抛 "cannot access local variable" 这种
                # 毫无信息量的错误（实测小红书链接触发）。这里给明确原因。
                if not video_path_str:
                    raise RuntimeError(
                        "视频下载失败：当前平台没有可用的视频源（关键帧抽取需要视频文件）。"
                        "请检查链接是否可访问，或关闭「关键画面抽取」后重试。"
                    )
                self.video_path = Path(video_path_str)
                logger.info(f"视频下载完成：{self.video_path}")

                if grid_size:
                    self.video_img_urls = VideoReader(
                        video_path=str(self.video_path),
                        grid_size=tuple(grid_size),
                        frame_interval=frame_interval,
                        unit_width=960,
                        unit_height=540,
                        save_quality=80,
                    ).run()
                else:
                    logger.info("未指定 grid_size，跳过缩略图生成")
            except Exception as exc:
                if isinstance(exc, TaskCancelled):
                    # 下载进度回调里的取消信号不能被当成"下载失败"去做直链兜底
                    raise
                logger.error(f"视频下载失败：{exc}")
                self._handle_exception(task_id, exc)
                raise

        # 下载音频
        try:
            logger.info("开始下载音频")
            # 短路：关键帧/截图流程已经把完整视频下到盘上（含音轨），
            # 这时不必再调平台 API 取音频——小红书 xsec_token 过期时
            # 视频能靠 hellotik 拿到，但音频 API 会 400101 直接炸掉整个任务。
            # 从已下载的 mp4 直接抽 mp3，既快又绕开 token 依赖。
            _vp = getattr(self, "video_path", None)
            if _vp and Path(_vp).exists() and Path(_vp).stat().st_size > 100_000:
                try:
                    mp3_path = str(Path(output_path or ".") / f"{Path(_vp).stem}.mp3")
                    if not (Path(mp3_path).exists() and Path(mp3_path).stat().st_size > 1000):
                        import subprocess as _sp
                        _sp.run(
                            ["ffmpeg", "-y", "-i", str(_vp), "-vn",
                             "-acodec", "libmp3lame", "-b:a", "64k", mp3_path],
                            check=True, capture_output=True, timeout=180,
                        )
                    _dur = 0.0
                    try:
                        _probe = _sp.run(
                            ["ffprobe", "-v", "quiet", "-print_format", "json",
                             "-show_format", str(_vp)],
                            capture_output=True, text=True, timeout=30,
                        )
                        import json as _json
                        _dur = float(_json.loads(_probe.stdout)["format"]["duration"])
                    except Exception:
                        pass
                    from app.models.audio_model import AudioDownloadResult as _ADR
                    audio = _ADR(
                        file_path=mp3_path,
                        title=getattr(self, "video_title", "") or Path(_vp).stem,
                        duration=_dur,
                        cover_url=getattr(self, "video_cover", "") or "",
                        platform=platform,
                        video_id=Path(_vp).stem,
                        raw_info={"engine": "local_video_extract"},
                        video_path=str(_vp),
                    )
                    audio_cache_file.write_text(
                        json.dumps(asdict(audio), ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info(f"从已下载视频提取音频成功（跳过平台 API）: {mp3_path}")
                    return audio
                except Exception as _e:
                    logger.warning(f"从本地视频提音频失败，回退平台 API: {_e}")
            with _download_scope():
                audio = downloader.download(
                    video_url=video_url,
                    quality=quality,
                    output_dir=output_path,
                    need_video=need_video,
                )
            audio_cache_file.write_text(json.dumps(asdict(audio), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"音频下载并缓存成功 ({audio_cache_file})")
            return audio
        except Exception as exc:
            if isinstance(exc, TaskCancelled):
                # 下载进度回调里的取消信号不能被当成"下载失败"去做直链兜底
                raise
            # 兜底：B 站 412 风控把 yt-dlp 拦成"视频不存在"时，若前端已用第三方
            # 网页解析到无水印直链，改走直链取片——这是风控下唯一还能拿到源片的路子。
            direct_url = getattr(downloader, "direct_url", None)
            if direct_url and hasattr(downloader, "download_direct"):
                logger.warning(f"主通道下载失败，改用 hellotik 直链兜底：{str(exc)[:120]}")
                audio = downloader.download_direct(
                    direct_url=direct_url,
                    video_url=video_url,
                    output_dir=output_path,
                    title=getattr(downloader, "direct_title", None),
                    cover_url=getattr(downloader, "direct_cover", None),
                )
                audio_cache_file.write_text(json.dumps(asdict(audio), ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info(f"直链兜底下载成功 ({audio_cache_file})")
                return audio
            logger.error(f"音频下载失败：{exc}")
            self._handle_exception(task_id, exc)
            raise


    def _get_transcript(
        self,
        downloader: Downloader,
        video_url: str,
        audio_file: str,
        transcript_cache_file: Path,
        status_phase: TaskStatus,
        task_id: Optional[str] = None,
    ) -> TranscriptResult | None:
        """
        优先获取平台字幕，没有则 fallback 到音频转写

        :param downloader: 下载器实例
        :param video_url: 视频链接
        :param audio_file: 音频文件路径（用于 fallback 转写）
        :param transcript_cache_file: 缓存文件路径
        :param status_phase: 状态枚举
        :param task_id: 任务 ID
        :return: TranscriptResult 对象
        """
        self._update_status(task_id, status_phase)

        # 已有缓存，直接返回
        if transcript_cache_file.exists():
            logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
            try:
                data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                return TranscriptResult(language=data.get("language"), full_text=data["full_text"], segments=segments)
            except Exception as e:
                logger.warning(f"加载转写缓存失败，将重新获取：{e}")

        # 1. 先尝试获取平台字幕
        logger.info("尝试获取平台字幕...")
        try:
            transcript = downloader.download_subtitles(video_url)
            if transcript and transcript.segments:
                logger.info(f"成功获取平台字幕，共 {len(transcript.segments)} 段")
                # 缓存结果
                transcript_cache_file.write_text(
                    json.dumps(asdict(transcript), ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                return transcript
            else:
                logger.info("平台无可用字幕，将使用音频转写")
        except Exception as e:
            logger.warning(f"获取平台字幕失败: {e}，将使用音频转写")

        # 2. Fallback 到音频转写
        return self._transcribe_audio(
            audio_file=audio_file,
            transcript_cache_file=transcript_cache_file,
            status_phase=status_phase,
        )

    def _transcribe_audio(
        self,
        audio_file: str,
        transcript_cache_file: Path,
        status_phase: TaskStatus,
    ) -> TranscriptResult | None:
        """
        1. 检查转写缓存；若存在则尝试加载，否则调用转写器生成并缓存。
        2. 返回 TranscriptResult 对象

        :param audio_file: 音频文件本地路径
        :param transcript_cache_file: 转写结果缓存路径
        :param status_phase: 对应的状态枚举，如 TaskStatus.TRANSCRIBING
        :return: TranscriptResult 对象
        """
        task_id = transcript_cache_file.stem.split("_")[0]
        self._update_status(task_id, status_phase)

        # 已有缓存，尝试加载
        if transcript_cache_file.exists():
            logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
            try:
                data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                return TranscriptResult(language=data["language"], full_text=data["full_text"], segments=segments)
            except Exception as e:
                logger.warning(f"加载转写缓存失败，将重新转写：{e}")

        # 调用转写器
        try:
            logger.info("开始转写音频")

            def _report_transcribe_progress(pct: float) -> None:
                # 转写进度回调：写进状态文件供前端进度卡展示（1 秒节流由转写器保证）。
                # stage_progress 是阶段内百分比（consumed/total 语义由 whisper.py 提供）；
                # progress 保持原样继续写，兼容旧前端。
                try:
                    stage_pct = clamp_pct(pct * 100)
                    self._update_status(
                        task_id, status_phase,
                        progress=stage_pct, stage_progress=stage_pct)
                except Exception:
                    pass  # 进度上报失败不影响转写主流程

            # 模型尚未加载（启动预热未完成用户就提交了）：先提示再触发加载，
            # 让"转写中"阶段的前 20s 等待有明确文案，不烧心
            if self._transcriber_instance is None:
                self._update_status(
                    task_id, status_phase,
                    message='正在加载转写模型（首次约 20 秒，仅此一次，之后即热）',
                    stage_progress=0.0)
                _ = self.transcriber  # 触发惰性加载
            # 仅 fast-whisper 支持进度回调；其他转写器（groq/bcut 等）回退原调用
            import inspect
            if "on_progress" in inspect.signature(self.transcriber.transcript).parameters:
                transcript = self.transcriber.transcript(file_path=audio_file, on_progress=_report_transcribe_progress)
            else:
                transcript = self.transcriber.transcript(file_path=audio_file)
            transcript_cache_file.write_text(json.dumps(asdict(transcript), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"转写并缓存成功 ({transcript_cache_file})")
            return transcript
        except Exception as exc:
            logger.error(f"音频转写失败：{exc}")
            self._handle_exception(task_id, exc)
            raise

    def _summarize_text(
        self,
        audio_meta: AudioDownloadResult,
        transcript: TranscriptResult,
        gpt: GPT,
        markdown_cache_file: Path,
        link: bool,
        screenshot: bool,
        formats: List[str],
        style: Optional[str],
        extras: Optional[str],
            video_img_urls: List[str],
        task_id: Optional[str] = None,
    ) -> str | None:
        """
        调用 GPT 对转写结果进行总结，生成 Markdown 文本并缓存。

        :param audio_meta: AudioDownloadResult 元信息
        :param transcript: TranscriptResult 转写结果
        :param gpt: GPT 实例
        :param markdown_cache_file: Markdown 缓存路径
        :param link: 是否在笔记中插入链接
        :param screenshot: 是否在笔记中生成截图占位
        :param formats: 包含 'link' 或 'screenshot' 的列表
        :param style: GPT 输出风格
        :param extras: GPT 额外参数
        :param task_id: 任务 ID。**必须显式传入**：缓存文件名是
            ``<task_id>_markdown.md``，直接取 ``stem`` 会得到
            ``<task_id>_markdown``，导致 LLM 阶段的计时与进度写进一个
            前端永远不读的孤立状态文件（历史 bug：SUCCESS 的 stage_times
            里始终缺 SUMMARIZING）。
        :return: 生成的 Markdown 字符串
        """
        if task_id is None:
            task_id = markdown_cache_file.stem
            if task_id.endswith("_markdown"):
                task_id = task_id[: -len("_markdown")]
        self._update_status(task_id, TaskStatus.SUMMARIZING)

        # LLM 分块进度：分块循环里按 i/N 上报（GPT 实现自己节流）
        def _report_llm_progress(pct: float) -> None:
            self._update_status(task_id, TaskStatus.SUMMARIZING, stage_progress=pct)

        source = GPTSource(
            title=audio_meta.title,
            segment=transcript.segments,
            tags=audio_meta.raw_info.get("tags", []),
            screenshot=screenshot,
            video_img_urls=video_img_urls,
            link=link,
            _format=formats,
            style=style,
            extras=extras,
            checkpoint_key=task_id,
            on_progress=_report_llm_progress,
        )

        try:
            markdown = gpt.summarize(source)
            markdown_cache_file.write_text(markdown, encoding="utf-8")
            logger.info(f"GPT 总结并缓存成功 ({markdown_cache_file})")
            return markdown
        except TaskCancelled:
            # 已取消：检查点已保存 partials，重试可断点续跑
            raise
        except Exception as exc:
            logger.error(f"GPT 总结失败：{exc}")
            # 供应商连接类失败（本机反代隧道断了/网关 5xx）时自动换备用供应商重试。
            # 网页版首模型常挂在「用户自己电脑上的反代」上，机器关机就会整任务失败；
            # 有备用通道时不该让用户看到失败。
            if _is_connection_failure(exc):
                fb = self._get_fallback_gpt(gpt)
                if fb is not None:
                    logger.warning(f"主模型连接失败，自动切换备用模型重试 (task={task_id[:8]})")
                    try:
                        markdown = fb.summarize(source)
                        markdown_cache_file.write_text(markdown, encoding="utf-8")
                        logger.info(f"备用模型总结成功 ({markdown_cache_file})")
                        return markdown
                    except TaskCancelled:
                        raise
                    except Exception as exc2:
                        logger.error(f"备用模型总结也失败：{exc2}")
                        self._handle_exception(task_id, exc2)
                        exc = exc2
            self._handle_exception(task_id, exc)
            # 供应商网关/超时类错误翻译成注册表错误码（否则 504 的整页 HTML 会透给用户）
            from app.utils.llm_error import classify_llm_error
            spec = classify_llm_error(exc)
            if spec:
                raise BizException(
                    spec.code, spec.message, err=spec.err,
                    detail={"provider_error": str(exc)[:200]}) from exc
            raise

    def _get_fallback_gpt(self, primary: GPT) -> Optional[GPT]:
        """选一个与主模型不同的可用供应商，用于连接失败时兜底。

        优先 Systalyze（云端，不依赖本机），其次任一已配 Key 的其它供应商。
        找不到就返回 None（调用方保持原错误）。
        """
        try:
            from app.services.provider import ProviderService
            providers = ProviderService.get_all_providers() or []
        except Exception as e:
            logger.warning(f"[fallback] 读取供应商列表失败: {e}")
            return None

        primary_key = (getattr(primary, "api_key", "") or "")[:12]
        primary_base = (getattr(primary, "base_url", "") or "").lower()

        usable = []
        for p in providers:
            if not p or p.get("enabled") == 0:
                continue
            key = (p.get("api_key") or "").strip()
            base = (p.get("base_url") or "").lower()
            if not key or not base:
                continue
            # 跳过主模型自己
            if base == primary_base and key[:12] == primary_key:
                continue
            usable.append(p)
        if not usable:
            return None

        # Systalyze 优先（云端直连，最稳）
        usable.sort(key=lambda p: 0 if "systalyze" in (p.get("base_url") or "").lower() else 1)

        for p in usable:
            try:
                # 该供应商下挑一个模型：优先 deepseek 系列
                from app.db.model_dao import get_models_by_provider
                models = get_models_by_provider(p["id"]) or []
                if not models:
                    continue
                names = [m.get("model_name") or "" for m in models]
                pick = next((n for n in names if "deepseek" in n.lower()), names[0])
                logger.info(f"[fallback] 使用备用供应商 {p.get('name')} / {pick}")
                return self._get_gpt(pick, str(p["id"]))
            except Exception as e:
                logger.warning(f"[fallback] 构建备用 GPT 失败 ({p.get('name')}): {e}")
                continue
        return None

    def _resolve_vision_model(self, provider_id: str, model_name: str, gpt):
        """选出可用来读图的模型，返回 (provider_info, model_name)。

        优先级：
        1. 用户当前选的模型，且能力标记里 image=true
        2. 该供应商下任一标记了 image=true 的模型
        3. 兜底：VISION_* 环境变量指向的反代（未配置能力标记时保持旧行为，
           避免老用户升级后关键帧功能直接不可用）

        返回 (None, "") 表示明确没有可用视觉模型。
        """
        try:
            from app.db.model_dao import get_model_by_provider_and_name, get_models_by_provider
            from app.db.provider_dao import get_provider_by_id
            prov = get_provider_by_id(provider_id) if provider_id else None
            if prov:
                # DAO 返回 ORM 对象，不是 dict
                info = {"base_url": getattr(prov, "base_url", "") or "",
                        "api_key": getattr(prov, "api_key", "") or ""}
                # 能力标记有两种格式：老版 {"image": true}、新版
                # {"inputFormat": {"supportsImage": true}}。之前只认老的，
                # 新版永远判 false → 一直走兜底 127.0.0.1（容器里不通）。
                def _can_read_image(caps) -> bool:
                    c = caps or {}
                    if c.get("image"):
                        return True
                    return bool((c.get("inputFormat") or {}).get("supportsImage"))

                rec = get_model_by_provider_and_name(provider_id, model_name) if model_name else None
                if rec and _can_read_image(rec.get("capabilities")):
                    return info, model_name
                # 该供应商下有没有别的视觉模型
                for m in get_models_by_provider(provider_id):
                    if _can_read_image(m.get("capabilities")):
                        return info, m["model_name"]
        except Exception as e:
            logger.warning(f"[keyframe] 查模型能力失败: {e}")

        # 兜底：环境变量配置的视觉反代（未打能力标记时的兼容路径）
        from app.services.keyframe_service import VISION_BASE_URL, VISION_API_KEY, VISION_MODEL
        if VISION_BASE_URL and VISION_MODEL:
            logger.info(f"[keyframe] 模型未标记视觉能力，回退到 {VISION_MODEL}")
            return {"base_url": VISION_BASE_URL, "api_key": VISION_API_KEY or ""}, VISION_MODEL
        return None, ""

    def _post_process_markdown(
        self,
        markdown: str,
        video_path: Optional[Path],
        formats: List[str],
        audio_meta: AudioDownloadResult,
        platform: str,
    ) -> str:
        """
        对生成的 Markdown 做后期处理：插入截图和/或插入链接。

        :param markdown: 原始 Markdown 字符串
        :param video_path: 本地视频路径（可为 None）
        :param formats: 包含 'link' 或 'screenshot' 的列表
        :param audio_meta: AudioDownloadResult 元信息，用于链接替换
        :param platform: 平台标识，用于链接替换
        :return: 处理后的 Markdown 字符串
        """
        if "screenshot" in formats and video_path:
            try:
                markdown = self._insert_screenshots(markdown, video_path)
            except Exception as exc:
                logger.warning("截图插入失败，跳过该步骤")

        if "link" in formats:
            try:
                markdown = replace_content_markers(markdown, video_id=audio_meta.video_id, platform=platform)
            except Exception as e:
                logger.warning(f"链接插入失败，跳过该步骤：{e}")

        return markdown

    def _insert_screenshots(self, markdown: str, video_path: Path) -> str | None | Any:
        """
        扫描 Markdown 文本中所有 Screenshot 标记，并替换为实际生成的截图链接。

        :param markdown: 含有 *Screenshot-mm:ss 或 Screenshot-[mm:ss] 标记的 Markdown 文本
        :param video_path: 本地视频文件路径
        :return: 替换后的 Markdown 字符串
        """
        matches: List[Tuple[str, int]] = extract_screenshot_timestamps(markdown)
        for idx, (marker, ts) in enumerate(matches):
            try:
                img_path = generate_screenshot(str(video_path), str(IMAGE_OUTPUT_DIR), ts, idx)
                filename = Path(img_path).name
                # 构建前端可访问的 URL，例如 /static/screenshots/{filename}
                img_url = f"{IMAGE_BASE_URL.rstrip('/')}/{filename}"
                markdown = markdown.replace(marker, f"![]({img_url})", 1)
            except Exception as exc:
                logger.error(f"生成截图失败 (timestamp={ts})：{exc}")
                # self._handle_exception(task_id, exc)
                return None
        return markdown

    @staticmethod
    def _extract_screenshot_timestamps(markdown: str) -> List[Tuple[str, int]]:
        """
        从 Markdown 文本中提取所有 '*Screenshot-mm:ss' 或 'Screenshot-[mm:ss]' 标记，
        返回 [(原始标记文本, 时间戳秒数), ...] 列表。

        :param markdown: 原始 Markdown 文本
        :return: 标记与对应时间戳秒数的列表
        """
        return extract_screenshot_timestamps(markdown)

    def _save_metadata(self, video_id: str, platform: str, task_id: str) -> None:
        """
        将生成的笔记任务记录插入数据库

        :param video_id: 视频 ID
        :param platform: 平台标识
        :param task_id: 任务 ID
        """
        try:
            insert_video_task(video_id=video_id, platform=platform, task_id=task_id)
            logger.info(f"已保存任务记录到数据库 (video_id={video_id}, platform={platform}, task_id={task_id})")
        except Exception as e:
            logger.error(f"保存任务记录失败：{e}")
