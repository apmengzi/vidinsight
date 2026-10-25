# -*- coding: utf-8 -*-
"""T1 检索评测集生成脚本。

从评测语料（去重后的真实视频笔记）按生产分块逻辑切块，
分层抽样后用 LLM 为每个 chunk 生成一条自然语言检索 query，
输出 backend/tests/eval_queries.json（region 级相关性标注，跨 chunk 粒度可比）。

用法（在 backend/ 目录下）:
    python tests/gen_eval_queries.py
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

NOTES_DIR = os.path.join(BACKEND_DIR, "tests", "eval_data", "notes")
OUT_PATH = os.path.join(BACKEND_DIR, "tests", "eval_queries.json")

QUERIES_PER_NOTE = 7

# TokenRhythm 网关（OpenAI 兼容）
API_BASE = "https://tokenrhythm.studio/v1/chat/completions"
MODEL = "deepseek-v4-flash-0731"


def get_api_key() -> str:
    """从本地数据库读取 API key（key 不落代码）。"""
    db = os.path.join(os.environ["APPDATA"], "VidInsight", "vidinsight.db")
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT api_key FROM providers WHERE name='tokenrhythm'",
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise RuntimeError("DB 中未找到 tokenrhythm key")
    return row[0]


def load_notes() -> dict[str, dict]:
    """加载去重后的笔记：同名视频只保留 markdown 最长的一份。"""
    candidates: dict[str, dict] = {}
    for fn in os.listdir(NOTES_DIR):
        if not re.fullmatch(r"[0-9a-f-]{36}\.json", fn):
            continue
        with open(os.path.join(NOTES_DIR, fn), encoding="utf-8") as f:
            data = json.load(f)
        md = data.get("markdown") or ""
        if isinstance(md, list):
            md = md[-1].get("content", "") if md else ""
        data["markdown"] = md
        title = (data.get("audio_meta") or {}).get("title") or fn
        prev = candidates.get(title)
        if prev is None or len(md) > len(prev["markdown"]):
            data["_task_id"] = fn[: -len(".json")]
            candidates[title] = data
    return candidates


def build_chunks(task_id: str, note: dict) -> list[dict]:
    """用生产分块逻辑（250 字递归分割 + 转录滑窗）构建 chunk。"""
    import app.services.knowledge_base as kb

    inst = object.__new__(kb.UnifiedKnowledgeBase)
    inst._splitter = kb.RecursiveTextSplitter()
    return inst._build_chunks(task_id, note)


def sample_chunks(chunks: list[dict], n: int) -> list[dict]:
    """分层抽样：markdown 与 transcript 都覆盖，同 section 不重复。"""
    md_pool, tr_pool = [], []
    seen_sections = set()
    for c in chunks:
        meta = c["metadata"]
        text = c["text"]
        if len(text) < 60:
            continue
        if meta["source_type"] == "markdown":
            sec = meta.get("section_title", "")
            if sec in seen_sections:
                continue
            md_pool.append(c)
        elif meta["source_type"] == "transcript":
            tr_pool.append(c)

    picked = []
    # markdown 与 transcript 交替取，保证两类均衡
    half = n // 2
    for c in md_pool[: max(half, 1)]:
        seen_sections.add(c["metadata"].get("section_title", ""))
        picked.append(c)
    step = max(len(tr_pool) // max(n - len(picked), 1), 1)
    for c in tr_pool[::step][: n - len(picked)]:
        picked.append(c)
    return picked[:n]


def llm_query(api_key: str, title: str, text: str) -> str:
    """LLM 依据 chunk 内容生成一条检索 query。"""
    prompt = f"""你是搜索查询标注员。下面是一段来自视频《{title}》的内容片段。

片段：
{text[:600]}

请站在「看过这个视频的用户想再次找到这段内容」的角度，写 1 条他会输入的中文搜索 query。
要求：
1. 8~25 个字，信息具体（包含片段中的关键实体/观点）
2. 用自己的话概括，不要照抄片段原句
3. 只输出 JSON：{{"query": "..."}}"""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 100,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_BASE, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            m = re.search(r"\{.*\}", content, re.DOTALL)
            return json.loads(m.group(0))["query"].strip()
        except Exception as e:
            print(f"    [retry {attempt + 1}] {e}")
            time.sleep(2 * (attempt + 1))
    return ""


def main():
    api_key = get_api_key()
    notes = load_notes()
    print(f"去重后笔记数: {len(notes)}")

    items = []
    for title, note in notes.items():
        chunks = build_chunks(note["_task_id"], note)
        picked = sample_chunks(chunks, QUERIES_PER_NOTE)
        print(f"\n[{note['_task_id'][:8]}] {title[:36]}  "
              f"chunks={len(chunks)} sampled={len(picked)}")
        for c in picked:
            q = llm_query(api_key, title, c["text"])
            if not q:
                continue
            meta = c["metadata"]
            item = {
                "query": q,
                "task_id": meta["task_id"],
                "source_type": meta["source_type"],
                "origin_chunk_id": c["id"],
                "video_title": title,
            }
            if meta["source_type"] == "markdown":
                item["section_title"] = meta.get("section_title", "")
            else:
                item["start_time"] = meta.get("start_time", 0)
                item["end_time"] = meta.get("end_time", 0)
            items.append(item)
            print(f"    {q}   <- {c['id']}")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"\n共 {len(items)} 条 query → {OUT_PATH}")


if __name__ == "__main__":
    main()
