# -*- coding: utf-8 -*-
"""T1 检索评测：同一评测集对比 4 种检索策略。

策略：
  vector        纯向量（ChromaDB 默认 MiniLM 嵌入）
  bm25          纯 BM25（jieba 分词）
  hybrid_rrf    向量 + BM25 → RRF(k=60) 融合（与生产 _hybrid_search 一致）
  hybrid_rerank RRF 候选 → BGE-Reranker-v2-M3 交叉编码器精排

指标：Recall@5 / MRR / NDCG@10（region 级二值相关性）+ 每查询平均延迟。

相关性判定（与 chunk 粒度无关，跨粒度可比）：
  markdown chunk：同 task_id 且同 section_title
  transcript chunk：同 task_id 且时间窗重叠 >1s

chunk 粒度对比：--chunk-sizes 125 250 500（仅影响 markdown 分块；
transcript 为固定 15 段滑窗，与生产一致）。

用法（在 backend/ 下）：
  python tests/eval_retrieval.py                       # 仅 250（生产默认）
  python tests/eval_retrieval.py --chunk-sizes 125 250 500
  python tests/eval_retrieval.py --skip-rerank         # 跳过精排
"""
import argparse
import json
import math
import os
import shutil
import sys
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, TESTS_DIR)

from gen_eval_queries import load_notes  # noqa: E402

import app.services.knowledge_base as kb  # noqa: E402

EVAL_DATA = os.path.join(TESTS_DIR, "eval_data")
QUERIES_PATH = os.path.join(TESTS_DIR, "eval_queries.json")
RESULTS_PATH = os.path.join(TESTS_DIR, "eval_results.json")
RERANKER_LOCAL_DIR = os.getenv(
    "RERANKER_LOCAL_DIR", r"D:\projects\models\bge-reranker-v2-m3",
)
BGE_ZH_DIR = os.getenv(
    "BGE_ZH_DIR", r"D:\projects\models\bge-small-zh-v1.5",
)

TOPK = 20          # 每策略候选深度（RRF 融合输入）
EPS_S = 1.0        # transcript 时间窗重叠判定容差（秒）


