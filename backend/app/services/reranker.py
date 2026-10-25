"""
BGE-Reranker 精排模块。
使用 BAAI/bge-reranker-v2-m3 交叉编码器对检索结果重排序。
若 sentence-transformers 不可用或加载失败，退化为直接截断。
"""
import os
import time
from typing import Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 全局单例
_RERANKER_INSTANCE: Optional["Reranker"] = None

# 可配置的模型名
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "0").lower() in ("1", "true", "yes")
RERANKER_MAX_CANDIDATES = int(os.getenv("RERANKER_MAX_CANDIDATES", "8"))


class Reranker:
    """BGE 交叉编码器精排器（基于 sentence-transformers CrossEncoder）。

    使用示例:
        reranker = get_reranker()
        ranked = reranker.rerank("查询", [{"text": "文档1"}, ...], top_k=5)
    """

    def __init__(
        self,
        model_name: str = DEFAULT_RERANKER_MODEL,
        use_fp16: bool = True,
    ):
        self.model_name = model_name
        self._model = None
        self._available = False
        self._enabled = RERANKER_ENABLED
        if not self._enabled:
            logger.info("BGE-Reranker 已关闭: RERANKER_ENABLED 未开启，检索将直接返回候选结果")
            return
        self._init_model(use_fp16)

    def _init_model(self, use_fp16: bool) -> None:
        """初始化 CrossEncoder 模型。"""
        try:
            from sentence_transformers import CrossEncoder
            logger.info(f"正在加载 BGE-Reranker 模型: {self.model_name} ...")

            self._model = CrossEncoder(
                self.model_name,
                trust_remote_code=True,
                model_kwargs={"torch_dtype": "float16"} if use_fp16 else None,
            )
            # 预热一次确保分词器正常
            _ = self._model.predict([["预热", "测试"]], show_progress_bar=False)
            self._available = True
            logger.info("BGE-Reranker 模型加载完成 ✓")
        except ImportError:
            logger.warning(
                "sentence-transformers 未安装，BGE-Reranker 不可用。"
                "请执行: pip install sentence-transformers"
            )
            self._available = False
        except Exception as e:
            logger.warning(f"BGE-Reranker 加载失败: {e}")
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def rerank(
        self,
        query: str,
        documents: list[dict],
        top_k: int = 5,
    ) -> list[dict]:
        """
        对文档列表重排序。

        Args:
            query: 用户查询
            documents: 文档列表，每个文档需包含 "text" 字段
            top_k: 返回数量

        Returns:
            重排序后的 top_k 文档，每个文档附加 "rerank_score" 字段
        """
        if not documents:
            return []

        if not self._available or self._model is None:
            return documents[:top_k]

        try:
            start = time.perf_counter()
            documents = documents[:RERANKER_MAX_CANDIDATES]
            # 截断过长文本，避免超出模型上下文窗口（512 tokens）
            pairs = [[query, doc["text"][:512]] for doc in documents]

            scores = self._model.predict(
                pairs,
                show_progress_bar=False,
                convert_to_tensor=True,
            )

            # BGE-Reranker 输出 logits，sigmoid 归一化到 [0, 1]
            import torch
            scores = torch.sigmoid(scores).cpu().tolist()
            logger.info(
                f"BGE-Reranker 精排完成: docs={len(documents)} "
                f"elapsed={time.perf_counter() - start:.2f}s"
            )

            # 处理单文档情况
            if isinstance(scores, float):
                scores = [scores]

            # 按分数降序
            ranked = sorted(
                zip(documents, scores),
                key=lambda x: x[1],
                reverse=True,
            )[:top_k]

            return [
                {**doc, "rerank_score": float(score)}
                for doc, score in ranked
                if float(score) > 0
            ] or documents[:top_k]

        except Exception as e:
            logger.error(f"Rerank 失败，退回原始排序: {e}")
            return documents[:top_k]

    def score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        """
        对 (query, document) 对列表打分（不排序）。

        Returns:
            归一化分数列表，范围 [0, 1]
        """
        if not self._available or self._model is None:
            return [1.0] * len(pairs)

        try:
            import torch
            scores = self._model.predict(
                pairs, show_progress_bar=False, convert_to_tensor=True,
            )
            scores = torch.sigmoid(scores).cpu().tolist()
            if isinstance(scores, float):
                return [float(scores)]
            return [float(s) for s in scores]
        except Exception:
            return [1.0] * len(pairs)


def get_reranker(
    model_name: str = DEFAULT_RERANKER_MODEL,
    use_fp16: bool = True,
) -> Reranker:
    """获取全局 Reranker 单例。"""
    global _RERANKER_INSTANCE
    if _RERANKER_INSTANCE is None:
        _RERANKER_INSTANCE = Reranker(model_name=model_name, use_fp16=use_fp16)
    return _RERANKER_INSTANCE


def reset_reranker() -> None:
    """重置全局 Reranker（用于测试或配置变更）。"""
    global _RERANKER_INSTANCE
    _RERANKER_INSTANCE = None
