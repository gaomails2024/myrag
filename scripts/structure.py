#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识结构：语义目录 + 文章挂载（PRD §5.7）。

三步，每步都能单独重跑，都幂等：

    .venv/bin/python scripts/structure.py mount          # 挂载 + 生成目录预览页
    # 看完预览页、确认目录没问题之后：
    .venv/bin/python scripts/structure.py apply          # 写入 themes / article_themes
    .venv/bin/python scripts/structure.py list          # 看当前生效的结构
    .venv/bin/python scripts/structure.py profiles      # 重算文章画像向量（换模型或改聚合方式时才需要）

**目录（`data/structure/catalog.json`）是人工/Agent 拟的，不是聚类出来的。**
这一条是整个功能的关键：第一版试过让算法把库切成 20 簇，失败的原因是形态错了——
划分靠相似度切边界，而主题是连续谱，边界必然模糊（实测 20 个话题里有 8 个在讲同一件事，
共 473 篇 = 63%）。改成目录后，上下级靠语义从属（领域 > 板块 > 话题），不需要切边界。

落库（apply）会**整表重建** themes / article_themes（都是派生数据），
`articles` 一行不动，并且执行前自动备份数据库到 data/_backup/。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import config, db, structure  # noqa: E402

STRUCT_DIR = config.DATA_DIR / "structure"
CATALOG = STRUCT_DIR / "catalog.json"
TREE = STRUCT_DIR / "tree.json"


def _load_catalog() -> dict:
    if not CATALOG.is_file():
        raise SystemExit("找不到 %s —— 目录是这个功能的源头，缺了它没法挂载" % CATALOG)
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _load_tree() -> dict:
    if not TREE.is_file():
        raise SystemExit("找不到 %s\n先跑：.venv/bin/python scripts/structure.py mount" % TREE)
    return json.loads(TREE.read_text(encoding="utf-8"))


def _slim(payload: dict) -> dict:
    """注入页面用的精简副本：去掉内部字段与成员 id 列表（页面只看代表篇）。"""
    out = {k: v for k, v in payload.items() if k != "nodes"}
    out["nodes"] = [{k: v for k, v in n.items() if k != "members"}
                    for n in payload["nodes"]]
    return out


def render(payload: dict, out_path: Path) -> None:
    """注入 web/catalog-preview.html 模板，产出可点开看的单文件目录页。

    为什么是独立页面而不是工作台里的一个页签：这一步的产物还没进库、随时可丢弃，
    等你确认目录本身站得住，再谈集成进工作台。
    """
    tpl = (config.WEB_DIR / "catalog-preview.html").read_text(encoding="utf-8")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        tpl.replace("/*__DATA__*/null", json.dumps({"payload": _slim(payload)},
                                                   ensure_ascii=False)),
        encoding="utf-8")


# ---------------------------------------------------------------- 子命令

def cmd_profiles(argv) -> int:
    force = "--force" in argv
    config.ensure_dirs()
    db.init_db()
    print("算文章画像向量（%s）" % ("全量重算" if force else "增量，只补缺失的"))
    with db.conn_ctx() as conn:
        st = structure.build_profiles(conn, force=force)
    print("完成：%s" % json.dumps(st, ensure_ascii=False))
    return 0