class STEmbedding:
    """sentence-transformers 嵌入函数（Chroma 自定义 embedding_function）。

    chromadb 默认嵌入是英文 MiniLM，对中文文本基本失效（见评测数字），
    --embedding bge-zh 时使用本地 BAAI/bge-small-zh-v1.5。
    """

    def __init__(self, path: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(path)

    def __call__(self, input):  # noqa: A002 - chroma 协议签名
        return self.model.encode(
            list(input), show_progress_bar=False,
            normalize_embeddings=True,
        ).tolist()

    def embed_documents(self, input):  # noqa: A002
        return self(input)

    def embed_query(self, input):  # noqa: A002
        return self(input)

    def name(self) -> str:
        return "st-bge-small-zh-v1.5"


class SizedSplitter(kb.RecursiveTextSplitter):
    """把指定 chunk 粒度注入生产分割器（保持生产分块逻辑不变）。"""

    def __init__(self, size: int, overlap: int):
        self._size, self._overlap = size, overlap

    def split(self, text, chunk_size=None, overlap=None):
        return super().split(text, self._size, self._overlap)


def build_corpus(chunk_size: int):
    """用生产 _build_chunks 构建整个语料。返回 (chunks, kb_inst)。"""
    overlap = max(chunk_size // 3, 20)
    inst = object.__new__(kb.UnifiedKnowledgeBase)
    inst._splitter = SizedSplitter(chunk_size, overlap)
    chunks = []
    for _title, note in load_notes().items():
        chunks.extend(inst._build_chunks(note["_task_id"], note))
    return chunks, inst


def build_indexes(chunks: list[dict], chunk_size: int, ef=None):
    """在临时目录建立 ChromaDB + BM25 索引。"""
    index_dir = os.path.join(EVAL_DATA, f"index_{chunk_size}")
    shutil.rmtree(index_dir, ignore_errors=True)
    os.makedirs(index_dir, exist_ok=True)

    import chromadb
    from chromadb.config import Settings

    t0 = time.perf_counter()
    client = chromadb.PersistentClient(
        path=os.path.join(index_dir, "chroma"),
        settings=Settings(anonymized_telemetry=False),
    )
    col = client.create_collection(
        name=f"eval_{chunk_size}", metadata={"hnsw:space": "cosine"},
        embedding_function=ef,
    )
    ids = [c["id"] for c in chunks]
    docs = [c["text"] for c in chunks]
    metas = [
        {k: v for k, v in c["metadata"].items() if isinstance(v, (str, int, float))}
        for c in chunks
    ]
    col.add(ids=ids, documents=docs, metadatas=metas)
    embed_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    bm25 = kb.BM25Index(os.path.join(index_dir, "bm25_corpus.json"))
    for c in chunks:
        bm25.add(c["id"], c["text"], c["metadata"])
    bm25_s = time.perf_counter() - t1
    return col, bm25, embed_s, bm25_s, index_dir


def relevant_ids(q: dict, chunks: list[dict]) -> set[str]:
    """按 region 判定相关 chunk 集合。"""
    rel = set()
    for c in chunks:
        m = c["metadata"]
        if m["task_id"] != q["task_id"]:
            continue
        if m["source_type"] == "markdown" and q["source_type"] == "markdown":
            if m.get("section_title") == q.get("section_title"):
                rel.add(c["id"])
        elif m["source_type"] == "transcript" and q["source_type"] == "transcript":
            if (min(m["end_time"], q["end_time"])
                    - max(m["start_time"], q["start_time"]) > EPS_S):
                rel.add(c["id"])
    return rel


def vector_search(col, query: str) -> list[dict]:
    n = min(TOPK, col.count())
    if n == 0:
        return []
    res = col.query(query_texts=[query], n_results=n)
    return [
        {"id": res["ids"][0][i], "text": res["documents"][0][i],
         "metadata": (res["metadatas"][0][i] if res.get("metadatas") else {})}
        for i in range(len(res["ids"][0]))
    ]


def make_reranker():
    """加载本地 BGE-Reranker（生产 rerank() 逻辑，绕过 RERANKER_ENABLED 门）。"""
    from sentence_transformers import CrossEncoder
    from app.services.reranker import Reranker

    t0 = time.perf_counter()
    model = CrossEncoder(RERANKER_LOCAL_DIR)
    load_s = time.perf_counter() - t0

    r = object.__new__(Reranker)  # 跳过 __init__ 的环境开关检查，注入本地模型
    r.model_name = RERANKER_LOCAL_DIR
    r._model = model
    r._available = True
    r._enabled = True
    return r, load_s


# ── 指标 ────────────────────────────────────────

def recall_at_k(ranked: list[str], rel: set[str], k: int) -> float:
    if not rel:
        return 0.0
    return len(rel & set(ranked[:k])) / len(rel)


def mrr(ranked: list[str], rel: set[str]) -> float:
    for i, cid in enumerate(ranked, 1):
        if cid in rel:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], rel: set[str], k: int) -> float:
    if not rel:
        return 0.0
    dcg = sum(
        1.0 / math.log2(i + 2)
        for i, cid in enumerate(ranked[:k]) if cid in rel
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(rel), k)))
    return dcg / ideal if ideal else 0.0


# ── 主流程 ──────────────────────────────────────

