"""
全局统一知识库（Unified Knowledge Base）。

核心特性：
- 单一 ChromaDB Collection 存储所有视频笔记
- 语义分块（Recursive + Overlap）
- 混合检索（向量 + BM25 → RRF 融合）
- BGE-Reranker 精排
- 来源引用追踪
"""
import json
import os
import re
import time
from typing import Optional

import chromadb
from chromadb.config import Settings

from app.services.bm25_index import BM25Index
from app.utils.logger import get_logger
from app.utils.path_helper import get_runtime_path

logger = get_logger(__name__)

NOTE_OUTPUT_DIR = os.getenv("NOTE_OUTPUT_DIR", get_runtime_path("note_results"))
VECTOR_DB_DIR = os.getenv("VECTOR_DB_DIR", get_runtime_path("vector_db"))
KB_VECTOR_SEARCH_ENABLED = os.getenv("KB_VECTOR_SEARCH_ENABLED", "0").lower() in ("1", "true", "yes")

UNIFIED_COLLECTION = "vidinsight_unified"

# 分块配置（标题 → 段落 → 句子 → 字符 逐级降级）
CHUNK_SIZE = 250         # 目标块大小（字符），更细粒度提高检索精度
CHUNK_OVERLAP = 80       # 块间重叠，保证语义连贯
MIN_CHUNK_LEN = 15       # 最小块长度，过滤无效片段


# ═══════════════════════════════════════════════════
# 递归语义文本分割器
# ═══════════════════════════════════════════════════