def cmd_mount(argv) -> int:
    del argv
    config.ensure_dirs()
    db.init_db()
    catalog = _load_catalog()
    print("挂载中（目录：%d 个领域）…" % len(catalog.get("domains") or []))
    with db.conn_ctx() as conn:
        payload = structure.mount_catalog(conn, catalog)
    STRUCT_DIR.mkdir(parents=True, exist_ok=True)
    TREE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out = STRUCT_DIR / "catalog.html"
    render(payload, out)

    topics = [n for n in payload["nodes"] if n["kind"] == "topic"]
    counts = sorted(n["count"] for n in topics)
    print("\n挂载结果：%s" % json.dumps(payload["mount"], ensure_ascii=False))
    print("话题 %d 个 · 篇数最少 %d / 中位 %d / 最多 %d · 空话题 %d 个"
          % (len(topics), counts[0], counts[len(counts) // 2], counts[-1],
             sum(1 for c in counts if c == 0)))
    if payload["overlaps"]:
        print("\n话题重叠（成员交集偏大，目录这几处写重了）：")
        for o in payload["overlaps"]:
            print("  %.2f  %s ↔ %s（共用 %d 篇）"
                  % (o["jaccard"], o["a"], o["b"], o["shared"]))
    else:
        print("\n话题重叠检查：无（没有两处目录在抢同一批文章）")

    print("\n各话题第 1 篇代表（判断目录准不准，看这一列最快）：")
    for n in sorted(topics, key=lambda x: x["key"]):
        a = n["top_articles"][0] if n["top_articles"] else {}
        print("  %-22s %3d 篇 │ %s"
              % (n["name"], n["count"], (a.get("title") or "（无）")[:46]))

    print("\n已写：\n  %s\n  %s" % (TREE, out))
    print("\n看完目录没问题，再跑：scripts/structure.py apply")
    return 0


def cmd_apply(argv) -> int:
    del argv
    payload = _load_tree()
    print("把目录与挂载结果写入 themes / article_themes")
    print("（这三张表整表重建，articles 不动；执行前会自动备份数据库）")
    # 用 conn_ctx 而不是 tx：写事务由 apply_catalog 内部自己开，
    # 这里若也开一个，空事务不持锁但会让人误以为写入走的是这个连接。
    with db.conn_ctx() as conn:
        st = structure.apply_catalog(conn, payload)
    print("\n落库完成：")
    print("  领域 %d · 板块 %d · 话题 %d"
          % (st["domains"], st["boards"], st["topics"]))
    print("  文章归属：主话题 %d 条 · 次话题（待你确认）%d 条"
          % (st["primary"], st["secondary"]))
    print("  未挂上任何话题 %d 篇 · 落库时跳过的文章 %d 篇"
          % (payload["mount"]["unmounted"], st["skipped_articles"]))
    print("  备份：%s" % st["backup"])
    print("\n跑 `scripts/structure.py list` 看结果。")
    return 0


def cmd_tags(argv) -> int:
    """导出标签词频与成员，生成词云页（web/tag-cloud.html）。

    词云用 canvas 自己画、不引外部库：词云要在离线环境可用（项目没有 CDN 依赖）。
    顺带把「标签碎片化」这件事量化出来打在页面上 —— 2148 个标签 / 752 篇
    意味着平均每个标签只覆盖 0.35 篇，词云上一眼就能看出同义词并排的样子。
    """
    del argv
    config.ensure_dirs()
    db.init_db()
    with db.conn_ctx() as conn:
        rows = conn.execute(
            "SELECT id, title, category, published_at, collected_at, tags "
            "FROM articles WHERE status <> 'failed' ORDER BY id").fetchall()
        cats = {c["key"]: c["name"] for c in db.get_categories(conn)}
    arts = {}
    counter, members = {}, {}
    for r in rows:
        arts[r["id"]] = {
            "t": r["title"] or "", "c": r["category"],
            "d": (r["published_at"] or r["collected_at"] or "")[:10],
        }
        for t in db.jloads(r["tags"], []):
            if not isinstance(t, str) or not t.strip():
                continue
            tag = t.strip()
            counter[tag] = counter.get(tag, 0) + 1
            members.setdefault(tag, []).append(r["id"])
    tags = []
    for tag, n in sorted(counter.items(), key=lambda x: (-x[1], x[0])):
        dist = {}
        for aid in members[tag]:
            k = arts[aid]["c"]
            dist[k] = dist.get(k, 0) + 1
        tags.append({"t": tag, "n": n, "c": dist, "ids": sorted(members[tag])})
    payload = {"total_tags": len(tags), "total_articles": len(arts),
               "tags_for_90": _tags_for_90(tags),
               "tags": tags, "articles": arts, "categories": cats}
    STRUCT_DIR.mkdir(parents=True, exist_ok=True)
    tpl = (config.WEB_DIR / "tag-cloud.html").read_text(encoding="utf-8")
    out = STRUCT_DIR / "tags.html"
    out.write_text(tpl.replace("/*__DATA__*/null",
                               json.dumps(payload, ensure_ascii=False)),
                   encoding="utf-8")
    once = sum(1 for t in tags if t["n"] == 1)
    print("标签总数 %d · 文章 %d 篇" % (len(tags), len(arts)))
    print("  只出现 1 次的标签：%d 个（占 %.0f%%）—— 词云上不画，否则全是碎字"
          % (once, 100.0 * once / max(1, len(tags))))
    print("  覆盖 90%% 文章所需的前 %d 个标签（占标签总数的 %.0f%%）"
          % (_tags_for_90(tags),
             100.0 * _tags_for_90(tags) / max(1, len(tags))))
    print("\n已写：\n  %s" % out)
    return 0


def _tags_for_90(tags: list) -> int:
    """前多少个标签能覆盖 90% 的文章 —— 用来看标签体系的实际有效规模。"""
    total = sum(t["n"] for t in tags)
    acc = 0
    for i, t in enumerate(tags, 1):
        acc += t["n"]
        if acc >= total * 0.9:
            return i
    return len(tags)


def cmd_list(argv) -> int:
    del argv
    with db.conn_ctx() as conn:
        meta = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM structure_meta")}
        themes = conn.execute(
            "SELECT id, name, level, article_count FROM themes "
            "ORDER BY level, article_count DESC").fetchall()
        pend = conn.execute(
            "SELECT COUNT(*) c FROM article_themes WHERE status='suggested'"
        ).fetchone()["c"]
    if not meta:
        print("还没有生效的结构。先跑 mount -> 确认目录 -> apply。")
        return 0
    print("生效结构：%s 话题 · %s 篇 · 建于 %s"
          % (meta.get("n_topics"), meta.get("n_articles"),
             meta.get("catalog_built_at")))
    print("主话题 %s 条 · 待确认的次话题 %d 条 · 未挂上任何话题 %s 篇 · 每话题 %s~%s 篇"
          % (meta.get("n_primary"), pend, meta.get("unmounted"),
             meta.get("topic_count_min"), meta.get("topic_count_max")))
    label = {1: "领域", 2: "板块", 3: "话题"}
    for row in themes:
        indent = "  " * (row["level"] - 1)
        print("%s[%s] %-30s %3d 篇"
              % (indent, label[row["level"]], row["name"][:30], row["article_count"]))
    return 0


CMDS = {"profiles": cmd_profiles, "mount": cmd_mount, "apply": cmd_apply,
        "list": cmd_list, "tags": cmd_tags}


def main(argv) -> int:
    cmd = argv[0] if argv and not argv[0].startswith("-") else None
    if not cmd:
        print(__doc__)
        return 0
    if cmd not in CMDS:
        print("未知子命令 %r，可选：%s" % (cmd, "、".join(CMDS)))
        return 2
    return CMDS[cmd](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))