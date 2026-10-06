#!/usr/bin/env python3
"""stage_check.py — 巡检 data/_stage 残留，分类「能不能安全删」。

为什么需要它：`ingest.py::_drop_stage` 是**静默失败**的（rmtree 撞上 shim 守卫时
只 `log.warning`，不报错、不重试），而"抓完没提交 ingest"这条路径压根不经过清理。
于是残留会悄悄攒起来 —— 2026-10-01 清过 328 个，三天后又是 50 个。
本脚本的目的不是"清一次"，是**让残留当天就被看见**。

只依赖标准库，系统 `python3` 即可跑（**不需要 .venv**）：

    python3 scripts/stage_check.py           # 报告（默认，只读）
    python3 scripts/stage_check.py --json    # 机器可读，供 status.sh / 监控消费
    python3 scripts/stage_check.py --clean   # 删掉三类"可安全清"的（需交互确认）

分类（四类，注意 `need_review` **绝不自动删**）：

| 类别 | 判据 | 含义 |
|---|---|---|
| `safe_dup` | 库里有这篇，本 token 无 ingest 记录 | 重复抓取遗留，正文在库里已有一份 |
| `safe_ingested` | 库里有这篇，且有 ingest 记录 | 已入库但目录没删掉（`_drop_stage` 静默失败） |
| `safe_nopayload` | 目录里没有 payload.json | 空壳 / 抓取失败残留 |
| `need_review` | 库里查不到，但目录有完整内容 | **可能只有这一份副本，要人判断** |

退出码：存在 `need_review` 时 1，否则 0。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import config  # noqa: E402  单一事实源：STAGE_DIR / DB_PATH 都从它来

SAFE_KINDS = ("safe_dup", "safe_ingested", "safe_nopayload")


def _canon_of(sdir: Path) -> str | None:
    """从 payload.json 取 url_canon；取不到返回 None。"""
    pj = sdir / "payload.json"
    if not pj.is_file():
        return None
    try:
        return (json.loads(pj.read_text(encoding="utf-8")) or {}).get("url_canon") or None
    except (json.JSONDecodeError, OSError, AttributeError):
        return None


def _title_of(sdir: Path) -> str:
    pj = sdir / "payload.json"
    try:
        return str((json.loads(pj.read_text(encoding="utf-8")) or {}).get("title") or "")[:40]
    except (json.JSONDecodeError, OSError, AttributeError):
        return ""


def scan() -> list[dict]:
    """扫 STAGE_DIR，返回每个残留目录的分类结果。"""
    if not config.STAGE_DIR.is_dir():
        return []

    # 不用 mode=ro：WAL 库在只读连接下拿不到 -shm，会报 "unable to open database file"。
    # 本脚本只跑 SELECT，连接本身不会写库。
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        rows = []
        for sdir in sorted(p for p in config.STAGE_DIR.iterdir() if p.is_dir()):
            token = sdir.name
            canon = _canon_of(sdir)

            if canon is None:
                kind, article_id = "safe_nopayload", None
            else:
                hit = conn.execute(
                    "SELECT id FROM articles WHERE url_canon = ? LIMIT 1", (canon,)
                ).fetchone()
                logged = conn.execute(
                    "SELECT 1 FROM ingest_log "
                    "WHERE json_extract(detail, '$.stage_token') = ? LIMIT 1",
                    (token,),
                ).fetchone()
                if hit is None:
                    kind, article_id = "need_review", None
                else:
                    kind = "safe_ingested" if logged else "safe_dup"
                    article_id = hit[0]

            rows.append({
                "token": token,
                "kind": kind,
                "article_id": article_id,
                "title": _title_of(sdir),
                "bytes": sum(f.stat().st_size for f in sdir.rglob("*") if f.is_file()),
            })
        return rows
    finally:
        conn.close()


def _fmt_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def report(rows: list[dict], brief: bool = False) -> int:
    """打印人类可读报告，返回进程退出码。"""
    if not rows:
        print("stage 残留：无（干净）")
        return 0

    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r["kind"], []).append(r)
    total = sum(r["bytes"] for r in rows)
    has_review = bool(buckets.get("need_review"))

    if brief:
        n = lambda k: len(buckets.get(k, []))  # noqa: E731
        print(f"stage 残留：{len(rows)} 个 / {_fmt_size(total)}"
              f"（已入库未删 {n('safe_ingested')}｜重复抓取 {n('safe_dup')}"
              f"｜空壳 {n('safe_nopayload')}｜待人工判断 {n('need_review')}）")
        return 1 if has_review else 0

    print(f"stage 残留：{len(rows)} 个目录 / {_fmt_size(total)}")

    order = [
        ("safe_ingested", "已入库但没删掉", "_drop_stage 静默失败，查 server.log 的 'stage 清理失败'"),
        ("safe_dup", "重复抓取遗留", "库里已有同一篇，正文是冗余的"),
        ("safe_nopayload", "空壳 / 抓取失败", "没有 payload.json"),
        ("need_review", "库里查不到", "★ 可能只有这一份副本，要人判断，不许自动删"),
    ]
    for kind, label, hint in order:
        items = buckets.get(kind, [])
        if not items:
            continue
        print(f"  ├─ {label}（{kind}）：{len(items)} 个 —— {hint}")
        for it in items:
            tag = f"→ {it['article_id']}" if it["article_id"] else "→ 未入库"
            title = f"  《{it['title']}》" if it["title"] else ""
            print(f"     {it['token']}  {tag}  {_fmt_size(it['bytes'])}  {title}")

    safe = [r for r in rows if r["kind"] in SAFE_KINDS]
    if safe:
        print(f"\n清理：python3 scripts/stage_check.py --clean   # 只删上面三类安全项，"
              f"{len(safe)} 个 / {_fmt_size(sum(r['bytes'] for r in safe))}")
    if has_review:
        print("注意：need_review 的目录**不会被 --clean 删掉**，请人工判断后自行处理。")
    return 1 if has_review else 0


def clean(rows: list[dict]) -> int:
    """删掉三类安全项。need_review 一律跳过。"""
    safe = [r for r in rows if r["kind"] in SAFE_KINDS]
    held = [r for r in rows if r["kind"] == "need_review"]
    if not safe:
        print("没有可安全清理的目录。")
        return 0

    print(f"将删除 {len(safe)} 个目录 / {_fmt_size(sum(r['bytes'] for r in safe))}：")
    for r in safe:
        print(f"  {r['token']}  [{r['kind']}]")
    if held:
        print(f"\n保留 {len(held)} 个 need_review 目录（需人工判断）：")
        for r in held:
            print(f"  {r['token']}  {r['title']}")

    if input("\n确认删除上面这些？输入 yes 继续：").strip() != "yes":
        print("已取消。")
        return 0

    freed = 0
    for r in safe:
        d = config.STAGE_DIR / r["token"]
        shutil.rmtree(d, ignore_errors=True)
        freed += r["bytes"]
    print(f"已删除 {len(safe)} 个目录，释放 {_fmt_size(freed)}。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="巡检 MyRag data/_stage 残留")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供监控消费）")
    ap.add_argument("--brief", action="store_true", help="只输出一行摘要（status.sh 用）")
    ap.add_argument("--clean", action="store_true",
                    help="删除三类安全项（需交互确认；need_review 永不删除）")
    args = ap.parse_args()

    rows = scan()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 1 if any(r["kind"] == "need_review" for r in rows) else 0
    if args.clean:
        return clean(rows)
    return report(rows, brief=args.brief)


if __name__ == "__main__":
    sys.exit(main())
