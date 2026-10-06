#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quality_check.py — 全库内容质检：捞出「入库了、但内容明显不对」的条目。

**为什么要有它**（2026-10-07 两次真实事故的教训）：
两次都不是代码崩，而是**静默接受** ——
  · 9 篇走渲染抓取的文章配图全丢（payload `images: []` 照样入库，正文里连图引用都没有）；
  · 2 篇从落地区残留「抢救」回来的条目没交摘要/标签（后端 `warnings` 恒为空数组，一句提醒都没有）。
系统自己不会喊疼，所以需要有人定期把「不对劲」的条目捞出来看。

检查项（判据 → 为什么可疑）：

| kind | 判据 | 为什么可疑 |
|---|---|---|
| `empty_body`         | 正文去标签后不足 `--min-body-chars`（默认 50） | 空壳入库：点开是空白，检索也搜不到 |
| `missing_annotation` | 摘要为空 或 标签为空 | 入库时批注漏交（2026-10-06 抢救那批就漏了） |
| `broken_image`       | 正文引用了 `media/<id>/x` 但文件不在 | 破图，点开是空白 |
| `no_chunks`          | 分类走 RAG 却没有 chunks | 这篇等于没进检索 |
| `suspect_source`     | `source` 为空 | 按 SKILL.md 约定 = 抓取降级，本该 `review_flag=1` |
| `maybe_lost_images`  | 来源降级（"微信公众平台"）且 0 张图 | 渲染路径曾系统性丢图（2026-10-07 修的就是它） |
| `unnamed_source`     | `source` == "微信公众平台" | 渲染路径拿不到真实公众号名，来源不可信 |

用法：

    .venv/bin/python scripts/quality_check.py
    .venv/bin/python scripts/quality_check.py --json
    .venv/bin/python scripts/quality_check.py --brief              # 一行摘要（status.sh 用）
    .venv/bin/python scripts/quality_check.py --min-body-chars 100

**只报告，不修**（2026-10-07 用户拍板）：自动重抓、自动补批注都有风险，
发现的问题交给人判断怎么处理。

