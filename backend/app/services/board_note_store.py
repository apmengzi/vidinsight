"""收藏专辑（board）笔记清单存储（磁盘持久化）。

来源：桌面端被动捕获窗口打开用户自己的收藏专辑页后，页面 JS 的 XHR/fetch
钩子 + DOM 卡片扫描顺路记录笔记条目（boardFeedsMap / 分页 API / 页面卡片），
POST 到 /api/xhs/ingest_board_notes 存进来。每条只存轻量元数据
（note_id / 标题 / 类型 / 封面 / 可选 xsec_token），不存正文——详情仍走
XHSNoteCache（用户点开过）或登录态 SSR。

2026-10-17 变更（用户要求"保持收藏专辑的数据"）：内存存储 → 磁盘持久化。
原实现"重启即清"，用户重启应用后辛苦捕获的 242 条全丢、又要重捕一遍。
现改为 JSON 文件持久化（get_runtime_path/board_notes.json），启动加载、
写入落盘。TTL 保留（默认 30 天，捕获元数据很轻，长一点没负担）。
"""
import json
import re
import threading
import time
import itertools
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

_TTL = 30 * 86400.0  # 30 天：捕获一次不容易，别轻易丢
_ID_RE = re.compile(r"[0-9a-f]{24}")


def _store_path() -> Path:
    from app.utils.path_helper import get_runtime_path
    p = Path(get_runtime_path("board_notes.json"))
    return p


