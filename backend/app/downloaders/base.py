import enum
import threading

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Callable, Optional, Union

from app.enmus.note_enums import DownloadQuality
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult
from app.utils.progress import ProgressThrottle, build_ytdlp_opts_with_progress
from os import getenv
QUALITY_MAP = {
    "fast": "32",
    "medium": "64",
    "slow": "128"
}


class Downloader(ABC):
    def __init__(self):
        #TODO 需要修改为可配置
        self.quality = QUALITY_MAP.get('fast')
        self.cache_data=getenv('DATA_DIR')
        # 下载器实例在 SUPPORT_PLATFORM_MAP 里是进程级单例，而任务并发跑在
        # 线程池里（TASK_MAX_WORKERS 默认 3）。进度回调必须按线程隔离，
        # 否则并发任务的百分比会串到别的 task 上。
        self._progress_local = threading.local()

    def _progress_state(self) -> threading.local:
        """惰性建线程局部状态：老子类若没调 super().__init__ 也不会炸。"""
        local = getattr(self, "_progress_local", None)
        if local is None:
            local = threading.local()
            self._progress_local = local
        return local

    @property
    def progress_reporter(self) -> Optional[Callable[[float], None]]:
        return getattr(self._progress_state(), "reporter", None)

    @property
    def progress_throttle(self) -> ProgressThrottle:
        local = self._progress_state()
        throttle = getattr(local, "throttle", None)
        if throttle is None:
            throttle = ProgressThrottle()
            local.throttle = throttle
        return throttle

    @contextmanager
    def progress_scope(self, reporter: Optional[Callable[[float], None]]):
        """在 with 块内为**当前线程**注入下载进度回调（0-100）。

        下载器不认识任务状态文件，也不该认识——它只负责把 yt-dlp 的
        downloaded/total 折成百分比交出去，写盘由调用方决定。
        """
        local = self._progress_state()
        prev = getattr(local, "reporter", None)
        local.reporter = reporter
        try:
            yield self
        finally:
            local.reporter = prev

    def attach_progress(self, ydl_opts: dict) -> dict:
        """给 yt-dlp opts 挂上当前线程的进度钩子；无回调时原样返回。"""
        return build_ytdlp_opts_with_progress(
            ydl_opts, self.progress_reporter, self.progress_throttle)



    @abstractmethod
    def download(self, video_url: str, output_dir: str = None,
                 quality: DownloadQuality = "fast", need_video: Optional[bool] = False,
                 skip_download: bool = False) -> AudioDownloadResult:
        '''

        :param need_video:
        :param video_url: 资源链接
        :param output_dir: 输出路径 默认根目录data
        :param quality: 音频质量 fast | medium | slow
        :return:返回一个 AudioDownloadResult 类
        '''
        pass

    @staticmethod
    def download_video(self, video_url: str,
                       output_dir: Union[str, None] = None) -> str:
        pass

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: list = None) -> Optional[TranscriptResult]:
        '''
        尝试获取平台字幕（人工字幕或自动生成字幕）

        :param video_url: 视频链接
        :param output_dir: 输出路径
        :param langs: 优先语言列表，如 ['zh-Hans', 'zh', 'en']
        :return: TranscriptResult 或 None（无字幕时）
        '''
        return None