def run(chunk_sizes: list[int], skip_rerank: bool, embedding: str):
    queries = json.load(open(QUERIES_PATH, encoding="utf-8"))
    print(f"评测集: {len(queries)} 条 query")

    ef = None
    embed_name = "chromadb 默认 all-MiniLM-L6-v2 (ONNX, 英文模型)"
    if embedding == "bge-zh":
        print(f"加载中文嵌入模型: {BGE_ZH_DIR} ...")
        ef = STEmbedding(BGE_ZH_DIR)
        embed_name = "BAAI/bge-small-zh-v1.5 (本地)"

    reranker, load_s = (None, 0.0)
    if not skip_rerank:
        print(f"加载 BGE-Reranker: {RERANKER_LOCAL_DIR} ...")
        reranker, load_s = make_reranker()
        print(f"Reranker 加载完成 ({load_s:.1f}s)")

    results = []
    for size in chunk_sizes:
        print(f"\n===== chunk_size={size} =====")
        chunks, inst = build_corpus(size)
        print(f"语料: {len(chunks)} chunks / 6 notes")
        col, bm25, embed_s, bm25_s, index_dir = build_indexes(chunks, size, ef)
        print(f"索引: 向量 {embed_s:.1f}s, BM25 {bm25_s:.1f}s")

        strategies = ["vector", "bm25", "hybrid_rrf"] + (
            [] if skip_rerank else ["hybrid_rerank"])
        acc = {s: {"r5": [], "mrr": [], "ndcg": [], "lat": []}
               for s in strategies}
        per_query = []

        for q in queries:
            rel = relevant_ids(q, chunks)
            t0 = time.perf_counter()
            vec = vector_search(col, q["query"])
            t_vec = time.perf_counter() - t0
            t0 = time.perf_counter()
            bm = bm25.search(q["query"], top_k=TOPK)
            t_bm = time.perf_counter() - t0
            t0 = time.perf_counter()
            rrf = inst._rrf_merge(vec, bm, TOPK)
            t_rrf = time.perf_counter() - t0

            runs = {
                "vector": (vec, t_vec),
                "bm25": (bm, t_bm),
                "hybrid_rrf": (rrf, t_vec + t_bm + t_rrf),
            }
            if reranker is not None:
                t0 = time.perf_counter()
                rr = reranker.rerank(q["query"], list(rrf), top_k=5)
                runs["hybrid_rerank"] = (rr, time.perf_counter() - t0)

            row = {"id": q["id"], "n_rel": len(rel)}
            for s, (docs, lat) in runs.items():
                ids = [d["id"] for d in docs]
                acc[s]["r5"].append(recall_at_k(ids, rel, 5))
                acc[s]["mrr"].append(mrr(ids, rel))
                acc[s]["ndcg"].append(ndcg_at_k(ids, rel, 10))
                acc[s]["lat"].append(lat)
                row[s] = {
                    "recall@5": round(recall_at_k(ids, rel, 5), 4),
                    "first_rel_rank": next(
                        (i + 1 for i, c in enumerate(ids) if c in rel), None),
                }
            per_query.append(row)

        summary = {
            "chunk_size": size,
            "chunk_count": len(chunks),
            "index_seconds": {"vector": round(embed_s, 2),
                              "bm25": round(bm25_s, 2)},
            "strategies": {
                s: {
                    "recall@5": round(sum(v["r5"]) / len(v["r5"]), 4),
                    "mrr": round(sum(v["mrr"]) / len(v["mrr"]), 4),
                    "ndcg@10": round(sum(v["ndcg"]) / len(v["ndcg"]), 4),
                    "avg_latency_s": round(sum(v["lat"]) / len(v["lat"]), 3),
                } for s, v in acc.items()
            },
            "per_query": per_query,
        }
        results.append(summary)

        header = f"{'策略':<14}{'Recall@5':>10}{'MRR':>8}{'NDCG@10':>10}{'延迟s':>8}"
        print(header)
        for s, v in summary["strategies"].items():
            print(f"{s:<14}{v['recall@5']:>10.4f}{v['mrr']:>8.4f}"
                  f"{v['ndcg@10']:>10.4f}{v['avg_latency_s']:>8.3f}")

    meta = {
        "corpus": "6 篇去重视频笔记（4 妙界按摩仪测评 + 2 CS2 分辨率事件）",
        "queries": len(queries),
        "embedding": embed_name,
        "reranker": None if skip_rerank else
            f"bge-reranker-v2-m3 (local, load {load_s:.1f}s)",
        "relevance": "region 级二值（markdown: 同 section；transcript: 时间窗重叠>1s）",
        "date": time.strftime("%Y-%m-%d %H:%M"),
    }
    out_path = RESULTS_PATH if embedding == "bge-zh" else (
        RESULTS_PATH.replace(".json", f"_{embedding}.json"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "results": results}, f,
                  ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-sizes", type=int, nargs="+", default=[250])
    ap.add_argument("--skip-rerank", action="store_true")
    ap.add_argument("--embedding", choices=["bge-zh", "default"],
                    default="bge-zh")
    args = ap.parse_args()
    run(args.chunk_sizes, args.skip_rerank, args.embedding)
