#!/usr/bin/env python3
"""检索评测（PRD §8.5）—— **只读**：不写库、不改配置、不动生产代码。

用法：

    .venv/bin/python scripts/eval.py                          # 跑基线，报告存 eval/reports/
    .venv/bin/python scripts/eval.py --label rerank-on        # 给报告打标签，便于前后对比
    .venv/bin/python scripts/eval.py --mode dense             # 只跑单路，对比融合是否有增益
    .venv/bin/python scripts/eval.py --fail-on-regression eval/reports/xxx.json

四个设计决定及其理由：

1. **走 HTTP 调 /api/search，不 import server.search**
   测的必须是「使用者实际走的那条链路」（路由、参数校验、过滤、limit 截断都在里面）。
   import 内部函数会绕过这些，数字好看但不对应真实行为。

2. **用 IR 指标（Recall/MRR/nDCG）而不是直接上 RAGAS**
   它们只依赖「哪些文章算回答了这个问题」，纯本地计算、零成本、**每次改动都能跑**。
   RAGAS 的核心指标（Faithfulness / Answer Relevancy）需要 LLM 生成的答案，
   而 MyRag 根本**不生成答案**——它只负责给出对的上下文，答案由下游 Agent 的 LLM 写。
   RAGAS 里另外两个（Context Precision/Recall）本脚本已经覆盖，且不需要 LLM。
   所以对本项目引入 RAGAS = 用很贵的手段测一半不适用、一半已覆盖的指标，
   还要把私人库内容发给第三方 —— 与「数据不出本机」的定位冲突。

3. **无答案 query 单独统计，不混进 Recall**
   库里没有答案时，召回必然是 0 —— 那不是失败，**正确的期望就是「找不到」**。
   真正要测的是：它**会不会硬凑**。所以这类条目只看 top-1 分数，
   再与「有答案 query 的 top-1 分数」对比：差距越大，越有希望加一道「无结果提示」的门槛。

4. **支持回归门（--fail-on-regression）**
   把评测从「工具」变成「规矩」：改动后指标比基线退化就以非零码退出。
   否则「退化」只能靠人记得去翻历史报告。
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
    """读评测集。文件格式与构造纪律见 eval/queries.yaml 顶部。"""
    doc = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    items = doc.get("queries") or []
    if not items:
        sys.exit("评测集是空的：%s" % path)
    for it in items:
        if not it.get("query"):
            sys.exit("条目缺 query：%r" % it)
        if not it.get("relevant") and not it.get("expect_absent"):
            sys.exit("条目既没有 relevant 也没标 expect_absent：%r" % it)
    return items


def search(base, query, mode, limit, timeout):
    """调一次检索，返回 (id 列表, RRF 分列表, 精排分列表, 耗时秒)。

    两种分数**必须分开记录**：
      · RRF 分  —— 排名融合值，只反映「两路排在第几」，**不反映相关度**；
      · 精排分  —— 0–1 的绝对相关度，未开精排时为 None。
    实测二者在「库里没答案」的判断上表现完全不同（见报告里的对照表）。
    """
    qs = urllib.parse.urlencode({"q": query, "mode": mode, "limit": limit})
    url = base.rstrip("/") + "/api/search?" + qs
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as f:
        data = json.loads(f.read().decode("utf-8"))
    dt = time.perf_counter() - t0
    hits = data.get("hits") or []
    return ([str(h.get("article_id")) for h in hits],
            [float(h.get("score") or 0.0) for h in hits],
            [h.get("rerank_score") for h in hits],
            dt)


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
    return len(set(ids[:k]) & set(rel)) / len(rel)


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
    ans = [r for r in rows if not r["expect_absent"]]
    ab = [r for r in rows if r["expect_absent"]]

    out = [
        "# 检索评测报告 · %s" % meta["label"],
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 时间 | %s |" % meta["time"],
        "| 检索模式 | `%s` |" % meta["mode"],
        "| 取回条数 | %d |" % meta["limit"],
        "| 评测集 | %s（有答案 %d 条 / 无答案 %d 条） |"
        % (meta["queries_file"], len(ans), len(ab)),
        "",
        "## 汇总（仅计有答案的 %d 条）" % len(ans),
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
    ]

    if ab:
        out += [
            "## 无答案探针（%d 条）——「会不会硬凑」" % len(ab),
            "",
            "这组不计入上面的分数：库里没有答案时，召回必然是 0，**那是正确结果**。",
            "这里要看的是它**会不会硬凑一堆不相干的** —— 以及能否与有答案的情形区分开。",
            "",
            "| id | RRF top-1 | 精排 top-1 | top-1 命中 | 提问 |",
            "|---|---|---|---|---|",
        ]
        for r in ab:
            rr = "%.4f" % r["rr_top1"] if r["rr_top1"] is not None else "—"
            out.append("| %s | %.4f | %s | %s | %s |" % (
                r["id"], r["top1"], rr,
                (r["ids"][0] if r["ids"] else "—"), r["query"]))
        out += [
            "",
            "### 两种分数的对照 —— 哪个才能用来判断「有没有答案」",
            "",
            "| 分数 | 有答案（%d 条） | 无答案（%d 条） | 差值 | 可信吗 |" % (len(ans), len(ab)),
            "|---|---|---|---|---|",
            "| RRF `score` | %.4f | %.4f | %.4f | %s |"
            % (agg["answer_top1"], agg["absent_top1"], agg["top1_gap"],
               "**不可信**" if agg["top1_gap"] < 0.01 else "可参考"),
            "| 精排 `rerank_score` | %.4f | %.4f | %.4f | %s |"
            % (agg["answer_rr"], agg["absent_rr"], agg["rr_gap"],
               "（未开精排）" if not agg["answer_rr"] and not agg["absent_rr"]
               else ("**可参考**" if agg["rr_gap"] > 0.1 else "**不可信**")),
            "",
            "> **怎么读**：光看均值差不够，必须看**分布是否重叠** —— 只要「有答案的最低分」",
            "> 低于「无答案的最高分」，就存在分不开的区间，用阈值做「没找到」提示就会误伤。",
            "> RRF 分已实测重叠（有答案 0.0152 < 无答案 0.0275），**不能用**。",
            "",
        ]

    out += [
        "## 逐条明细（有答案）",
        "",
        "| id | 首个相关排名 | %s | 延迟 | 提问 |" % " | ".join("R@%d" % k for k in meta["ks"]),
        "|---|---|" + "---|" * len(meta["ks"]) + "---|---|",
    ]
    for r in ans:
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


# ================================================================ 主流程

def run(args):
    ks = sorted(set(int(x) for x in args.k.split(",") if x.strip()))
    items = load_queries(args.queries)

    # 预热：embedding 与 reranker 都是懒加载，第一次调用会慢上几秒。
    # 不预热的话，第一条 query 的延迟会污染统计。
    try:
        search(args.base, "预热", args.mode, 1, args.timeout)
    except Exception as e:
        sys.exit("后端不可达（%s）：%s\n先用 bash scripts/start.sh 拉起来" % (args.base, e))

    rows = []
    for it in items:
        ids, scores, rrs, dt = search(
            args.base, it["query"], args.mode, args.limit, args.timeout)
        rel = [str(x) for x in (it.get("relevant") or [])]
        absent = bool(it.get("expect_absent"))
        rows.append({
            "id": it.get("id") or "?",
            "query": it["query"],
            "note": it.get("note"),
            "relevant": rel,
            "expect_absent": absent,
            "ids": ids,
            "top1": scores[0] if scores else 0.0,
            "rr_top1": rrs[0] if (rrs and rrs[0] is not None) else None,
            "rank": None if absent else first_rank(ids, rel),
            "recall": [recall_at_k(ids, rel, k) for k in ks],
            "mrr": mrr(ids, rel),
            "ndcg": ndcg_at_k(ids, rel, ks[-1]),
            "ms": int(dt * 1000),
        })

    ans = [r for r in rows if not r["expect_absent"]]
    ab = [r for r in rows if r["expect_absent"]]

    def avg(vals):
        return sum(vals) / len(vals) if vals else 0.0

    answer_top1 = avg([r["top1"] for r in ans])
    absent_top1 = avg([r["top1"] for r in ab])
    ans_rr = [r["rr_top1"] for r in ans if r["rr_top1"] is not None]
    ab_rr = [r["rr_top1"] for r in ab if r["rr_top1"] is not None]
    agg = {
        "recall": [avg([r["recall"][i] for r in ans]) for i in range(len(ks))],
        "mrr": avg([r["mrr"] for r in ans]),
        "ndcg": avg([r["ndcg"] for r in ans]),
        "miss": sum(1 for r in ans if r["rank"] is None),
        "p50": pct([r["ms"] / 1000 for r in rows], 0.50),
        "p95": pct([r["ms"] / 1000 for r in rows], 0.95),
        # 两种分数的对照 —— 判断「能不能做没找到提示」就看这一对差值的对比
        "answer_top1": answer_top1,
        "absent_top1": absent_top1,
        "top1_gap": answer_top1 - absent_top1,
        "answer_rr": avg(ans_rr),          # 精排分（0–1 绝对相关度）
        "absent_rr": avg(ab_rr),
        "rr_gap": avg(ans_rr) - avg(ab_rr),
    }
    meta = {
        "label": args.label,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": args.mode,
        "limit": args.limit,
        "ks": ks,
        "queries_file": str(pathlib.Path(args.queries).name),
        "n_queries": len(rows),
        "n_answer": len(ans),
        "n_absent": len(ab),
    }
    return rows, agg, meta


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
    ap.add_argument("--fail-on-regression", metavar="BASELINE_JSON",
                    help="与指定基线对比，指标退化则退出码非零（回归门）")
    args = ap.parse_args()

    ks = sorted(set(int(x) for x in args.k.split(",") if x.strip()))
    rows, agg, meta = run(args)
    ans = [r for r in rows if not r["expect_absent"]]
    ab = [r for r in rows if r["expect_absent"]]

    # ---- 控制台 ----
    print("\n%s  ·  mode=%s  limit=%d  有答案 %d 条 / 无答案 %d 条\n"
          % (args.label, args.mode, args.limit, len(ans), len(ab)))
    print("%-6s %-10s %s" % ("id", "首个相关", "提问"))
    print("-" * 74)
    for r in ans:
        print("%-6s %-10s %s" % (
            r["id"], ("#%d" % r["rank"]) if r["rank"] else "✗ 未召回", r["query"]))
    print("-" * 74)
    print("  ".join("Recall@%d=%.3f" % (k, v) for k, v in zip(ks, agg["recall"])))
    print("MRR=%.3f  nDCG@%d=%.3f  未召回=%d  P50=%.0fms P95=%.0fms" % (
        agg["mrr"], ks[-1], agg["ndcg"], agg["miss"], agg["p50"] * 1000, agg["p95"] * 1000))

    if ab:
        print("\n无答案探针（不计入上面的分数，看会不会硬凑）")
        print("-" * 74)
        for r in ab:
            rr = "%.4f" % r["rr_top1"] if r["rr_top1"] is not None else "  —   "
            print("%-6s RRF=%.4f  精排=%s  %s" % (r["id"], r["top1"], rr, r["query"]))
        print("-" * 74)
        print("RRF  分：有答案 %.4f  vs  无答案 %.4f   →  差值 %.4f"
              % (agg["answer_top1"], agg["absent_top1"], agg["top1_gap"]))
        print("精排 分：有答案 %.4f  vs  无答案 %.4f   →  差值 %.4f"
              % (agg["answer_rr"], agg["absent_rr"], agg["rr_gap"]))
        print("（差值大且分布不重叠，才谈得上做「没找到」提示）")

    # ---- 落盘 ----
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "%s-%s" % (stamp, args.label)
    payload = {"meta": meta, "summary": agg, "rows": rows}
    (outdir / (base + ".json")).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / (base + ".md")).write_text(render_md(meta, rows, agg), encoding="utf-8")
    print("\n报告已写入：%s" % (outdir / (base + ".md")))

    # ---- 回归门 ----
    if args.fail_on_regression:
        return check_regression(args.fail_on_regression, agg)
    return 0


def check_regression(baseline_path, agg):
    """与基线对比，退化则非零退出。

    阈值取 0.02：小于这个幅度的波动更可能是 LLM/浮点噪声，不该拦人。
    只比「越大越好」的指标；延迟单独看，且放宽（P95 涨 50% 才报警）。
    """
    bp = pathlib.Path(baseline_path)
    if not bp.exists():
        sys.exit("基线文件不存在：%s" % bp)
    b = json.loads(bp.read_text(encoding="utf-8"))["summary"]
    tol = 0.02
    bad = []

    names = ["mrr", "ndcg"]
    for n in names:
        if b.get(n, 0) - agg.get(n, 0) > tol:
            bad.append("%s %.3f → %.3f（-%0.3f）" % (n, b[n], agg[n], b[n] - agg[n]))
    for i, v in enumerate(agg["recall"]):
        o = (b.get("recall") or [])[i] if i < len(b.get("recall") or []) else None
        if o is not None and o - v > tol:
            bad.append("Recall[%d] %.3f → %.3f（-%0.3f）" % (i, o, v, o - v))
    if agg["p95"] > b.get("p95", 0) * 1.5 and agg["p95"] > 0.5:
        bad.append("P95 延迟 %.2fs → %.2fs（超 1.5 倍）" % (b.get("p95", 0), agg["p95"]))

    print("\n" + "=" * 74)
    if bad:
        print("✗ 相对基线有退化：")
        for x in bad:
            print("   -", x)
        return 1
    print("✓ 相对基线无退化（容差 %.2f）" % tol)
    return 0


if __name__ == "__main__":
    sys.exit(main())