class RecursiveTextSplitter:
    """递归语义文本分割器。

    按优先级降级分割：标题 → 段落 → 句子 → 字符。
    """

    # 分割符优先级（从语义到机械）
    SEPARATORS = [
        r'\n(?=#{2,3}\s)',          # Markdown 标题
        r'\n\n+',                    # 段落空白
        r'(?<=[。！？.!?\n])',       # 句子边界
        r'(?<=[，,；;：:\n])',       # 子句边界
    ]

    def split(
        self,
        text: str,
        chunk_size: int = CHUNK_SIZE,
        overlap: int = CHUNK_OVERLAP,
    ) -> list[str]:
        """递归分割文本为语义连贯的块。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = self._split_recursive(
            text.strip(), self.SEPARATORS, 0, chunk_size, overlap,
        )
        return [c for c in chunks if len(c.strip()) >= MIN_CHUNK_LEN]

    def _split_recursive(
        self, text: str, separators: list[str], sep_idx: int,
        chunk_size: int, overlap: int,
    ) -> list[str]:
        if sep_idx >= len(separators):
            return self._force_split(text, chunk_size, overlap)

        parts = re.split(separators[sep_idx], text)
        parts = [p.strip() for p in parts if p.strip()]

        if not parts:
            return []

        chunks = []
        for part in parts:
            if len(part) <= chunk_size:
                chunks.append(part)
            else:
                chunks.extend(
                    self._split_recursive(
                        part, separators, sep_idx + 1, chunk_size, overlap,
                    )
                )

        return self._merge_with_overlap(chunks, chunk_size, overlap)

    def _force_split(self, text: str, chunk_size: int, overlap: int) -> list[str]:
        """最终降级：强制定长分割。"""
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(text[start:end].strip())
            start += chunk_size - overlap
        return chunks

    def _merge_with_overlap(
        self, chunks: list[str], max_size: int, overlap: int,
    ) -> list[str]:
        """合并过短的 chunk，并在 chunk 间添加语义重叠。"""
        if not chunks:
            return []

        merged = []
        buffer = ""
        for chunk in chunks:
            if len(buffer) + len(chunk) <= max_size:
                buffer = f"{buffer}\n{chunk}".strip() if buffer else chunk
            else:
                if buffer.strip():
                    merged.append(buffer)
                buffer = chunk

        if buffer.strip():
            merged.append(buffer)

        # 添加重叠：每个 chunk 的后 overlap 字符作为下一个 chunk 的前缀
        if overlap > 0 and len(merged) > 1:
            overlapped = [merged[0]]
            for i in range(1, len(merged)):
                prev_tail = (
                    merged[i - 1][-overlap:]
                    if len(merged[i - 1]) > overlap
                    else merged[i - 1]
                )
                overlapped.append(f"{prev_tail}\n{merged[i]}")
            return overlapped

        return merged


# ═══════════════════════════════════════════════════
# 统一知识库
# ═══════════════════════════════════════════════════

class UnifiedKnowledgeBase:
    """全局统一知识库管理器。

    所有视频笔记共享一个 ChromaDB Collection，
    配合 BM25 实现混合检索，BGE-Reranker 精排。
    """
    _instance: Optional["UnifiedKnowledgeBase"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        os.makedirs(VECTOR_DB_DIR, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=VECTOR_DB_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        self._splitter = RecursiveTextSplitter()
        # 注意：BM25 必须在 _get_or_create_collection 之前初始化，
        # 因为 _get_or_create_collection 内部会访问 self._bm25 做一致性检查。
        self._bm25 = BM25Index(os.path.join(VECTOR_DB_DIR, "bm25_corpus.json"))
        self._reranker = None
        self._stats_cache: Optional[dict] = None
        self._stats_cache_at = 0.0
        self._collection = self._get_or_create_collection()
        self._initialized = True

    def _invalidate_stats(self) -> None:
        self._stats_cache = None
        self._stats_cache_at = 0.0

    def _get_reranker(self):
        if self._reranker is None:
            from app.services.reranker import get_reranker
            self._reranker = get_reranker()
        return self._reranker

    def _get_or_create_collection(self):
        """获取或创建统一 collection。"""
        # 先检查 collection 是否已存在（避免依赖异常控制流）
        existing_names = [c.name for c in self._client.list_collections()]

        if UNIFIED_COLLECTION in existing_names:
            col = self._client.get_collection(UNIFIED_COLLECTION)
            existing = col.count()
            logger.info(f"已加载统一知识库: {existing} chunks")
            # 如果 BM25 为空但 ChromaDB 有数据，重建 BM25
            if existing > 0 and len(self._bm25) == 0:
                logger.info("检测到 BM25 索引为空，正在从 ChromaDB 重建...")
                self._bm25.rebuild_from_chromadb(col)
            return col

        # 不存在则创建
        col = self._client.create_collection(
            name=UNIFIED_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("已创建统一知识库 collection")
        return col

    # ── 索引 ──────────────────────────────────────

    def index_note(self, task_id: str, note_data: Optional[dict] = None) -> int:
        """将一篇笔记索引到统一知识库。返回 chunk 数量。

        幂等操作：先删除旧索引，再添加新索引。
        """
        if note_data is None:
            note_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")
            if not os.path.exists(note_path):
                logger.warning(f"笔记文件不存在，跳过索引: {note_path}")
                return 0
            with open(note_path, "r", encoding="utf-8") as f:
                note_data = json.load(f)

        # 删除旧索引（幂等）
        self.delete_note(task_id)

        # 构建语义块
        chunks = self._build_chunks(task_id, note_data)
        if not chunks:
            logger.warning(f"笔记无可索引内容: {task_id}")
            return 0

        # 写入 ChromaDB
        ids = [c["id"] for c in chunks]
        documents = [c["text"] for c in chunks]
        metadatas = [c["metadata"] for c in chunks]

        self._collection.add(ids=ids, documents=documents, metadatas=metadatas)

        # 写入 BM25
        for c in chunks:
            self._bm25.add(c["id"], c["text"], c["metadata"])
        self._bm25.flush()
        self._invalidate_stats()

        logger.info(f"已索引笔记: task_id={task_id}, chunks={len(chunks)}")
        return len(chunks)

    def _build_chunks(self, task_id: str, note_data: dict) -> list[dict]:
        """构建笔记的所有语义块（meta + markdown + transcript）。"""
        video_title = (
            note_data.get("audio_meta", {}).get("title")
            or note_data.get("audio_meta", {}).get("raw_info", {}).get("title", "")
            or "未命名"
        )
        video_url = (
            note_data.get("audio_meta", {})
            .get("raw_info", {})
            .get("webpage_url", "")
        )
        platform = note_data.get("audio_meta", {}).get("platform", "")

        chunks = []
        idx = 0

        def base_meta(chunk_type: str, **extra) -> dict:
            return {
                "task_id": task_id,
                "source_type": chunk_type,
                "video_title": video_title,
                "video_url": video_url,
                "platform": platform,
                **extra,
            }

        # 1. Meta chunk
        for mc in self._build_meta_chunks(note_data.get("audio_meta", {})):
            chunks.append({
                "id": f"{task_id}_{idx}",
                "text": mc["text"],
                "metadata": base_meta("meta"),
            })
            idx += 1

        # 2. Markdown chunks（递归语义分块）
        markdown = note_data.get("markdown", "")
        if isinstance(markdown, list):
            markdown = markdown[-1].get("content", "") if markdown else ""

        # 先按标题拆分
        md_sections = re.split(
            r'(?=^#{2,3}\s)', markdown, flags=re.MULTILINE,
        )
        for section in md_sections:
            section = section.strip()
            if len(section) < MIN_CHUNK_LEN:
                continue

            heading_match = re.match(r'^(#{2,3})\s+(.+)', section)
            section_title = (
                heading_match.group(2).strip() if heading_match else "概述"
            )

            # 递归分割
            sub_chunks = self._splitter.split(section)
            for sc in sub_chunks:
                chunks.append({
                    "id": f"{task_id}_{idx}",
                    "text": sc,
                    "metadata": base_meta(
                        "markdown", section_title=section_title,
                    ),
                })
                idx += 1

        # 3. Transcript chunks（滑动窗口）
        segments = note_data.get("transcript", {}).get("segments", [])
        for tc in self._build_transcript_chunks(segments):
            chunks.append({
                "id": f"{task_id}_{idx}",
                "text": tc["text"],
                "metadata": base_meta(
                    "transcript",
                    start_time=tc["metadata"]["start_time"],
                    end_time=tc["metadata"]["end_time"],
                ),
            })
            idx += 1

        return chunks

    def _build_meta_chunks(self, audio_meta: dict) -> list[dict]:
        """构建视频元信息块。"""
        if not audio_meta:
            return []

        raw = audio_meta.get("raw_info", {}) or {}
        parts = []

        title = audio_meta.get("title") or raw.get("title", "")
        if title:
            parts.append(f"视频标题：{title}")

        uploader = raw.get("uploader", "")
        if uploader:
            parts.append(f"作者/UP主：{uploader}")

        desc = raw.get("description", "")
        if desc:
            parts.append(f"简介：{desc[:500]}")

        tags = raw.get("tags", [])
        if tags and isinstance(tags, list):
            parts.append(f"标签：{', '.join(str(t) for t in tags[:20])}")

        duration = audio_meta.get("duration", 0)
        if duration:
            m, s = divmod(int(duration), 60)
            parts.append(f"时长：{m}分{s}秒")

        platform = audio_meta.get("platform", "")
        if platform:
            parts.append(f"平台：{platform}")

        url = raw.get("webpage_url", "")
        if url:
            parts.append(f"链接：{url}")

        if not parts:
            return []

        return [{"text": "\n".join(parts), "metadata": {"source_type": "meta"}}]

    def _build_transcript_chunks(self, segments: list[dict]) -> list[dict]:
        """滑动窗口构建转录块。"""
        if not segments:
            return []

        window_size = 15
        overlap = 3
        step = max(window_size - overlap, 1)

        chunks = []
        for i in range(0, len(segments), step):
            window = segments[i : i + window_size]
            if not window:
                break

            text = "\n".join(
                f"[{seg.get('start', 0):.0f}s] {seg.get('text', '')}"
                for seg in window
            )

            if len(text.strip()) < MIN_CHUNK_LEN:
                continue

            chunks.append({
                "text": text,
                "metadata": {
                    "source_type": "transcript",
                    "start_time": window[0].get("start", 0),
                    "end_time": window[-1].get("end", 0),
                },
            })

        return chunks

    # ── 检索管道 ─────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 20,
        rerank_top_k: int = 5,
        source_filter: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> list[dict]:
        """
        完整检索管道：混合检索 → Reranker 精排 → 来源标注。

        Args:
            query: 搜索查询
            top_k: 混合检索候选数
            rerank_top_k: 精排后返回数
            source_filter: 来源过滤（meta/markdown/transcript/all）
            task_id: 限定到特定笔记

        Returns:
            精排后的文档列表，每个文档含 citation 字段
        """
        # Step 1: 混合检索
        start = time.perf_counter()
        candidates = self._hybrid_search(
            query, top_k, source_filter, task_id,
        )

        hybrid_elapsed = time.perf_counter() - start

        if not candidates:
            logger.info(f"知识库检索完成: query={query[:40]} candidates=0 elapsed={hybrid_elapsed:.2f}s")
            return []

        # Step 2: Reranker 精排
        rerank_start = time.perf_counter()
        ranked = self._get_reranker().rerank(query, candidates, top_k=rerank_top_k)
        rerank_elapsed = time.perf_counter() - rerank_start

        # Step 3: 附加来源引用
        for doc in ranked:
            doc["citation"] = self._build_citation(doc)

        logger.info(
            "知识库检索完成: "
            f"query={query[:40]} candidates={len(candidates)} results={len(ranked)} "
            f"hybrid={hybrid_elapsed:.2f}s rerank={rerank_elapsed:.2f}s "
            f"total={time.perf_counter() - start:.2f}s"
        )
        return ranked

    def _hybrid_search(
        self,
        query: str,
        top_k: int,
        source_filter: Optional[str],
        task_id: Optional[str],
    ) -> list[dict]:
        """向量检索 + BM25 → RRF 融合。"""
        where = self._build_where(source_filter, task_id)

        vec_chunks = []
        if KB_VECTOR_SEARCH_ENABLED:
            vector_start = time.perf_counter()
            logger.info(f"向量检索开始: query={query[:40]} top_k={top_k}")
            try:
                vec_results = self._collection.query(
                    query_texts=[query],
                    n_results=top_k,
                    where=where,
                )
                vec_chunks = self._parse_chroma_results(vec_results)
                logger.info(
                    f"向量检索完成: results={len(vec_chunks)} "
                    f"elapsed={time.perf_counter() - vector_start:.2f}s"
                )
            except Exception as e:
                logger.warning(f"向量检索失败: {e}")
        else:
            logger.info("向量检索已跳过: KB_VECTOR_SEARCH_ENABLED 未开启")

        bm25_start = time.perf_counter()
        logger.info(f"BM25 检索开始: query={query[:40]} top_k={top_k}")
        bm25_chunks = self._bm25.search(query, top_k=top_k)
        logger.info(
            f"BM25 检索完成: results={len(bm25_chunks)} "
            f"elapsed={time.perf_counter() - bm25_start:.2f}s"
        )

        if task_id:
            bm25_chunks = [
                r for r in bm25_chunks
                if r.get("metadata", {}).get("task_id") == task_id
            ]
        if source_filter and source_filter != "all":
            bm25_chunks = [
                r for r in bm25_chunks
                if r.get("metadata", {}).get("source_type") == source_filter
            ]

        # RRF 融合
        return self._rrf_merge(vec_chunks, bm25_chunks, top_k)

    def _build_where(
        self, source_filter: Optional[str], task_id: Optional[str],
    ) -> Optional[dict]:
        """构建 ChromaDB where 过滤条件。"""
        conditions = []
        if source_filter and source_filter != "all":
            conditions.append({"source_type": source_filter})
        if task_id:
            conditions.append({"task_id": task_id})

        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    def _parse_chroma_results(self, results: dict) -> list[dict]:
        """解析 ChromaDB query 结果。"""
        chunks = []
        if (
            not results
            or not results.get("documents")
            or not results["documents"][0]
        ):
            return chunks

        for i in range(len(results["documents"][0])):
            chunks.append({
                "id": results["ids"][0][i],
                "text": results["documents"][0][i],
                "metadata": (
                    results["metadatas"][0][i]
                    if results.get("metadatas") else {}
                ),
                "distance": (
                    results["distances"][0][i]
                    if results.get("distances") else None
                ),
            })
        return chunks

    def _rrf_merge(
        self,
        vec_results: list[dict],
        bm25_results: list[dict],
        top_k: int,
        k: int = 60,
    ) -> list[dict]:
        """Reciprocal Rank Fusion 融合两组搜索结果。"""
        scores: dict[str, tuple[float, dict]] = {}

        for rank, doc in enumerate(vec_results):
            doc_id = doc["id"]
            rrf = 1.0 / (k + rank + 1)
            prev, _ = scores.get(doc_id, (0, doc))
            scores[doc_id] = (prev + rrf, doc)

        for rank, doc in enumerate(bm25_results):
            doc_id = doc["id"]
            rrf = 1.0 / (k + rank + 1)
            prev, _ = scores.get(doc_id, (0, doc))
            scores[doc_id] = (prev + rrf, doc)

        sorted_items = sorted(
            scores.values(), key=lambda x: x[0], reverse=True,
        )
        return [doc for _, doc in sorted_items[:top_k]]

    def _build_citation(self, doc: dict) -> str:
        """构建人类可读的来源引用字符串。"""
        meta = doc.get("metadata", {})
        video_title = meta.get("video_title", "未知视频")
        source_type = meta.get("source_type", "unknown")

        if source_type == "meta":
            return f"📋 {video_title} · 视频信息"
        elif source_type == "markdown":
            section = meta.get("section_title", "")
            label = f"📝 {video_title}"
            return f"{label} · {section}" if section else label
        elif source_type == "transcript":
            start = meta.get("start_time", 0)
            end = meta.get("end_time", 0)
            return f"🎙 {video_title} [{start:.0f}s–{end:.0f}s]"
        return video_title

    # ── 管理 ─────────────────────────────────────

    def delete_note(self, task_id: str) -> None:
        """从知识库删除一篇笔记的所有 chunks。"""
        try:
            self._collection.delete(where={"task_id": task_id})
        except Exception as e:
            logger.warning(f"ChromaDB 删除失败: {e}")

        to_remove = self._bm25.search_by_metadata("task_id", task_id)
        for doc_id in to_remove:
            self._bm25.remove(doc_id)
        self._bm25.flush()
        self._invalidate_stats()

        logger.info(f"已删除笔记索引: {task_id}")

    def get_all_notes(self) -> list[dict]:
        """获取所有已索引笔记的摘要列表。"""
        try:
            meta_results = self._collection.get(where={"source_type": "meta"})
        except Exception:
            return []

        notes = []
        seen = set()
        if meta_results.get("ids"):
            for i in range(len(meta_results["ids"])):
                meta = meta_results["metadatas"][i]
                tid = meta.get("task_id", "")
                if tid in seen:
                    continue
                seen.add(tid)
                notes.append({
                    "task_id": tid,
                    "title": meta.get("video_title", "未命名"),
                    "platform": meta.get("platform", ""),
                    "url": meta.get("video_url", ""),
                })
        return notes

    def is_indexed(self, task_id: str) -> bool:
        """检查笔记是否已索引。"""
        try:
            result = self._collection.get(
                where={
                    "$and": [
                        {"task_id": task_id},
                        {"source_type": "meta"},
                    ],
                },
                limit=1,
            )
            return len(result["ids"]) > 0
        except Exception:
            return False

    def get_stats(self) -> dict:
        """知识库统计信息。"""
        now = time.perf_counter()
        if self._stats_cache and now - self._stats_cache_at < 5:
            return dict(self._stats_cache)
        try:
            stats = {
                "total_chunks": self._collection.count(),
                "total_notes": len(self.get_all_notes()),
                "bm25_docs": len(self._bm25),
                "bm25_available": self._bm25.available,
                "reranker_available": (
                    self._reranker.available if self._reranker is not None else False
                ),
            }
            self._stats_cache = dict(stats)
            self._stats_cache_at = now
            return stats
        except Exception:
            return {
                "total_chunks": 0, "total_notes": 0,
                "bm25_docs": 0, "bm25_available": False,
                "reranker_available": False,
            }

    def rebuild_bm25(self) -> int:
        """从 ChromaDB 重建 BM25 索引（修复不一致）。"""
        count = self._bm25.rebuild_from_chromadb(self._collection)
        self._invalidate_stats()
        return count


def get_unified_knowledge_base() -> UnifiedKnowledgeBase:
    return UnifiedKnowledgeBase()
