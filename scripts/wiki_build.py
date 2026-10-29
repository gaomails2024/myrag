#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识编译 Wiki（PRD §5.7）：把一个话题下的文章编译成一页带引用的知识页。

    .venv/bin/python scripts/wiki_build.py topics                # 话题清单：各多少篇、编译过没有
    .venv/bin/python scripts/wiki_build.py build t54 --dry-run   # 先看取材结果，不调模型不写库
    .venv/bin/python scripts/wiki_build.py build t54             # 真编译（需 .env 配好 LLM）
    .venv/bin/python scripts/wiki_build.py show t54              # 终端预览 + 角标死链自检
    .venv/bin/python scripts/wiki_build.py list                  # 已编译页面清单

**为什么是独立脚本而不是后端接口**：编译一次要读二三十篇素材、跑几十秒到几分钟。
放进后端就得引入后台线程、任务队列、进度轮询 —— 而这是单人低频工具
（PROJECT.md：按需启动、无常驻），为一个低频动作加一套常驻任务队列属于过度设计。
写法与 `scripts/structure.py apply` 完全同构：独立进程直连 DB，写前自动备份。

**提示词在 `prompts/wiki_compile_v1.md`，不在代码里**：改完直接重编译生效，
前后版本（文件名#内容指纹）记进页面，页面上能看出来这页是哪版提示词编译的。

**为什么归属筛选是命令行参数而不是改数据**：实测「道家思想与道德经」31 篇里
只有 6~7 篇真是道家类，其余是「不懂经」公众号的 AI 认知文章 —— 这是主题归属
的质量问题（标签碎片化，PROJECT.md §4 已记），**该在结构层解决**。
wiki 这边只提供 `--category dao` 这样的取料口子，不去动 article_themes。
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import config, db, llm, wiki  # noqa: E402


def _flag(argv, name, default=None):
    """取 --name value 或 --name=value；只有 --name 没带值时返回 True（当开关用）。"""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _has(argv, name) -> bool:
    return any(a == name or a.startswith(name + "=") for a in argv)


def _split_list(value) -> list[str]:
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


# ---------------------------------------------------------------- 子命令

def cmd_topics(argv) -> int:
    del argv
    db.init_db()
    with db.conn_ctx() as conn:
        rows = wiki.topic_rows(conn)
    print("话题共 %d 个。**目录 key**（catalog.json 里的，如 t37）与 **库 id**（themes.id）"
          "是两套编号，两个都能用：\n" % len(rows))
    for r in rows:
        mark = "  ← 已编译" if r["compiled_slug"] else ""
        key = r["catalog_key"] or "（不在目录里）"
        print("%-6s %-6s %-30s %4d 篇  %s%s"
              % (r["slug"], key, r["name"][:28], r["article_count"],
                 " / ".join(p for p in r["path"][:2] if p), mark))
    print("\n编译一个话题：wiki_build.py build <目录key或库id> [--dry-run] [--category dao]")
    return 0


def cmd_build(argv) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 0
    ref = argv[0]
    dry = _has(argv, "--dry-run")
    force = _has(argv, "--force")
    prompt = _flag(argv, "--prompt")
    max_chars = _flag(argv, "--max-chars")
    categories = _split_list(_flag(argv, "--category"))
    roles = _split_list(_flag(argv, "--role"))

    config.ensure_dirs()
    db.init_db()

    with db.conn_ctx() as conn:
        row = wiki.find_topic(conn, ref)
        if row is None:
            print("找不到话题 %r。跑 `wiki_build.py topics` 看有哪些。" % ref, file=sys.stderr)
            return 2
        if row["compiled_slug"] and not force and not dry:
            print("话题「%s」已经编译过了（%s）。要重编译请加 --force。"
                  % (row["name"], row["compiled_slug"]))
            print("改了 prompts/ 里的提示词之后，用 --force 重编译就会用上新提示词。")
            return 0

        if dry:
            st = wiki.build_page(conn, row["id"], dry_run=True,
                                 max_chars=int(max_chars) if max_chars else None,
                                 prompt_path=prompt, categories=categories or None,
                                 roles=roles or None)
            print("\n[dry-run] 不调模型、不写库。取材结果：")
            _print_stats(st)
            return 0

        # 真编译：先备份，再动笔。备份必须在**写之前**，不是之后。
        print("备份数据库…")
        backup = db.backup_db("wiki")
        print("备份：%s" % backup)
        try:
            st = wiki.build_page(conn, row["id"], dry_run=False,
                                 max_chars=int(max_chars) if max_chars else None,
                                 prompt_path=prompt, categories=categories or None,
                                 roles=roles or None)
        except wiki.WikiBuildError as e:
            # build_page 在校验全过之后才 _persist，所以这里库里什么都没变，
            # 不需要回滚 —— 只要别把半成品留在库里（这正是「先校验、后写入」的意义）。
            print("\n编译失败：%s" % e, file=sys.stderr)
            return 1
        print("\n编译完成：")
        _print_stats(st)
        # 落盘自检：这一页必须**当场**能查出来。
        # 「终端说成功、库里查不到」是这里最危险的失败模式 —— 之前就踩过
        # （在只读连接里写、没 commit，进程退出即回滚）。宁可多查一次。
        with db.conn_ctx() as chk:
            saved = wiki.page_detail(chk, st["slug"])
        if not saved or len(saved["refs"]) != len(st["refs"]):
            print("\n⚠️  写入没能落库（页面或引用查不到），请报我。", file=sys.stderr)
            return 1
        print("  已落盘确认：%d 条引用都在库里" % len(saved["refs"]))
        print("\n看页面：bash scripts/start.sh 起后端 → 工作台点「Wiki」页签。")
    return 0