class BoardNoteStore:
    # (捕获时间, 单调序号, note)：Windows time.time() 精度约 15ms，
    # 同 tick 内连续捕获的时间戳会相同，必须靠序号保证「后捕获在前」严格成立
    _seq = itertools.count()
    _data: dict[str, dict[str, tuple[float, int, dict]]] = {}
    _lock = threading.Lock()
    _loaded = False

    @classmethod
    def _load(cls) -> None:
        """启动时（首次访问时）从磁盘加载。失败不致命——退化为空库重新捕获。"""
        if cls._loaded:
            return
        cls._loaded = True
        try:
            path = _store_path()
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            # 文件格式：{board_id: [[ts, seq, note], ...]}
            for bid, rows in raw.items():
                if not _ID_RE.fullmatch(str(bid)) or not isinstance(rows, list):
                    continue
                board = cls._data.setdefault(str(bid), {})
                for row in rows:
                    try:
                        ts, seq, note = float(row[0]), int(row[1]), row[2]
                        nid = str(note.get("note_id") or "")
                        if _ID_RE.fullmatch(nid):
                            board[nid] = (ts, seq, note)
                    except Exception:
                        continue
            total = sum(len(b) for b in cls._data.values())
            if total:
                logger.info(f"专辑笔记库已从磁盘加载: {len(cls._data)} 个专辑 / {total} 条")
        except Exception as e:
            logger.warning(f"专辑笔记库加载失败（退化为空库）: {e}")

    @classmethod
    def _flush(cls) -> None:
        """落盘。调用方必须已持有 _lock。失败只告警——不能因写盘失败丢捕获。"""
        try:
            path = _store_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 只保留最近 5000 条/专辑，防无限膨胀
            out = {}
            for bid, board in cls._data.items():
                items = sorted(board.values(), key=lambda x: (x[0], x[1]), reverse=True)[:5000]
                out[bid] = [[ts, seq, note] for ts, seq, note in items]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"专辑笔记库落盘失败: {e}")

    @classmethod
    def put(cls, board_id: str, note: dict) -> None:
        """upsert 一条专辑笔记（note_id 冲突时新数据覆盖旧数据）并落盘。

        2026-10-19：空字段不覆盖旧值。被动捕获有两条上报路径（board DOM 扫描
        带封面 / 分页 API 扫描不带封面），后到的空 cover 会把先到的封面擦掉；
        cover_local 一旦落盘也不该被后续无本地路径的重捕获清空。
        """
        nid = str(note.get("note_id") or "")
        if not _ID_RE.fullmatch(nid) or not _ID_RE.fullmatch(str(board_id)):
            return
        with cls._lock:
            cls._load()
            board = cls._data.setdefault(str(board_id), {})
            prev = board.get(nid)
            if prev and isinstance(prev[2], dict):
                merged = dict(prev[2])
                for k, v in note.items():
                    if v in ("", None, [], {}) and merged.get(k):
                        continue
                    # 2026-10-20：骨架卡片（虚拟列表未渲染出标题）会被 ingest 成
                    # "未命名"——它是占位不是真值，不能反向覆盖已有的真标题；
                    # 真名后到时正常覆盖（重捕获自动补全）。
                    if k == "title" and str(v).strip() in ("未命名", "未命名笔记") and merged.get(k):
                        continue
                    merged[k] = v
                note = merged
            # 骨架卡（无标题且无封面）重扫不刷新时间戳/序号：它没有带来新信息，
            # 刷新只会把旧笔记顶到"最新收藏"位挤掉真正的新收藏
            skeleton = (not str(note.get("title") or "").strip()
                        or str(note.get("title")).strip() in ("未命名", "未命名笔记"))                        and not note.get("cover") and not note.get("cover_local")
            if prev and skeleton:
                ts_old, seq_old = prev[0], prev[1]
                board[nid] = (ts_old, seq_old, note)
            else:
                board[nid] = (time.time(), next(cls._seq), note)
            cls._flush()

    @classmethod
    def set_cover_local(cls, board_id: str, note_id: str, rel_path: str) -> None:
        """封面下载完成后回填本地路径（保持原有捕获时间/顺序不变）。"""
        cls.set_cover_locals(board_id, {note_id: rel_path})

    @classmethod
    def set_cover_locals(cls, board_id: str, mapping: dict[str, str]) -> None:
        """批量回填本地封面路径（整批只落盘一次——242 条全量时避免 242 次写盘）。"""
        if not _ID_RE.fullmatch(str(board_id)) or not mapping:
            return
        with cls._lock:
            cls._load()
            board = cls._data.get(str(board_id))
            if not board:
                return
            changed = False
            for note_id, rel_path in mapping.items():
                if not _ID_RE.fullmatch(str(note_id)) or not rel_path:
                    continue
                row = board.get(note_id)
                if not row:
                    continue
                ts, seq, note = row
                if note.get("cover_local") == rel_path:
                    continue
                updated = dict(note)
                updated["cover_local"] = rel_path
                board[note_id] = (ts, seq, updated)
                changed = True
            if changed:
                cls._flush()

    @classmethod
    def get(cls, board_id: str, note_id: str) -> dict | None:
        """取单条（供下载后回填/幂等判断用）。"""
        with cls._lock:
            cls._load()
            row = (cls._data.get(str(board_id)) or {}).get(str(note_id))
            return dict(row[2]) if row else None

    @classmethod
    def list_notes(cls, board_id: str) -> list[dict]:
        """按捕获先后返回专辑笔记列表（新捕获的在前=最新收藏在前，顺手清过期）。

        2026-10-17 修正：原来倒序（后捕获在前）导致显示的是**最旧的在前**——
        因为用户从上往下翻 = 从最新翻到最旧，最旧的最后被捕获。
        用户要求"从最新收藏的笔记显示"，故改为正序。

        2026-10-19：读取时校验 cover_local 是否真在盘上（用户清缓存/换机器后
        路径会悬空），悬空则抹掉，让前端回退到原始 URL。
        """
        from app.services.cover_persist import cover_file_exists
        with cls._lock:
            cls._load()
            board = cls._data.get(str(board_id))
            if not board:
                return []
            now = time.time()
            expired = [k for k, (ts, _, _) in board.items() if now - ts > _TTL]
            if expired:
                for k in expired:
                    board.pop(k, None)
                cls._flush()
            def _skel(n: dict) -> int:
                t = str(n.get("title") or "").strip()
                if t in ("", "未命名", "未命名笔记") and not n.get("cover") and not n.get("cover_local"):
                    return 1
                return 0
            # 未命名骨架排尾（等重捕获补全），其余按捕获时间正序（最新收藏在前）
            items = sorted(board.values(), key=lambda x: (_skel(x[2]), x[0], x[1]))
            out = []
            healed = False
            for _, _, note in items:
                rel = note.get("cover_local")
                if rel and not cover_file_exists(rel):
                    note = dict(note)
                    note.pop("cover_local", None)
                    healed = True
                out.append(note)
            if healed:
                # 只抹内存 + 落盘一次；不改变 ts/seq，顺序不变
                for k, (ts, seq, note) in list(board.items()):
                    rel = note.get("cover_local")
                    if rel and not cover_file_exists(rel):
                        n2 = dict(note)
                        n2.pop("cover_local", None)
                        board[k] = (ts, seq, n2)
                cls._flush()
            return out

    @classmethod
    def size(cls, board_id: str) -> int:
        return len(cls.list_notes(board_id))

    @classmethod
    def clear(cls, board_id: str) -> None:
        with cls._lock:
            cls._load()
            cls._data.pop(str(board_id), None)
            cls._flush()
