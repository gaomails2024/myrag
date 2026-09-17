#!/usr/bin/env python3
"""检索评测（PRD §8.5）—— **只读**：不写库、不改配置、不动生产代码。

用法：

    .venv/bin/python scripts/eval.py                          # 跑基线，报告存 eval/reports/
    .venv/bin/python scripts/eval.py --label rerank-on        # 给报告打标签，便于前后对比
    .venv/bin/python scripts/eval.py --mode dense             # 只跑单路，对比融合是否有增益
    .venv/bin/python scripts/eval.py --k 1,3,5,10,20 --limit 30

两个设计决定及其理由：

1. **走 HTTP 调 /api/search，不 import server.search**
   测的必须是「使用者实际走的那条链路」（路由、参数校验、过滤、limit 截断都在里面）。
   import 内部函数会绕过这些，数字好看但不对应真实行为。

2. **用 IR 指标（Recall/MRR/nDCG）而不是直接上 RAGAS**
   它们只依赖「哪些文章算回答了这个问题」，纯本地计算、零成本、**每次改动都能跑**。
   RAGAS 是需要 LLM 评判的更深一层复核（PRD §8.5），不是替代品：
   日常体检用这个，调参 / 发版前再用 RAGAS 复核。
"""

import argparse
import json
import math
import pathlib
import sys
import time
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:
    sys.exit("缺少 pyyaml：.venv/bin/pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_QUERIES = ROOT / "eval" / "queries.yaml"
DEFAULT_OUT = ROOT / "eval" / "reports"


# ================================================================ 取数

def load_queries(path):
    """读评测集。文件格式见 eval/queries.yaml 顶部的构造纪律。"""
    doc = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    items = doc.get("queries") or []
    if not items:
        sys.exit("评测集是空的：%s" % path)
    for it in items:
        if not it.get("query") or not it.get("relevant"):
            sys.exit("条目缺 query 或 relevant：%r" % it)
    return items


def search(base, query, mode, limit, timeout):
    """调一次检索，返回 (命中文章 id 列表, 耗时秒)。"""
    qs = urllib.parse.urlencode({"q": query, "mode": mode, "limit": limit})
    url = base.rstrip("/") + "/api/search?" + qs
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as f:
        data = json.loads(f.read().decode("utf-8"))
    dt = time.perf_counter() - t0
    ids = [str(h.get("article_id")) for h in (data.get("hits") or [])]
    return ids, dt


# ================================================================ 指标
#
# 三个指标各回答一个不同的问题，缺一不可：
#   Recall@k —— 「该找的找到了吗」  （漏没漏）
#   MRR      —— 「第一个对的排多前」（找得准不准）
#   nDCG@k   —— 「相关的是否都在前面」（整条排序的质量）
# 只看 Recall 会被「反正前 20 里有」麻痹；只看 MRR 则会忽略「还有 5 篇相关的排在第 30」。

def recall_at_k(ids, rel, k):
    if not rel:
        return 0.0
    got = len(set(ids[:k]) & set(rel))
    return got / len(rel)


def mrr(ids, rel):
    for i, aid in enumerate(ids, 1):
        if aid in rel:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ids, rel, k):
    """二值相关性的 nDCG@k（相关=1，不相关=0）。"""
    relset = set(rel)
    dcg = sum(1.0 / math.log2(i + 1) for i, aid in enumerate(ids[:k], 1) if aid in relset)
    ideal = min(len(relset), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal + 1))
    return dcg / idcg if idcg else 0.0


def first_rank(ids, rel):
    """第一个相关结果的排名；没召回返回 None。"""
    for i, aid in enumerate(ids, 1):
        if aid in rel:
            return i
    return None


def pct(values, p):
    """百分位数（线性插值）。样本少时只作参考，报告里会注明。"""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * p
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


# ================================================================ 报告

