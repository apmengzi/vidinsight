"""
BM25 关键词检索引擎。
维护持久化的文档语料库，支持增量添加/删除和中文分词。
"""
import json
import os
from typing import Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


class BM25Index:
    """基于 rank_bm25 的关键词检索引擎，使用 jieba 中文分词。"""

    def __init__(self, persist_path: str):
        self.persist_path = persist_path
        self.documents: list[dict] = []  # [{id, text, metadata}]
        self._bm25: Optional["BM25Okapi"] = None
        self._tokenized_corpus: list[list[str]] = []
        self._dirty = False
        self._needs_persist = False
        self._load()

    def _tokenize(self, text: str) -> list[str]:
        """中文分词 + 英文小写化。"""
        try:
            import jieba
            return [t.lower().strip() for t in jieba.cut(text) if t.strip()]
        except ImportError:
            # 退化：简单按空白和标点切分
            import re
            tokens = re.findall(r'[一-鿿]+|[a-zA-Z]+|\d+', text)
            return [t.lower() for t in tokens]

    def add(self, doc_id: str, text: str, metadata: dict) -> None:
        """添加文档到索引（幂等：先删后加）。"""
        self.remove(doc_id)
        self.documents.append({"id": doc_id, "text": text, "metadata": metadata})
        self._tokenized_corpus.append(self._tokenize(text))
        self._dirty = True
        self._needs_persist = True

    def remove(self, doc_id: str) -> None:
        """从索引中删除指定文档。"""
        indices = [i for i, d in enumerate(self.documents) if d["id"] == doc_id]
        if not indices:
            return
        for i in reversed(indices):
            self.documents.pop(i)
            self._tokenized_corpus.pop(i)
        self._dirty = True
        self._needs_persist = True

    def search(self, query: str, top_k: int = 20) -> list[dict]:
        """BM25 关键词搜索，返回 top_k 个结果。"""
        if not self.documents:
            return []
        if self._dirty or self._bm25 is None:
            self._build()
        if self._bm25 is None:
            return []

        tokenized_query = self._tokenize(query)
        if not tokenized_query:
            return []

        scores = self._bm25.get_scores(tokenized_query)

        ranked = sorted(
            [(i, scores[i]) for i in range(len(scores)) if scores[i] > 0],
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]

        return [
            {**self.documents[i], "bm25_score": float(score)}
            for i, score in ranked
        ]

    def search_by_metadata(self, filter_key: str, filter_value) -> list[str]:
        """按元数据过滤返回匹配的 doc_id 列表。"""
        return [
            d["id"] for d in self.documents
            if d.get("metadata", {}).get(filter_key) == filter_value
        ]

    def _build(self) -> None:
        """重建 BM25 索引。"""
        if not self._tokenized_corpus:
            self._bm25 = None
            return
        try:
            from rank_bm25 import BM25Okapi
            self._bm25 = BM25Okapi(self._tokenized_corpus)
        except ImportError:
            logger.warning("rank_bm25 未安装，BM25 搜索不可用")
            self._bm25 = None
        self._dirty = False
        if self._needs_persist:
            self.flush()

    def flush(self) -> None:
        """Persist pending corpus changes to disk."""
        if not self._needs_persist:
            return
        self._persist()
        self._needs_persist = False

    def _persist(self) -> None:
        """持久化语料到磁盘。"""
        try:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            data = [
                {"id": d["id"], "text": d["text"], "metadata": d["metadata"]}
                for d in self.documents
            ]
            with open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            logger.info(f"BM25 索引已持久化: {len(data)} 文档 → {self.persist_path}")
        except Exception as e:
            logger.error(f"BM25 持久化失败: {e}")

    def _load(self) -> None:
        """从磁盘加载语料。"""
        if not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.documents = data
            self._tokenized_corpus = [self._tokenize(d["text"]) for d in data]
            self._dirty = True
            self._needs_persist = False
            logger.info(f"BM25 索引已加载: {len(data)} 文档")
        except Exception as e:
            logger.error(f"BM25 加载失败: {e}")

    def clear(self) -> None:
        """清空索引。"""
        self.documents = []
        self._tokenized_corpus = []
        self._bm25 = None
        self._dirty = False
        self._needs_persist = False
        if os.path.exists(self.persist_path):
            os.remove(self.persist_path)

    def rebuild_from_chromadb(self, collection) -> int:
        """
        从 ChromaDB collection 重建 BM25 索引。
        用于初始化或修复不一致。
        """
        self.clear()
        try:
            all_data = collection.get()
        except Exception as e:
            logger.error(f"从 ChromaDB 读取失败: {e}")
            return 0

        if not all_data or not all_data.get("ids"):
            return 0

        count = 0
        for i in range(len(all_data["ids"])):
            doc_id = all_data["ids"][i]
            text = all_data["documents"][i] if all_data.get("documents") else ""
            metadata = all_data["metadatas"][i] if all_data.get("metadatas") else {}
            self.add(doc_id, text, metadata)
            count += 1

        self.flush()
        logger.info(f"BM25 索引已从 ChromaDB 重建: {count} 文档")
        return count

    @property
    def available(self) -> bool:
        """BM25 搜索是否可用。"""
        try:
            import rank_bm25  # noqa: F401
            return True
        except ImportError:
            return False

    def __len__(self) -> int:
        return len(self.documents)