退出码：有「高优先级」问题（empty_body / missing_annotation / broken_image / no_chunks）时 1。
"""

import argparse
import json
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "wxk.db"

MIN_BODY_CHARS = 50          # 正文短于此视为空壳（正常文章实测中位数 3524 字）
SHOW_PER_KIND = 8            # 报告里每类最多列几条（其余只给计数）

# 级别的先后顺序就是报告里的顺序：高 → 中 → 低
ORDER = [
    ("empty_body",         "高", "空壳入库",   "正文去标签后不足阈值 —— 点开空白、检索搜不到"),
    ("missing_annotation", "高", "批注缺失",   "摘要或标签为空（入库时漏交批注）"),
    ("broken_image",       "高", "破图",       "正文引用的配图文件不存在"),
    ("no_chunks",          "高", "没进检索",   "分类走 RAG 却没有切段"),
    ("suspect_source",     "中", "来源为空",   "按约定「来源为空 = 抓取降级」，本该进待复核"),
    ("maybe_lost_images",  "中", "疑丢图",     "来源降级 + 0 张图（渲染路径曾系统性丢图）"),
    ("unnamed_source",     "低", "来源不可信", "来源写成「微信公众平台」，不是真实公众号名"),
]
HIGH_KINDS = {k for k, lv, _, _ in ORDER if lv == "高"}
MID_KINDS = {k for k, lv, _, _ in ORDER if lv == "中"}


def _tags(value) -> list:
    try:
        return [t for t in json.loads(value or "[]") if str(t).strip()]
    except Exception:
        return []


def _body_chars(text: str) -> int:
    """正文实质字数：去掉图片引用与标题行，**保留图内文字（blockquote）**。

    保留 blockquote 是有意的 —— 图片合辑类文章的正文本来就只有文案与图内文字，
    把它们一起去掉会把正常文章误判成空壳。
    """
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")
    text = re.sub(r"^#.*$", "", text, flags=re.M)
    return len(re.sub(r"\s+", "", text))


def scan(min_body_chars: int = MIN_BODY_CHARS) -> list:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = []
    try:
        cats = {r["key"]: (r["retrieval"] or "rag")
                for r in conn.execute("SELECT key, retrieval FROM categories")}
        chunked = {r[0] for r in conn.execute("SELECT DISTINCT article_id FROM chunks")}

        for r in conn.execute(
                "SELECT id, category, title, source, summary, tags, attachments, raw_path"
                "  FROM articles"):
            aid = r["id"]
            raw = DATA / (r["raw_path"] or "")
            text = raw.read_text(encoding="utf-8", errors="ignore") if raw.is_file() else ""
            src = (r["source"] or "").strip()

            def mark(kind, detail):
                rows.append({"id": aid, "kind": kind, "title": r["title"],
                             "category": r["category"], "detail": detail})

            n = _body_chars(text)
            if n < min_body_chars:
                mark("empty_body", "正文 %d 字" % n)

            has_sum = bool((r["summary"] or "").strip())
            has_tags = bool(_tags(r["tags"]))
            if not has_sum or not has_tags:
                mark("missing_annotation",
                     "摘要%s / 标签%s" % ("有" if has_sum else "空", "有" if has_tags else "空"))

            broken = [f for oid, f in re.findall(r"media/([^/)\s]+)/([^/)\s]+)", text)
                      if not (DATA / "media" / oid / f).is_file()]
            if broken:
                mark("broken_image", "%d 个引用指不到文件（如 %s）" % (len(broken), broken[0]))

            if cats.get(r["category"], "rag") == "rag" and aid not in chunked:
                mark("no_chunks", "分类 %s 走 RAG，但库里没有它的切段" % r["category"])

            if not src:
                mark("suspect_source", "来源为空")
            elif src == "微信公众平台":
                mark("unnamed_source", "来源不是真实公众号名")
                if not _tags(r["attachments"]):
                    mark("maybe_lost_images", "来源降级且 0 张图")
    finally:
        conn.close()
    return rows


def _bucket(rows):
    out = {}
    for r in rows:
        out.setdefault(r["kind"], []).append(r)
    return out


def report(rows: list, brief: bool = False, min_body_chars: int = MIN_BODY_CHARS) -> int:
    buckets = _bucket(rows)
    high = sum(1 for r in rows if r["kind"] in HIGH_KINDS)
    mid = sum(1 for r in rows if r["kind"] in MID_KINDS)
    low = len(rows) - high - mid

    if brief:
        if not rows:
            print("内容质检：无异常")
        else:
            print("内容质检：高 %d ｜ 中 %d ｜ 观察 %d（明细：python3 scripts/quality_check.py）"
                  % (high, mid, low))
        return 1 if high else 0

    if not rows:
        print("内容质检：无异常（%d 项检查全过）" % len(ORDER))
        return 0

    print("内容质检：发现 %d 条可疑（高 %d ｜ 中 %d ｜ 观察 %d）"
          % (len(rows), high, mid, low))
    print("阈值：正文短于 %d 字视为空壳（正常文章中位数约 3500 字）" % min_body_chars)

    for kind, level, label, hint in ORDER:
        items = buckets.get(kind, [])
        if not items:
            continue
        print("\n【%s】%s（%s）：%d 条 —— %s" % (level, label, kind, len(items), hint))
        for it in items[:SHOW_PER_KIND]:
            print("   %-16s %-8s %s  ← %s"
                  % (it["id"], it["category"], (it["title"] or "")[:26], it["detail"]))
        if len(items) > SHOW_PER_KIND:
            print("   …还有 %d 条（用 --json 看全量）" % (len(items) - SHOW_PER_KIND))

    print("\n本脚本**只报告不修**（用户 2026-10-07 定）：处理方式人来判断——"
          "空壳/破图一般要重抓，批注缺失可补 PATCH，来源不可信多为渲染路径降级。")
    return 1 if high else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="巡检 MyRag 全库内容质量（只报告，不修改）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供监控消费）")
    ap.add_argument("--brief", action="store_true", help="只输出一行摘要（status.sh 用）")
    ap.add_argument("--min-body-chars", type=int, default=MIN_BODY_CHARS,
                    help="正文短于此字数视为空壳（默认 %d）" % MIN_BODY_CHARS)
    args = ap.parse_args()

    if not DB.is_file():
        sys.exit("找不到库：%s" % DB)

    rows = scan(args.min_body_chars)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 1 if any(r["kind"] in HIGH_KINDS for r in rows) else 0
    return report(rows, brief=args.brief, min_body_chars=args.min_body_chars)


if __name__ == "__main__":
    sys.exit(main())