def _print_stats(st: dict) -> None:
    if st.get("dry_run"):
        print("  素材：%d 篇 / %d 字" % (st["source_count"], st["chars"]))
        print("  填好提示词后共 %d 字（约 %d token）" % (st["prompt_chars"], st["prompt_chars"] // 2))
        print("  提示词版本：%s" % st["prompt_version"])
        print("  模型：%s" % st["model"])
        if st["dropped"]:
            print("  被截断 %d 篇：%s" % (len(st["dropped"]), "、".join(st["dropped"])))
        return
    print("  页面：%s（%s）" % (st["slug"], st["title"]))
    print("  正文 %d 字 · 引用 %d / %d 篇（覆盖 %.0f%%）"
          % (len(st["content_md"]), len(st["refs"]), st["source_count"],
             100.0 * len(st["refs"]) / max(1, st["source_count"])))
    print("  模型：%s" % st["model"])
    print("  提示词版本：%s" % st["prompt_version"])
    if st.get("warnings"):
        print("  警告 %d 条：" % len(st["warnings"]))
        for w in st["warnings"]:
            print("    - %s" % w)
    u = st.get("usage") or {}
    if u.get("prompt_tokens") or u.get("completion_tokens"):
        think = u.get("reasoning_tokens") or 0
        print("  token：输入 %d · 输出 %d%s"
              % (u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                 ("（其中推理 %d）" % think) if think else ""))
    if st.get("dropped"):
        print("  被截断 %d 篇：%s" % (len(st["dropped"]), "、".join(st["dropped"])))


def cmd_show(argv) -> int:
    if not argv:
        print("用法：wiki_build.py show <目录key / 库id / 话题名>")
        return 0
    with db.conn_ctx() as conn:
        # **先当 slug 查**，查不到才去解析成话题 —— show 的对象是「页面」，
        # 页面的标识是 slug。反过来（先按目录 key 解释）会出这种怪事：
        # `show t21` 里的 t21 恰好是另一个话题的目录 key，于是去查了那个话题的页面。
        d = wiki.page_detail(conn, argv[0])
        if not d:
            row = wiki.find_topic(conn, argv[0])
            d = wiki.page_detail(conn, row["slug"]) if row else None
    if not d:
        print("没有编译过 %s。" % argv[0], file=sys.stderr)
        return 2
    print("=" * 68)
    print(d["title"])
    print("话题：%s（目录 key %s · 页面 slug %s）"
          % (d.get("topic_name") or "?", d.get("catalog_key") or "?", d["slug"]))
    print("概览：%s" % (d["summary"] or "（无）"))
    print("模型=%s · 提示词=%s · 构建于 %s · 更新于 %s"
          % (d["model"], d["prompt_version"], d["built_at"], d["updated_at"]))
    print("=" * 68)
    print(d["content_md"])
    print("\n引用文献（%d 篇）：" % len(d["refs"]))
    for r in d["refs"]:
        print("  [R%d] %s · %s%s" % (r["ord"], r["id"], (r["title"] or "")[:40],
                                      ("  「%s」" % r["quote"][:30]) if r["quote"] else ""))
    if d["links"]:
        print("\n相关页面：%s" % "、".join(l["label"] or l["dst_slug"] for l in d["links"]))
    # 自检：正文里出现的 [Rn] 是否都在 refs 里登记过 —— 没登记就是点不开的死角标
    used = {int(x) for x in re.findall(r"\[R(\d+)\]", d["content_md"])}
    known = {r["ord"] for r in d["refs"]}
    dead = sorted(used - known)
    print("\n角标自检：正文用到 %d 个编号 / refs 登记 %d 个%s"
          % (len(used), len(known), ("，死链 %s" % dead) if dead else "，无死链"))
    return 1 if dead else 0


def cmd_list(argv) -> int:
    del argv
    db.init_db()
    with db.conn_ctx() as conn:
        rows = wiki.page_rows(conn)
    if not rows:
        print("还没有编译过任何话题。\n先看清单：.venv/bin/python scripts/wiki_build.py topics")
        return 0
    print("%-6s %-30s %5s %-14s %s" % ("slug", "标题", "引用", "模型", "构建时间"))
    for r in rows:
        print("%-6s %-30s %5d %-14s %s"
              % (r["slug"], (r["title"] or "")[:28], r["source_count"] or 0,
                 (r["model"] or "")[:14], r["built_at"]))
    return 0


def cmd_concepts(argv) -> int:
    """概念层：发现 / 归属 / 编译 / 织网 / 增量（PRD §5.7.1）。

        concepts discover        从素材里发现概念（分批调 LLM，几分钟）
        concepts assign          向量补全成员（秒级，纯向量）
        concepts list            概念清单
        concepts compile <名字>   编译一个概念页
        concepts compile --all --min-members N   批量编译
        concepts link            织网（概念之间互链）
        concepts attach          把概念归到主题树的话题下（纯向量，0 token）
        concepts grow            扫新文章，把受影响的页标记待更新
    """
    from server import wiki_concepts as wc

    if not argv:
        print(__doc__)
        return 0
    sub, rest = argv[0], argv[1:]
    db.init_db()
    config.ensure_dirs()

    if sub == "discover":
        _lim = _flag(rest, "--limit")
        limit = int(_lim) if _lim else None
        with db.conn_ctx() as conn:
            st = wc.discover(conn, limit=limit)
        print("\n新增 %d · 合并 %d · 成员 %d 条 · 无成员丢弃 %d · 成员不足丢弃 %d"
              % (st["new"], st["merged"], st["members"],
                 st["dropped_no_member"], st["dropped_too_few"]))
        return 0

    if sub == "assign":
        with db.conn_ctx() as conn:
            st = wc.assign(conn)
        return 0 if st["added"] >= 0 else 1

    if sub == "list":
        with db.conn_ctx() as conn:
            rows = wc.concept_rows(conn)
        if not rows:
            print("还没有概念。先跑：wiki_build.py concepts discover")
            return 0
        print("%-22s %5s %5s %6s %s" % ("概念", "成员", "互链", "待更新", "编译状态"))
        for r in rows:
            mark = "已编译" if r["page_id"] else ("待编译" if r["article_count"] else "无成员")
            if r["pending"]:
                mark += " +%d" % r["pending"]
            print("%-22s %5d %5d %6s %s"
                  % (r["name"][:20], r["article_count"], r["link_count"],
                     r["pending"] or "", mark))
        print("\n共 %d 个概念 · 已编译 %d 个"
              % (len(rows), sum(1 for r in rows if r["page_id"])))
        return 0

    if sub == "compile":
        force = _has(rest, "--force")
        dry = _has(rest, "--dry-run")
        pending_only = _has(rest, "--pending-only")
        min_members = int(_flag(rest, "--min-members") or 2)
        _mc = _flag(rest, "--max-chars")
        max_chars = int(_mc) if _mc else None
        targets: list[dict] = []
        with db.conn_ctx() as conn:
            if rest and not rest[0].startswith("-"):
                c = conn.execute("SELECT id, name, slug FROM wiki_concepts WHERE name = ?",
                                 (rest[0],)).fetchone()
                if not c:
                    c = conn.execute(
                        "SELECT id, name, slug FROM wiki_concepts WHERE slug = ?",
                        (rest[0],)).fetchone()
                if not c:
                    print("没有叫「%s」的概念。跑 `concepts list` 看有哪些。" % rest[0],
                          file=sys.stderr)
                    return 2
                targets = [dict(c)]
            else:
                rows = wc.concept_rows(conn)
                targets = [dict(r) for r in rows
                           if r["article_count"] >= min_members
                           and (force or not r["page_id"])]
                if pending_only:
                    # 只重编译「真的多了新成员」的概念 —— sync 报的就是这批。
                    # 不加这个过滤的话 --force 会把 145 页全部重编一遍（≈30 分钟、烧钱）。
                    hit = [dict(r) for r in targets if r["pending"]]
                    print("待更新（有新成员）的概念 %d 个 / 共 %d 个"
                          % (len(hit), len(targets)))
                    targets = hit
                if not targets:
                    print("没有待编译的概念（成员≥%d%s）。加 --force 可重编译。"
                          % (min_members, "、有 pending" if pending_only else ""))
                    return 0
            print("待编译 %d 个概念。\n" % len(targets))
            ok = fail = 0
            for t in targets:
                try:
                    st = wc.build_page(conn, t["id"], dry_run=dry, max_chars=max_chars)
                    if dry:
                        print("  %-20s 素材 %d 篇 / %d 字%s"
                              % (t["name"][:18], st["source_count"], st["chars"],
                                 ("，截断 %d 篇" % len(st["dropped"])) if st["dropped"] else ""))
                    else:
                        ok += 1
                        print("  %-20s 正文 %d 字 · 引用 %d/%d 篇%s"
                              % (t["name"][:18], len(st["content_md"]), len(st["refs"]),
                                 st["source_count"],
                                 "  ⚠️%d 条警告" % len(st["warnings"]) if st["warnings"] else ""))
                except wiki.WikiBuildError as e:
                    fail += 1
                    print("  %-20s 失败：%s" % (t["name"][:18], str(e).split("\n")[0]),
                          file=sys.stderr)
                except llm.LLMError as e:
                    fail += 1
                    print("  %-20s LLM 失败：%s" % (t["name"][:18], str(e).split("\n")[0]),
                          file=sys.stderr)
        print("\n成功 %d · 失败 %d" % (ok, fail))
        return 1 if fail else 0

    if sub == "link":
        with db.conn_ctx() as conn:
            st = wc.link(conn)
        return 0

    if sub == "attach":
        with db.conn_ctx() as conn:
            st = wc.attach_topics(conn)
        return 0

    if sub == "grow":
        with db.conn_ctx() as conn:
            st = wc.scan_growth(conn)
        return 0

    print("未知子命令 %r。\n%s" % (sub, __doc__))
    return 2


def cmd_rebind(argv) -> int:
    """结构重建（structure.py apply）之后，把 wiki 页挂回话题。

    **为什么必须有这一步**：apply 会 `DELETE FROM themes` 整表重建，
    而 wiki_pages.topic_id 是 ON DELETE SET NULL —— 重建完所有页都会丢掉话题关联
    （页还在，但工作台左树的「已编译」圆点消失、点话题打不开它）。
    这个子命令把 apply 前记下的「页 → 话题名」重新对回新的话题 id。

    用法（必须按这个顺序，apply 前后各一次）：
        wiki_build.py rebind --before     # apply 之前：记下名字
        scripts/structure.py apply
        wiki_build.py rebind --after      # apply 之后：按名字挂回去
    """
    before = "--before" in argv
    db.init_db()
    if before:
        with db.conn_ctx() as conn:
            saved = wiki.snapshot_topic_names(conn)
        path = config.DATA_DIR / "structure" / "wiki_topics.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已记下 %d 个页面对应的话题：" % len(saved))
        for slug, name in saved.items():
            print("  %-6s %s" % (slug, name))
        print("→ 存到 %s\n  现在可以跑 scripts/structure.py apply，"
              "之后再跑 wiki_build.py rebind --after" % path)
        return 0

    path = config.DATA_DIR / "structure" / "wiki_topics.json"
    if not path.is_file():
        print("找不到 %s —— 先跑 `wiki_build.py rebind --before`（在 apply 之前）。" % path,
              file=sys.stderr)
        return 2
    saved = json.loads(path.read_text(encoding="utf-8"))
    with db.conn_ctx() as conn:
        st = wiki.rebind_topics(conn, saved)
    print("已挂回 %d 个页面" % len(st["rebound"]))
    for s in st["rebound"]:
        print("  %s ✓" % s)
    if st["orphan"]:
        print("\n挂不回去的 %d 个（目录里那个话题被改名或删了，**不硬挂**）：" % len(st["orphan"]),
              file=sys.stderr)
        for slug, name in st["orphan"]:
            print("  %s（原话题「%s」）" % (slug, name), file=sys.stderr)
        return 1
    return 0


def cmd_sync(argv) -> int:
    """把新文章同步进 wiki —— **这一步 0 token**，跑完只报告，不花钱。

    这是给「以后又有新文章进来」准备的。四步，前三步全在本地：

        1. profiles  给没有画像的新文章补向量                    本地 BGE-M3，0 token
        2. assign    新文章 × 概念质心比相似度，命中就加成员    纯向量，0 token
        3. grow      标出「有概念该更新了」（pending+N）        纯向量，0 token
        4. 报告      告诉你哪些概念要重编译、花多少 token        0 token

    **故意不自动重编译**：那一步每次约 2 万 token（实测 33k 字素材 ≈ 1.7 万输入），
    该由你决定什么时候花。自动化边界就卡在 token 上。

        wiki_build.py sync              # 同步并报告
        wiki_build.py sync --discover   # 顺便重新发现概念（会花约 7 次调用）
    """
    from server import structure, wiki_concepts as wc

    do_discover = _has(argv, "--discover")
    config.ensure_dirs()
    db.init_db()

    print("=" * 68)
    print("同步开始（前三步不花 token）")
    print("=" * 68)

    with db.conn_ctx() as conn:
        st = structure.build_profiles(conn)
    print("\n[1/4] 画像向量：新算 %d 篇" % st.get("computed", 0))

    if do_discover:
        with db.conn_ctx() as conn:
            dst = wc.discover(conn)
        print("\n[2/4] 概念发现：新增 %d · 合并 %d · 丢掉（成员不足）%d"
              % (dst["new"], dst["merged"], dst["dropped_too_few"]))
    else:
        print("\n[2/4] 概念发现：跳过（加 --discover 会跑，约 7 次 LLM 调用）")

    with db.conn_ctx() as conn:
        before = conn.execute("SELECT COUNT(*) n FROM wiki_concept_articles").fetchone()["n"]
        wc.assign(conn)
        after = conn.execute("SELECT COUNT(*) n FROM wiki_concept_articles").fetchone()["n"]
    # 口径必须与 assign 的实际判断一致：**向量成员**（非 confirmed）满额才算满员。
    # 之前这里按 article_count（总成员数）算，145 个概念里报「144 满员」——
    # 但 confirmed 是 LLM 点名的、从不占名额，拿它算就是误导。
    with db.conn_ctx() as conn:
        full = sum(1 for r in conn.execute(
            """SELECT concept_id, COUNT(*) k FROM wiki_concept_articles
               WHERE role <> 'confirmed' GROUP BY concept_id HAVING k >= ?""",
            (wc.MAX_NEW_MEMBERS,)))
    print("\n[3/4] 概念归属：新挂 %d 条（成员关系 %d → %d）；%d 个概念的向量成员已满 %d 篇"
          "\n      （满员后不再拒绝新文章：分数更高的会把分数更低的挤掉）"
          % (after - before, before, after, full, wc.MAX_NEW_MEMBERS))

    with db.conn_ctx() as conn:
        wc.scan_growth(conn)
        # **报告要读数据库里的 pending，不能只读 scan_growth 的返回值** ——
        # 那个返回值只含「本轮 grow 新命中」的，而 pending 还可能是上一轮
        # assign 挤位时累加的（实测：grow 返回 0 个，但库里有 37 个 pending）。
        pend = conn.execute(
            """SELECT name, pending, article_count FROM wiki_concepts
               WHERE pending > 0 AND compiled_at IS NOT NULL
               ORDER BY pending DESC, article_count DESC""").fetchall()

    print("\n[4/4] 待更新的概念：%d 个" % len(pend))
    if not pend:
        print("  没有概念需要更新 —— 没有新文章命中任何概念（或上一轮刚同步过）。")
        print("\n✅ 同步完成，本次没有花任何 token。")
        return 0

    print()
    for d in pend[:12]:
        print("  %-22s +%d 篇新成员（共 %d 篇在谈）"
              % (d["name"][:20], d["pending"], d["article_count"]))
    if len(pend) > 12:
        print("  … 另有 %d 个（跑 concepts list 看全部）" % (len(pend) - 12))

    n = len(pend)
    print("\n" + "-" * 68)
    print("要让这些页面反映新文章，需要重编译（**这一步要花 token**）：")
    print("  约 %d 次调用 × 约 2 万 token ≈ %d 万 input token，耗时约 %d 分钟"
          % (n, n * 2, max(1, n * 12 // 60)))
    print("  .venv/bin/python scripts/wiki_build.py concepts compile --pending-only --force")
    print("-" * 68)
    print("不急可以攒着：页面现在的内容仍然是对的，只是少了这几篇新文章。")
    return 0



CMDS = {"topics": cmd_topics, "build": cmd_build, "show": cmd_show,
        "list": cmd_list, "rebind": cmd_rebind, "concepts": cmd_concepts,
        "sync": cmd_sync}


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