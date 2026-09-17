#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建分段与索引：chunks / chunk_vecs / chunk_sparse（不动原文与配图）。

什么时候用：切段规则或 embedding 模型变了，已有文章的分段还是按旧规则算的。
本脚本按新规则重算，正文从 data/raw/<id>.md 读回 —— 原文与配图在 PRD §4 里
是「底本，只写不改」，所以这里也只读；向量与词权重全部可由原文重建。

用法（在项目根下执行）：
    .venv/bin/python scripts/reindex.py                    # 全部文章
    .venv/bin/python scripts/reindex.py T-20260101-01 ...  # 指定若干篇
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import config, db, ingest  # noqa: E402


def main(argv) -> int:
    want = [a for a in argv if not a.startswith("-")]
    config.ensure_dirs()
    db.init_db()

    done, skipped = 0, 0
    with db.tx() as conn:
        if want:
            rows = [{"id": i} for i in want]
        else:
            rows = conn.execute("SELECT id FROM articles ORDER BY id").fetchall()

        for r in rows:
            aid = r["id"]
            art = conn.execute("SELECT category, raw_path FROM articles WHERE id = ?",
                               (aid,)).fetchone()
            if not art:
                print("  跳过 %s：库中无此文章" % aid)
                skipped += 1
                continue
            p = config.DATA_DIR / (art["raw_path"] or "")
            if not art["raw_path"] or not p.exists():
                print("  跳过 %s：原文缺失（%s）" % (aid, art["raw_path"]))
                skipped += 1
                continue

            # retrieval=fulltext 的分类本来就不建索引，重建时要跳过，
            # 否则会把它们建出分段来，跟分类配置自相矛盾
            cat = db.category_map(conn).get(art["category"]) or {}
            if (cat.get("retrieval") or "rag") != "rag":
                print("  跳过 %s：分类「%s」是直接全文，不建索引"
                      % (aid, cat.get("name") or art["category"]))
                skipped += 1
                continue

            md = p.read_text(encoding="utf-8")
            idx = ingest.chunk_and_index(conn, aid, md)
            ingest.upsert_meta_chunk(conn, aid)
            db.log_ingest(conn, None, aid, "reindex",
                          {"chunks": idx["chunks"], "tokens": idx["tokens"],
                           "warnings": idx["warnings"]})
            done += 1
            note = ("；".join(idx["warnings"])) if idx["warnings"] else ""
            print("  %s → %d 段 / %d token%s" % (aid, idx["chunks"], idx["tokens"],
                                                 ("；" + note) if note else ""))

    print("重建完成：成功 %d 篇，跳过 %d 篇" % (done, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
