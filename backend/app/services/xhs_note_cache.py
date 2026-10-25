"""小红书笔记详情缓存（进程内，TTL）。

来源：桌面端隐藏 WebView 打开笔记页后，由页面自身 JS 完成签名取数，
把 __INITIAL_STATE__ 里的笔记节点 POST 到 /api/xhs/ingest_state 存进来。
download_subtitles / download 在抓详情前先查这里，命中即免 curl 抓取——
解决「无 xsec_token 的裸链接（合集展开产物）SSR 不含笔记数据」的问题。

TTL 6 小时：批量解析一组合集时，串行执行器可能要十几分钟才轮到最后一条
（下载/转写/LLM 都耗时），15 分钟的旧 TTL 会让队尾任务缓存过期、退回
curl 通道再撞死-cookie 墙。缓存只是内存元数据，重启即清，不会脏盘。
"""
import threading
import time

_TTL = 21600.0  # 6 小时


class XHSNoteCache:
    _data: dict[str, tuple[float, dict]] = {}
    _lock = threading.Lock()

    @classmethod
    def put(cls, note_id: str, note: dict) -> None:
        with cls._lock:
            cls._data[note_id] = (time.time(), note)

    @classmethod
    def get(cls, note_id: str) -> dict | None:
        with cls._lock:
            item = cls._data.get(note_id)
            if not item:
                return None
            ts, note = item
            if time.time() - ts > _TTL:
                cls._data.pop(note_id, None)
                return None
            return note

    @classmethod
    def size(cls) -> int:
        with cls._lock:
            now = time.time()
            expired = [k for k, (ts, _) in cls._data.items() if now - ts > _TTL]
            for k in expired:
                cls._data.pop(k, None)
            return len(cls._data)


class XHSTokenStore:
    """xsec_token 存储（桌面端 WebView 从合集/主页页面 DOM 收割）。

    背景：XHS 服务端渲染的合集 noteList 不带 xsec_token，只有登录态 SPA
    渲染出的卡片链接（href）才带。WebView 收割后存这里，expand_collection
    合并进逐条笔记 URL，后端 SSR 详情抓取（带 cookie+token）即可走通。
    """

    _TTL = 3600.0  # 1 小时，足够覆盖一次批量解析
    _data: dict[str, tuple[float, str]] = {}
    _lock = threading.Lock()

    @classmethod
    def put(cls, note_id: str, url_with_token: str) -> None:
        with cls._lock:
            cls._data[note_id] = (time.time(), url_with_token)

    @classmethod
    def get(cls, note_id: str) -> str | None:
        with cls._lock:
            item = cls._data.get(note_id)
            if not item:
                return None
            ts, url = item
            if time.time() - ts > cls._TTL:
                cls._data.pop(note_id, None)
                return None
            return url
