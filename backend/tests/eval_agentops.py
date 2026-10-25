# -*- coding: utf-8 -*-
"""T1/T4 AgentOps 聚合：把 T4 轨迹日志聚合成「成功率 / 延迟 / 成本」口径。

输出（stdout + tests/eval_agentops.json）：
  - 请求级：总数、各终态计数、成功率（answered / 总数）
  - 延迟：total_ms 的 P50 / P95
  - 成本：prompt/completion token 均值与合计；--price-prompt/--price-completion
    （元 / 百万 token，默认按 deepseek-chat 价 2/8）折算单次成本
  - 检索评测表（eval_results.json，若存在）原样并列，构成完整四列口径

用法（backend/ 下）：python tests/eval_agentops.py [--price-prompt 2 --price-completion 8]
"""
import argparse
import json
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 生产（PyInstaller 冻结）轨迹在 %APPDATA%/VidInsight/traces；dev 在 backend/runtime。
# --trace-dir 可显式覆盖。
_CANDIDATE_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "VidInsight" / "traces"
    if os.environ.get("APPDATA") else None,
]


def _default_trace_dir() -> Path:
    for cand in _CANDIDATE_DIRS:
        if cand and cand.is_dir() and any(cand.glob("*.jsonl")):
            return cand
    from app.services.agent_trace import TRACE_DIR
    return TRACE_DIR


RESULTS_PATH = TESTS_DIR / "eval_results.json"
OUT_PATH = TESTS_DIR / "eval_agentops.json"


def load_traces() -> list[list[dict]]:
    traces = []
    for p in sorted(TRACE_DIR_RESOLVED.glob("*.jsonl")):
        events = []
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
        if events:
            traces.append(events)
    return traces


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, round(q * (len(vs) - 1))))
    return vs[idx]


def aggregate(traces: list[list[dict]], price_prompt: float, price_completion: float) -> dict:
    finals = [evs[-1] for evs in traces if evs and evs[-1].get("event") == "final"]
    total = len(finals)
    # answered（轮次内完成）与 forced_summary（5 轮打满后强制总结）都产出真实回答
    _OK_STATUSES = ("answered", "forced_summary")
    answered = sum(1 for f in finals if f.get("status") in _OK_STATUSES)
    total_ms = [float(f.get("total_ms") or 0) for f in finals]
    pt = [float(f.get("prompt_tokens") or 0) for f in finals]
    ct = [float(f.get("completion_tokens") or 0) for f in finals]

    def avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else 0.0

    per_req_cost = [
        (p / 1e6 * price_prompt) + (c / 1e6 * price_completion)
        for p, c in zip(pt, ct)
    ]

    status_counts: dict[str, int] = {}
    for f in finals:
        status_counts[f.get("status") or "unknown"] = \
            status_counts.get(f.get("status") or "unknown", 0) + 1

    rounds = [float(f.get("rounds") or 0) for f in finals]

    return {
        "requests": total,
        "status_counts": status_counts,
        "success_rate": round(answered / total, 4) if total else None,
        "latency_ms": {
            "avg": avg(total_ms),
            "p50": round(percentile(total_ms, 0.50), 1),
            "p95": round(percentile(total_ms, 0.95), 1),
        },
        "tokens": {
            "prompt_avg": avg(pt),
            "completion_avg": avg(ct),
        },
        "cost_cny_per_request_avg": round(
            sum(per_req_cost) / len(per_req_cost), 5) if per_req_cost else 0.0,
        "rounds_avg": avg(rounds),
        "price_cny_per_1m": {"prompt": price_prompt, "completion": price_completion},
    }


def load_retrieval_table() -> dict | None:
    try:
        data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    # 取目标 chunk_size（默认 250 生产口径）的策略汇总
    for r in data.get("results", []):
        if r.get("chunk_size") == 250:
            return {"meta": data.get("meta"), "chunk_size": 250,
                    "strategies": r.get("strategies")}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--price-prompt", type=float, default=2.0,
                    help="prompt 价（元/百万 token），默认 deepseek-chat 2")
    ap.add_argument("--price-completion", type=float, default=8.0,
                    help="completion 价（元/百万 token），默认 8")
    ap.add_argument("--trace-dir", type=str, default="",
                    help="轨迹目录（默认自动探测：生产 AppData → dev runtime）")
    args = ap.parse_args()

    global TRACE_DIR_RESOLVED
    TRACE_DIR_RESOLVED = Path(args.trace_dir) if args.trace_dir else _default_trace_dir()
    traces = load_traces()
    if not traces:
        print(f"traces 目录为空（{TRACE_DIR_RESOLVED}）——先在对话里跑几次 Agent 问答，或部署 T4 后积累数据。")
    agg = aggregate(traces, args.price_prompt, args.price_completion)
    retrieval = load_retrieval_table()

    out = {
        "meta": {
            "trace_dir": str(TRACE_DIR_RESOLVED),
            "note": "成功率/延迟/成本来自 T4 轨迹聚合；检索表来自 eval_results.json（bge-zh, 40 评测集）",
        },
        "agent_pipeline": agg,
        "retrieval_250": retrieval,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    print("=" * 60)
    print(f"AgentOps 汇总（Agent 全链路，来源 T4 轨迹 {TRACE_DIR_RESOLVED}）")
    print("=" * 60)
    print(f"请求数: {agg['requests']}  终态: {agg['status_counts']}")
    print(f"成功率: {agg['success_rate']}")
    print(f"延迟: avg {agg['latency_ms']['avg']}ms  P50 {agg['latency_ms']['p50']}ms  "
          f"P95 {agg['latency_ms']['p95']}ms")
    print(f"token: prompt avg {agg['tokens']['prompt_avg']}  "
          f"completion avg {agg['tokens']['completion_avg']}  "
          f"单次成本 ≈ {agg['cost_cny_per_request_avg']} 元")
    print(f"平均 ReAct 轮次: {agg['rounds_avg']}")
    if retrieval:
        print("-" * 60)
        print("检索评测（40 评测集, chunk=250）")
        for name, m in (retrieval.get("strategies") or {}).items():
            print(f"  {name:<14} recall@5={m.get('recall@5')} "
                  f"mrr={m.get('mrr')} ndcg@10={m.get('ndcg@10')} "
                  f"延迟={m.get('avg_latency_s')}s")
    print(f"\n已写入 {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