def render_md(meta, rows, agg):
    out = [
        "# 检索评测报告 · %s" % meta["label"],
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 时间 | %s |" % meta["time"],
        "| 检索模式 | `%s` |" % meta["mode"],
        "| 取回条数 | %d |" % meta["limit"],
        "| 评测集 | %s（%d 条） |" % (meta["queries_file"], meta["n_queries"]),
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
    ]

    for i, k in enumerate(meta["ks"]):
        out.append("| Recall@%d | %.3f |" % (k, agg["recall"][i]))
    out += [
        "| MRR | %.3f |" % agg["mrr"],
        "| nDCG@%d | %.3f |" % (meta["ks"][-1], agg["ndcg"]),
        "| 完全未召回 | %d 条 |" % agg["miss"],
        "| 延迟 P50 / P95 | %.0f ms / %.0f ms |" % (agg["p50"] * 1000, agg["p95"] * 1000),
        "",
        "## 逐条明细",
        "",
        "| id | 首个相关排名 | %s | 延迟 | 提问 |" % " | ".join("R@%d" % k for k in meta["ks"]),
        "|---|---|" + "---|" * len(meta["ks"]) + "---|---|",
    ]
    for r in rows:
        rank = "#%d" % r["rank"] if r["rank"] else "**未召回**"
        cells = " | ".join("%.2f" % v for v in r["recall"])
        out.append("| %s | %s | %s | %d ms | %s |" % (
            r["id"], rank, cells, r["ms"], r["query"]))
    out += [
        "",
        "> 延迟为**单次测量**（%d 条样本），仅作量级参考；模型为懒加载，"
        "本次已先做一次预热调用、不计入统计。" % len(rows),
        "",
    ]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="MyRag 检索评测（只读）")
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES))
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "dense", "sparse"])
    ap.add_argument("--k", default="1,3,5,10")
    ap.add_argument("--limit", type=int, default=20, help="每次检索取回多少条（<=100）")
    ap.add_argument("--label", default="baseline", help="报告标签，便于前后对比")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    ks = [int(x) for x in args.k.split(",") if x.strip()]
    ks = sorted(set(ks))
    items = load_queries(args.queries)

    # 预热：embedding 模型是懒加载的，第一次检索会慢上几秒。
    # 不预热的话，第一条 query 的延迟会污染统计。
    try:
        search(args.base, "预热", args.mode, 1, args.timeout)
    except Exception as e:
        sys.exit("后端不可达（%s）：%s\n先用 bash scripts/start.sh 拉起来" % (args.base, e))

    rows = []
    for it in items:
        ids, dt = search(args.base, it["query"], args.mode, args.limit, args.timeout)
        rel = [str(x) for x in it["relevant"]]
        rows.append({
            "id": it.get("id") or "?",
            "query": it["query"],
            "note": it.get("note"),
            "relevant": rel,
            "rank": first_rank(ids, rel),
            "recall": [recall_at_k(ids, rel, k) for k in ks],
            "mrr": mrr(ids, rel),
            "ndcg": ndcg_at_k(ids, rel, ks[-1]),
            "ms": int(dt * 1000),
        })

    agg = {
        "recall": [sum(r["recall"][i] for r in rows) / len(rows) for i in range(len(ks))],
        "mrr": sum(r["mrr"] for r in rows) / len(rows),
        "ndcg": sum(r["ndcg"] for r in rows) / len(rows),
        "miss": sum(1 for r in rows if r["rank"] is None),
        "p50": pct([r["ms"] / 1000 for r in rows], 0.50),
        "p95": pct([r["ms"] / 1000 for r in rows], 0.95),
    }
    meta = {
        "label": args.label,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": args.mode,
        "limit": args.limit,
        "ks": ks,
        "queries_file": str(pathlib.Path(args.queries).name),
        "n_queries": len(rows),
    }

    # ---- 控制台 ----
    print("\n%s  ·  mode=%s  limit=%d  %d 条\n" % (args.label, args.mode, args.limit, len(rows)))
    print("%-6s %-10s %s" % ("id", "首个相关", "提问"))
    print("-" * 74)
    for r in rows:
        print("%-6s %-10s %s" % (
            r["id"], ("#%d" % r["rank"]) if r["rank"] else "✗ 未召回", r["query"]))
    print("-" * 74)
    print("  ".join("Recall@%d=%.3f" % (k, v) for k, v in zip(ks, agg["recall"])))
    print("MRR=%.3f  nDCG@%d=%.3f  未召回=%d  P50=%.0fms P95=%.0fms" % (
        agg["mrr"], ks[-1], agg["ndcg"], agg["miss"], agg["p50"] * 1000, agg["p95"] * 1000))

    # ---- 落盘 ----
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "%s-%s" % (stamp, args.label)
    (outdir / (base + ".json")).write_text(
        json.dumps({"meta": meta, "summary": agg, "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / (base + ".md")).write_text(render_md(meta, rows, agg), encoding="utf-8")
    print("\n报告已写入：%s" % (outdir / (base + ".md")))


if __name__ == "__main__":
    main()
