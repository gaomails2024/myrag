# -*- coding: utf-8 -*-
"""知识编译 Wiki（PRD §5.7）：把一个话题下的文章编译成一页带引用的知识页。

**为什么要有这个功能**：检索是「提问时现场找」—— 同一批原文被反复读，每次都要
重新理解，而理解出来的东西不留下。Wiki 是反过来：在编译期把一批文章读透，写成
一篇成品页，每条结论挂着编号引用，页面上点角标就能回到原文。它替代不了检索
（库里 752 篇、粒度到段，没被编译过的内容仍然只能靠检索找），但「这个话题整体
讲了什么」这种问题，只适合读一篇成品页。

**取材原则（纯 SQL，不碰文件系统、不加载任何模型）**：
一篇 wiki 页要覆盖二三十篇文章，直接塞全文必然溢出且昂贵。所以每篇只取
「元数据 + 摘要 + 从 chunks 均匀抽样的几段」：
  · 均匀抽样而不是取前 N 段 —— 前 N 段往往是开场白，均匀抽才覆盖得到全文论点；
  · 段落从 chunks 表读（早就切好存在库里了），顺带省掉读原始 md 的 IO。
31 篇大约读 400 段，毫秒级。

**任何一步失败都不写库**：校验不过、模型返回不合法、素材一条都没有 —— 全部抛
WikiBuildError，由调用方保证「要么整页写进去，要么库里什么都没变」。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

from . import config, db, llm

# 单段送进模型前的字数上限。chunks 最长可到 1024 token，逐段砍半能把一篇的
# 上下文压到可控范围，又不至于只剩一句话看不出论点。
MAX_CHARS_PER_CHUNK = 400
# 每篇最多抽几段。段数少的（3~5 段）全给，多的均匀抽。
MAX_CHUNKS_PER_ARTICLE = 6

LEVEL_NAMES = {1: "领域", 2: "板块", 3: "话题"}


class WikiBuildError(RuntimeError):
    """编译失败。调用方收到它必须放弃写入，不留半成品页。"""


# ---------------------------------------------------------------- 话题查询

def slug_for(topic_id: int) -> str:
    """页面 slug：与话题一一对应，不随标题变化。

    为什么不用标题做 slug：改一次标题就换一个链接，已经分享出去的地址全废。
    """
    return "t%d" % int(topic_id)


def _catalog_keys() -> dict[str, str]:
    """catalog.json 的 key（t37）→ 主题名。

    **为什么需要这一层**：目录源 `data/structure/catalog.json` 里的 key 与库表
    `themes.id` 是两套编号，**同一个话题对不上** —— 实测「道家思想与道德经」
    在 catalog 里是 t37、在库里是 id 54。用户照目录敲 `build t37` 会编译到
    另一个话题上（而且不报错，编出来才发现）。所以任何编号都先按 catalog 解释，
    再退回库 id。
    """
    path = config.DATA_DIR / "structure" / "catalog.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for d in data.get("domains") or []:
        if d.get("key"):
            out[str(d["key"])] = d.get("name") or ""
        for b in d.get("boards") or []:
            if b.get("key"):
                out[str(b["key"])] = b.get("name") or ""
            for t in b.get("topics") or []:
                if t.get("key"):
                    out[str(t["key"])] = t.get("name") or ""
    return out


def topic_rows(conn) -> list[dict]:
    """全部话题（level=3）连同祖先路径、篇数、是否已编译、目录 key。"""
    themes = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, summary, level, parent_id FROM themes WHERE status = 'active'")}
    counts = {r["theme_id"]: r["n"] for r in conn.execute(
        """SELECT theme_id, COUNT(*) n FROM article_themes
           WHERE status <> 'rejected' GROUP BY theme_id""")}
    compiled = {r["topic_id"]: r["slug"] for r in conn.execute(
        "SELECT topic_id, slug FROM wiki_pages WHERE topic_id IS NOT NULL")}
    # 名字 → 目录 key：反查一次，让命令行能同时报出两套编号
    keys = _catalog_keys()
    name2key = {v: k for k, v in keys.items() if v}

    out = []
    for t in themes.values():
        if t["level"] != 3:
            continue
        board = themes.get(t["parent_id"]) or {}
        domain = themes.get(board.get("parent_id")) or {}
        out.append({
            "id": t["id"], "name": t["name"], "summary": t["summary"] or "",
            "slug": slug_for(t["id"]), "catalog_key": name2key.get(t["name"], ""),
            "path": [domain.get("name"), board.get("name"), t["name"]],
            "article_count": counts.get(t["id"], 0),
            "compiled_slug": compiled.get(t["id"]),
        })
    out.sort(key=lambda x: (x["path"][0] or "", x["path"][1] or "", x["name"]))
    return out


def find_topic(conn, ref: str | int) -> dict | None:
    """按目录 key（t37）、库 id（'54' 或 't54'）、或名字找话题。

    **顺序有意为之**：带 t 前缀的编号先按目录 key 解释，找不到才按库 id ——
    用户手上是目录（catalog.json）与文档，写的是 key。
    """
    if ref is None:
        return None
    ref = str(ref).strip()
    rows = topic_rows(conn)

    if ref.startswith("t") and ref[1:].isdigit():
        name = _catalog_keys().get(ref)
        if name:
            for row in rows:
                if row["name"] == name:
                    return row
        for row in rows:
            if row["slug"] == ref:
                return row
    elif ref.isdigit():
        for row in rows:
            if row["id"] == int(ref):
                return row
    for row in rows:
        if row["name"] == ref:
            return row
    return None


def topic_path_str(row: dict) -> str:
    return " / ".join(p for p in row.get("path") or [] if p)


# ---------------------------------------------------------------- 取材

def _pick_evenly(items: list, k: int) -> list:
    """从 items 里均匀抽最多 k 个（保留原顺序）。"""
    n = len(items)
    if n <= k:
        return list(items)
    step = n / k
    return [items[int(i * step)] for i in range(k)]


def _md_cleanup(text: str) -> str:
    """素材里去掉纯噪音：图片引用、图内 OCR 行、HTML 残留、连续空行。

    为什么必须清：OCR 注入的 `> 图片文字：` 块一个字一行，留着会白白吃掉
    上下文预算，而且对「这一篇在讲什么」毫无帮助。
    """
    out = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    out = re.sub(r"^>\s*图片文字.*$", "", out, flags=re.M)     # 图内 OCR 引导行
    out = re.sub(r"<[^>]+>", "", out)                          # HTML 标签
    out = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", out)            # 再清一次（清标签后才暴露的）
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def collect_material(conn, topic_id: int, *, categories=None, roles=None,
                     max_chars=None):
    """按话题取材（话题综述页用）。返回 (素材块, 被截断的, 总字数)。

    真正干活的是 `material_from_rows` —— 概念页喂给它的 rows 结构完全一样
    （只是来源不同），所以**取材只有一份实现**，改抽样规则不会两边走偏。
    """
    limit = max_chars or config.LLM_MAX_CHARS
    rows = [dict(r) for r in conn.execute(
        """SELECT a.id, a.title, a.summary, a.source, a.published_at, a.category,
                  a.tags, at.weight, at.role
           FROM article_themes at JOIN articles a ON a.id = at.article_id
           WHERE at.theme_id = ? AND at.status <> 'rejected' AND a.status <> 'failed'
           ORDER BY at.weight DESC""", (topic_id,))]

    # 筛选：话题归属质量参差（实测 t37「道家」31 篇里只有 6~7 篇真是 dao 类），
    # 所以给一个只取某几类的口子 —— 但**不改归属数据**，那是结构层的事。
    if categories:
        cats = set(categories)
        rows = [r for r in rows if r["category"] in cats]
    if roles:
        rs = set(roles)
        rows = [r for r in rows if r["role"] in rs]
    return material_from_rows(conn, rows, max_chars=limit)


def material_from_rows(conn, rows: list[dict], *, max_chars: int | None = None):
    """把「一篇文章清单」变成素材块。话题页与概念页共用这一份。

    截断规则：按 weight **从高到低**取，累计超上限就停。
    权重低的文章先被丢掉，并且**逐条回报**给调用方打印 —— 静默丢料是最难查
    的一类问题（页面看着完整，其实少了一半素材）。
    """
    limit = max_chars or config.LLM_MAX_CHARS
    if not rows:
        return [], [], 0

    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    grouped: dict[str, list[dict]] = {i: [] for i in ids}
    for r in conn.execute(
            """SELECT article_id, ordinal, heading_path, text FROM chunks
               WHERE article_id IN (%s) AND ordinal > 0
               ORDER BY article_id, ordinal""" % marks, ids):
        grouped[r["article_id"]].append(dict(r))

    cat_names = {r["key"]: r["name"] for r in conn.execute("SELECT key, name FROM categories")}
    chosen: list[dict] = []
    dropped: list[dict] = []
    total = 0
    for r in rows:
        picked = _pick_evenly(grouped.get(r["id"]) or [], MAX_CHUNKS_PER_ARTICLE)
        excerpts = []
        for c in picked:
            t = _md_cleanup(c["text"] or "")[:MAX_CHARS_PER_CHUNK]
            if t:
                hp = (c.get("heading_path") or "").strip()
                excerpts.append(("%s：%s" % (hp, t)) if hp else t)
        body = "\n".join("> " + e.replace("\n", "\n> ") for e in excerpts)
        tags = db.jloads(r.get("tags"), [])
        head = "[%d] %s · %s · %s · %s · 分类 %s(%s) · 归属权重 %.2f · %s" % (
            len(chosen) + 1, r["id"], (r["title"] or "").strip() or "(无标题)",
            r["source"] or "来源未知", (r["published_at"] or "")[:10] or "日期未知",
            r["category"], cat_names.get(r["category"], r["category"]), r["weight"] or 0.0,
            r["role"])
        meta = [head]
        if r["summary"]:
            meta.append("摘要：%s" % r["summary"].strip())
        if tags:
            meta.append("标签：" + "、".join(str(x) for x in tags))
        block = {"article_id": r["id"], "title": r["title"] or "",
                 "weight": r["weight"] or 0.0, "category": r["category"],
                 "text": "\n".join(meta) + ("\n" + body if body else "\n（正文分段为空）")}

        cost = len(block["text"])
        if total + cost > limit and chosen:
            dropped.append({"id": r["id"], "title": r["title"] or "",
                            "chars": cost, "reason": "超出字数上限 %d" % limit})
            continue
        total += cost
        block["ord"] = len(chosen) + 1
        chosen.append(block)

    return chosen, dropped, total


# ---------------------------------------------------------------- 提示词

def load_prompt(path=None) -> tuple[str, str]:
    """读外置提示词模板，返回 (正文, 版本标识)。

    版本标识 = 「文件名#内容前 8 位 sha1」：提示词文件被改动后重编译，
    页面上能立刻看出这一页是新 prompt 编译的，日后也能照原样复现。
    """
    p = config.PROMPT_DEFAULT if path is None else config.ROOT / str(path)
    p = p if p.is_absolute() else (config.ROOT / p)
    if not p.is_file():
        raise WikiBuildError(
            "找不到提示词文件：%s\n它决定「怎么把素材写成知识页」，必须有。" % p)
    text = p.read_text(encoding="utf-8")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return text, "%s#%s" % (p.name, digest)


_REF_RE = re.compile(r"\[R(\d+)\]")
# 真实编译踩过：提示词里写的是 [Rn]，模型交回来的是 [n]（少了 R），
# 而页面渲染与死链自检都只认 [Rn] —— 结果一个角标都不出现。
# 所以**两种写法都收进来，在写库前统一改写成 [Rn]**：
# 收是宽容（不因为一个字母丢掉整页引用），统一是严格（下游只认一种形状）。
# 纯数字方括号不会误伤 markdown 链接（那是 [文字](url)，带括号）。
_REF_ANY_RE = re.compile(r"\[R?(\d+)\]")


def build_page(conn, topic_id: int, *, dry_run: bool = False, max_chars: int | None = None,
               prompt_path=None, categories: list[str] | None = None,
               roles: list[str] | None = None,
               log: Callable[[str], None] = print) -> dict:
    """编译一个话题。返回统计 dict。

    流程：取材 → 填提示词 → 调模型 → **强校验** → 单事务 upsert。
    dry_run 只走取材与提示词组装，不调模型、不写库（没配 key 也能先看取材质量）。
    """
    row = find_topic(conn, topic_id)
    if row is None:
        raise WikiBuildError("找不到话题 %r。用 `wiki_build.py topics` 看有哪些话题。" % topic_id)

    template, prompt_version = load_prompt(prompt_path)
    blocks, dropped, total = collect_material(
        conn, row["id"], categories=categories, roles=roles, max_chars=max_chars)
    if not blocks:
        raise WikiBuildError(
            "话题「%s」在当前筛选下没有素材。放宽筛选再试（默认取全部挂载文章）。"
            % row["name"])

    log("话题：%s（%s）" % (row["name"], topic_path_str(row)))
    log("素材：%d 篇 / %d 字；被截掉 %d 篇"
        % (len(blocks), total, len(dropped)))
    for d in dropped:
        log("  [截断] %s %s（%d 字）—— %s" % (d["id"], d["title"][:28], d["chars"], d["reason"]))

    user = (template
            .replace("{{topic}}", row["name"])
            .replace("{{topic_summary}}", row["summary"] or "（无）")
            .replace("{{n_articles}}", str(len(blocks)))
            .replace("{{material}}", "\n\n".join(b["text"] for b in blocks)))

    if dry_run:
        return {"slug": row["slug"], "topic": row["name"], "dry_run": True,
                "source_count": len(blocks), "chars": total,
                "dropped": [d["id"] for d in dropped], "prompt_chars": len(user),
                "prompt_version": prompt_version, "model": config.LLM_MODEL,
                "content_md": "", "refs": [], "usage": {}}

    reason = llm.missing_reason()
    if reason:
        raise WikiBuildError(reason + "\n（也可以先用 --dry-run 看取材质量，不调模型。）")

    log("调用模型 %s …（素材 %d 字，约 %d token）" % (config.LLM_MODEL, total, total // 2))
    obj, usage = llm.chat_json(
        system=("你是一个知识编译器：把给定的文章素材编译成一篇结构化的中文知识页。"
                "只使用素材内的信息，每条具体论断都要挂 [Rn] 角标，输出严格的 JSON。"),
        user=user)
    page = _validate(obj, blocks, log=log)

    md = "\n\n".join("## %s\n\n%s" % (s["heading"], s["body"].strip())
                     for s in page["sections"])
    result = {"slug": row["slug"], "topic": row["name"], "dry_run": False,
              "title": page["title"], "summary": page["summary"],
              "source_count": len(blocks), "chars": total, "dropped": [d["id"] for d in dropped],
              "content_md": md, "refs": page["refs"], "links": page["links"],
              "warnings": page["warnings"], "usage": usage,
              "prompt_version": prompt_version, "model": config.LLM_MODEL}

    _persist(row, result)
    return result


# ---------------------------------------------------------------- 校验

def _quote_for(n: int, sections: list[dict]) -> str:
    """给编号 n 找一句配套的原文摘录。

    摘录在 _validate 里已按「与 refs 同序一一对应」配好（quote_of），
    这里只负责汇总。**配不上就留空** —— 猜一句挂上去会被用户当成这条引用的出处，
    那比空着坏得多。
    """
    for sec in sections:
        if n in sec["refs"] and sec["quote_of"].get(n):
            return sec["quote_of"][n]
    return ""


def _validate(obj: dict, blocks: list[dict], log=print) -> dict:
    """把模型返回的东西查干净。**宁可丢内容也不让脏引用进库** —— 死链比缺一段更糟。"""
    warnings: list[str] = []
    title = str(obj.get("title") or "").strip()
    summary = str(obj.get("summary") or "").strip()
    if not title:
        title = "（未命名）"
        warnings.append("模型没给标题，已用占位名")
    if not summary:
        warnings.append("模型没给概览")

    raw_sections = obj.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise WikiBuildError("模型返回里没有 sections（正文分节），无法编译成页面。"
                             "换个大模型重试，或改 prompts/ 里的格式说明。")

    by_ord = {b["ord"]: b for b in blocks}
    max_n = len(blocks)
    sections: list[dict] = []
    ref_ord: dict[int, dict] = {}

    for idx, s in enumerate(raw_sections):
        if not isinstance(s, dict):
            warnings.append("第 %d 节不是对象，已跳过" % (idx + 1))
            continue
        heading = str(s.get("heading") or "").strip() or "第 %d 节" % (idx + 1)
        body = str(s.get("body") or "").strip()
        if not body:
            warnings.append("第 %d 节「%s」正文为空，已跳过" % (idx + 1, heading))
            continue
        # 引用编号：refs 里没有的，从正文里 [Rn] / [n] 捞回来；越界的一律丢弃。
        # order 记的是**模型给的原始顺序** —— quotes 要按它配对（见下面 quote 的处理）。
        nums: set[int] = set()
        order: list[int] = []
        for x in (s.get("refs") or []):
            try:
                n = int(x)
            except (TypeError, ValueError):
                warnings.append("第 %d 节的 refs 里有非数字 %r，已忽略" % (idx + 1, x))
                continue
            if n not in nums:
                nums.add(n)
                if n in by_ord:
                    order.append(n)
        for m in _REF_ANY_RE.finditer(body):
            n = int(m.group(1))
            if n not in nums:
                nums.add(n)
                if n in by_ord:
                    order.append(n)
        good, bad = set(), set()
        for n in nums:
            (good if n in by_ord else bad).add(n)
        for n in sorted(bad):
            warnings.append("第 %d 节引用了不存在的编号 %d（素材只有 %d 篇），已丢弃"
                            % (idx + 1, n, max_n))
        used = sorted(good)
        for n in used:
            ref_ord.setdefault(n, by_ord[n])
        # 正文的角标一律改写成 [Rn]：合法的补上 R，越界的整个抹掉
        #（抹掉是因为页面上留一个点不开的 [37] 比不留更糟 —— 它看起来像个引用）。
        body = _REF_ANY_RE.sub(
            lambda m: ("[R%d]" % int(m.group(1))) if int(m.group(1)) in good else "",
            body)
        body = re.sub(r"[ \t]{2,}", " ", body)
        # quotes 与 refs **同序一一对应**：只有长度一致才敢配对。
        # 配错比不配更糟 —— 文献表里挂一句不相干的原文，用户会当成这条引用的出处。
        quotes = [str(q).strip() for q in (s.get("quotes") or []) if str(q).strip()]
        quote_of: dict[int, str] = {}
        if quotes and len(quotes) == len(order):
            quote_of = dict(zip(order, quotes))
        elif quotes:
            warnings.append("第 %d 节给了 %d 条原文摘录但引用了 %d 篇，对不上，摘录留空"
                            % (idx + 1, len(quotes), len(order)))
        sections.append({"heading": heading, "body": body, "refs": used,
                         "quote_of": quote_of})

    if not sections:
        raise WikiBuildError("模型的 sections 全是空正文，编不出页面。")

    # 双链：dst_slug 必须长成 t<数字>，否则断链（本次只写不校验，但格式错了就没意义）
    links = []
    for lk in (obj.get("links") or []):
        if not isinstance(lk, dict):
            continue
        slug = str(lk.get("slug") or "").strip()
        if not re.fullmatch(r"t\d+", slug):
            warnings.append("links 里的 slug %r 不是话题编号（应为 t54 这种），已丢弃" % slug)
            continue
        links.append({"slug": slug, "label": str(lk.get("label") or "").strip()})

    refs = [{"ord": n, "article_id": ref_ord[n]["article_id"],
             "title": ref_ord[n]["title"], "weight": ref_ord[n]["weight"],
             "quote": _quote_for(n, sections)}
            for n in sorted(ref_ord)]
    for w in warnings:
        log("  [警告] %s" % w)
    return {"title": title, "summary": summary, "sections": sections,
            "refs": refs, "links": links, "warnings": warnings}


# ---------------------------------------------------------------- 写入

def _persist(topic: dict, result: dict) -> None:
    """单事务 upsert。引用与双链**先删后插**，重编译不会留下上一次的残影。

    **自己开写事务，不复用调用方给的连接** —— 这是踩过的坑：调用方给的是
    `db.conn_ctx()` 的只读连接，它**只 yield 不 commit**，在它里面 INSERT/UPDATE
    不会报错、终端照样打印「编译完成」，但进程一退出连接关闭，未提交的事务
    直接回滚 —— 表现就是「编译成功、库里查不到」。所以写必须自己 `db.tx()`。
    """
    with db.tx() as conn:
        _persist_in(conn, topic, result)


def _persist_in(conn, topic: dict, result: dict) -> None:
    now = db.now_iso()
    old = conn.execute("SELECT id, built_at FROM wiki_pages WHERE slug = ?",
                       (result["slug"],)).fetchone()
    if old:
        page_id = old["id"]
        conn.execute(
            """UPDATE wiki_pages SET title=?, topic_id=?, concept_id=?, summary=?,
                   content_md=?, source_count=?, model=?, prompt_version=?,
                   status='ok', updated_at=?
               WHERE id=?""",
            (result["title"], topic["id"], result.get("concept_id"), result["summary"],
             result["content_md"], result["source_count"], result["model"],
             result["prompt_version"], now, page_id))
        conn.execute("DELETE FROM wiki_refs WHERE page_id = ?", (page_id,))
        conn.execute("DELETE FROM wiki_links WHERE src_page_id = ?", (page_id,))
        built_at = old["built_at"]
    else:
        cur = conn.execute(
            """INSERT INTO wiki_pages
                   (slug, title, topic_id, concept_id, summary, content_md, source_count,
                    model, prompt_version, status, built_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'ok',?,?)""",
            (result["slug"], result["title"], topic["id"], result.get("concept_id"),
             result["summary"], result["content_md"], result["source_count"],
             result["model"], result["prompt_version"], now, now))
        page_id = int(cur.lastrowid)
        built_at = now

    for r in result["refs"]:
        conn.execute(
            "INSERT INTO wiki_refs (page_id, article_id, ord, quote) VALUES (?,?,?,?)",
            (page_id, r["article_id"], r["ord"], r.get("quote") or ""))
    for lk in result["links"]:
        conn.execute(
            "INSERT OR IGNORE INTO wiki_links (src_page_id, dst_slug, label) VALUES (?,?,?)",
            (page_id, lk["slug"], lk["label"]))
    result["page_id"] = page_id
    result["built_at"] = built_at


# ---------------------------------------------------------------- 读取（给 API 用）

def snapshot_topic_names(conn) -> dict[str, str]:
    """**apply 之前**调用：记下每个已编译页当时属于哪个话题（按名字，不按 id）。

    为什么记名字不记 id：`apply_catalog` 会 `DELETE FROM themes` 整表重建，
    id 按目录顺序重新编号，重建后同一个话题的 id 可能完全变了。
    而 `wiki_pages.topic_id` 是 `ON DELETE SET NULL` —— 重建完所有页的
    话题关联都会被置空（页还在，但工作台左树的「已编译」圆点会消失、点话题打不开）。
    名字是唯一稳定的锚点。
    """
    out: dict[str, str] = {}
    for p in conn.execute(
            "SELECT slug, topic_id FROM wiki_pages WHERE topic_id IS NOT NULL"):
        r = conn.execute("SELECT name FROM themes WHERE id = ? AND level = 3",
                         (p["topic_id"],)).fetchone()
        if r:
            out[p["slug"]] = r["name"]
    return out


def rebind_topics(conn, saved: dict[str, str]) -> dict:
    """**apply 之后**调用：按 apply 前记下的名字，把 wiki 页挂回新的话题 id。

    对不上的（目录里那个话题被改名或删了）**如实报出来，绝不随便挂** ——
    挂错话题比不挂更糟：用户点开一个话题，看到的是另一个话题的知识页。
    """
    ids = {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM themes WHERE level = 3")}
    rebound, orphan = [], []
    with db.tx() as w:
        for slug, name in saved.items():
            tid = ids.get(name)
            if tid is None:
                orphan.append((slug, name))
                continue
            w.execute("UPDATE wiki_pages SET topic_id = ? WHERE slug = ?", (tid, slug))
            rebound.append(slug)
    return {"rebound": rebound, "orphan": orphan}


def page_rows(conn) -> list[dict]:
    """全部已编译页面（列表页用，不含正文）。"""
    return [dict(r) for r in conn.execute(
        """SELECT p.id, p.slug, p.title, p.topic_id, p.summary, p.source_count,
                  p.model, p.prompt_version, p.status, p.built_at, p.updated_at,
                  (SELECT t.name FROM themes t WHERE t.id = p.topic_id) AS topic_name
           FROM wiki_pages p ORDER BY p.updated_at DESC""")]


def page_detail(conn, slug: str) -> dict | None:
    """一页的全部数据：正文 md + 引用（带原文标题，供角标与文献表）+ 双链。"""
    p = conn.execute("SELECT * FROM wiki_pages WHERE slug = ?", (slug,)).fetchone()
    if not p:
        return None
    refs = [dict(r) for r in conn.execute(
        """SELECT r.ord, r.quote, a.id, a.title, a.source, a.category, a.published_at
           FROM wiki_refs r JOIN articles a ON a.id = r.article_id
           WHERE r.page_id = ? ORDER BY r.ord""", (p["id"],))]
    links = [dict(r) for r in conn.execute(
        "SELECT dst_slug, label FROM wiki_links WHERE src_page_id = ?", (p["id"],))]
    d = dict(p)
    d["refs"] = refs
    d["links"] = links
    topic = conn.execute("SELECT id, name FROM themes WHERE id = ?",
                         (p["topic_id"],)).fetchone() if p["topic_id"] else None
    if topic:
        chain, cur = [], conn.execute(
            "SELECT id, name, parent_id FROM themes WHERE id = ?", (topic["id"],)).fetchone()
        while cur:
            chain.append({"id": cur["id"], "name": cur["name"]})
            cur = (conn.execute("SELECT id, name, parent_id FROM themes WHERE id = ?",
                                (cur["parent_id"],)).fetchone() if cur["parent_id"] else None)
        d["topic_path"] = list(reversed(chain))
    else:
        d["topic_path"] = []
    # 带上话题名与目录 key：页面上要告诉人「这一页是哪个话题、两套编号各是什么」，
    # 不然对着 catalog.json 敲 t37 却看到 slug=t54 会以为坏了。
    d["topic_name"] = topic["name"] if topic else ""
    d["catalog_key"] = next((k for k, v in _catalog_keys().items()
                             if v and v == d["topic_name"]), "")
    return d