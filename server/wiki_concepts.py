# -*- coding: utf-8 -*-
"""概念层：从素材里**发现概念**、把文章挂到概念上、织出互链网（PRD §5.7.1）。

这一层是「真Wiki」与「话题综述」的分界：

    话题综述（wiki.py）    页面单位 = 人工目录里预设的话题，一页覆盖几十上百篇
    概念网（这一模块）      页面单位 = LLM 从素材里发现的**概念**（有名有姓的术语）

三件事，也是 Wiki 形态成立的三个条件：

1. **发现**（`discover`）：分批把「标题 + 摘要」喂给 LLM，让它说出这批文章里
   哪些**概念**值得单独成页，并点名哪些文章在谈它。实测一批 150 篇抽出 24 个
   概念（FDE / Rerank / 混合检索 / 认知负荷 / 餐饮食材周期…），质量可用。
2. **归属**（`assign`）：LLM 最多只点名 8 篇/概念，真实的成员数远不止 ——
   用**向量近邻**补全（概念质心 vs 文章画像），这是"库越来越大会自己长"的机制。
3. **织网**（`link`）：两个概念共享 ≥N 篇文章 → 候选边；再让 LLM 写清
   「这两个概念怎么关联」，页与页之间才真的有链接，而不是随口说的"相关话题"。

**为什么概念发现要分批**：全库 1020 篇的标题+摘要约 29 万字（≈14.5 万 token），
一次塞不进上下文。分批后不同批会抽出同一个概念（同一术语在多批出现），
所以合并按**归一化后的名字**（`normalize_name`）—— **不做向量判重**：
跨语言的同义（`Agent` ↔ `智能体`）向量抓不准，而合并错会把两个概念的
页面并成一个。宁可留两个各自分明的概念页。
"""

from __future__ import annotations

import hashlib
import pathlib
import json
import re
import time
import unicodedata
from typing import Callable

import numpy as np

from . import config, db, llm, wiki

# 分批大小：150 篇的标题+摘要约 2 万 token，一次调用 7 秒、约 0.02 元。
# 太大容易超上下文，太小则同一概念会在多批里重复出现、合并成本高。
DISCOVER_BATCH = 150

# 向量补全成员的相似度下限 —— **实测标定出来的，不是拍的**（2026-10-13，全库 1020 篇）：
#把 LLM 点名的文章当「已知正例」量了一下：
#     LLM 点名的文章   0.81 ~ 0.91
#     全库相似度分布   p50 = 0.71~0.76   p90 = 0.78~0.82
# 也就是说**任何概念对任何文章都有 0.7 以上**（都是同领域的中文文本，BGE-M3 拉不开）
# —— 阈值必须落在 p90 之上。0.50 会让每个概念都捞到 26+ 篇（实测踩过）。
ASSIGN_MIN_SIM = 0.80

# 每篇文章最多进几个概念。**必须设上限**：不给上限的话，一篇什么都谈的文章会
# 同时挂进十几个概念，把网织成一团毛线。与主题树的 TOPIC_TOP_N=2 同一思路。
ASSIGN_TOP_N = 3

# 两个概念成员 Jaccard 超过这个值才算候选边。
# **0.25 是重新标定过的**：成员表变大（挤位后平均 19 篇/概念）之后，0.12 会炸出
# 1524 条边（平均每概念 21 条），网糊成一团、LLM 一次也写不完说明（超长被截断，
# 结果所有边的 label 都是空的 —— 实测踩过）。实测标定：
#   0.12→1524 条 ｜ 0.20→556 ｜ **0.25→333 条（平均 4.6 条/概念）** ｜ 0.30→184
LINK_MIN_JACCARD = 0.25

# 每个概念最多连几条边。**必须有**：不加度数上限会出现 hub —— 一个宽泛的概念
# （如「AI原生组织」）连几十条边，读者点进去发现全是噪声，网的语义就废了。
LINK_MAX_DEGREE = 8

# **向量补全的增量上限**，不是「成员总数上限」—— 这两件事一开始被我混成一个，
# 结果裁剪时把 LLM 点名的成员也裁掉了 30 条（实测事故）。
#
# 正确的语义分工：
#   · LLM 点名的成员（role=confirmed）**永远保留** —— 它说「这些文章在谈这个概念」，
#     是知识库的客观事实，裁掉就是丢知识。实测「AI原生组织」被点名 31 篇。
#   · **向量补全**（role=suggested）才受这个上限约束 —— 否则每跑一次 sync
#     就往每个概念里灌一批，一个概念能涨到 291 篇（实测炸过）。
#   · **编译取材**取前 N 篇（按 weight 排序）—— 概念页要「聚焦」，
#     16 篇读起来最舒服；再多就成综述了，那正是话题页该干的活。
MAX_NEW_MEMBERS = 16
# 挤位迟滞量：新文章必须比最低分成员高出这么多才挤位（防质心微移导致的边界震荡）
ASSIGN_HYSTERESIS = 0.01

# 编译一页概念时最多取几篇素材
MAX_MEMBERS = 16

# 每批抽多少个概念。宁可少而实 —— 抽 24 个已经覆盖了技术/商业/认知三面。
CONCEPTS_PER_BATCH = 24

# 一个概念至少要有几篇文章在谈才值得单独成页。
# 1 篇撑不起「这个概念在库里被怎么谈」这种页面（那只是一篇文章的摘要），
# 而且会把概念的网撑脏 —— 页越多、边越乱，就越没人看得下去。
MIN_MEMBERS = 2


# ---------------------------------------------------------------- 名字归一

def normalize_name(name: str) -> str:
    """概念名归一化，用来判「这两个是不是同一个概念」。

    要处理的是真会出现的不一致：`RAG` / `Rag` / `rag`、`Agent` / `智能体`、
    `FDE 与对结果负责` / `FDE`、全角半角、前后空格。**不做跨语言同义合并**
    （`Agent` ↔ `智能体` 向量也抓不准，而合并错比不合并坏得多 ——
    会把两个不同概念的页面并成一个，知识页就毁了）。
    """
    s = unicodedata.normalize("NFKC", (name or "").strip())
    s = re.sub(r"\s+", " ", s)
    # 只做大小写与全半角这类**绝对安全**的归一；同义词一律交给向量判重
    return s


def name_key(name: str) -> str:
    """概念的判重键：归一化 + 转小写 + 分隔符统一。

    **这是「同一个概念的多种写法」的唯一依据**：LLM 在不同批次会把RAG 写成
    「RAG」「Rag」「R.A.G」，把智能体写成「智能体」「Agent」（后者**判不出**，
    跨语言的同义向量也抓不准，所以不在这里合并）。
    """
    s = normalize_name(name).lower()
    s = re.sub(r"[\s_·・]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def concept_slug(name: str) -> str:
    """概念 slug：英文数字直接小写用；纯中文名用**确定性**短哈希。

    三个坑都在这里踩过：

    1. **不能用 `hash()`** —— CPython 的 str hash 带随机种子（PYTHONHASHSEED），
       同一个中文概念名两次运行会得到不同 slug。slug 是页面身份，它一变，
       已分享的链接和已存的 wiki_pages 关联就全断了。必须用 sha1。
    2. **截断会撞车** —— 长 ascii 名截断后可能相同，撞上唯一约束（整批回滚）。
    3. **丢掉中文部分会撞车（更隐蔽）** —— slug 原本只取 ascii，于是
       「AI Native 组织」「AI Native 组织变革」「AI-Native 组织设计」
       三个不同概念**全都变成 `c-ai-native`**。所以规则是：
       **名字里只要有非 ASCII 字符，就必须带名字哈希** —— 那种情况下
       ascii 部分是不完整的、不足以区分身份。
    """
    s = normalize_name(name)
    h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]
    ascii_part = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    pure_ascii = all(ord(ch) < 128 for ch in s)
    if pure_ascii and ascii_part and len(ascii_part) <= 24:
        return "c-" + ascii_part               # 纯 ASCII 短名：本身就唯一，可读
    if ascii_part and len(ascii_part) >= 2:
        return "c-%s-%s" % (ascii_part[:18].rstrip("-"), h)
    return "c-cn-" + h


# ---------------------------------------------------------------- 概念发现

def _discover_batch(articles: list[dict], log: Callable[[str], None]) -> list[dict]:
    """让 LLM 从一批文章里抽概念。返回 [{name, definition, articles:[article_id]}]。"""
    material = "\n\n".join(
        "[%d] %s｜%s" % (i, a["title"] or "", (a["summary"] or "")[:260])
        for i, a in enumerate(articles, 1))
    prompt = config.PROMPT_DIR / "wiki_concepts_v1.md"
    system = (prompt.read_text(encoding="utf-8") if prompt.is_file()
              else "从素材里找出值得单独成页的概念，输出 json 格式。")
    # 提示词是**模板**，`{{material}}` 必须被真素材替换掉 —— 不替换就是把
    # 一份带占位符的说明书当 system 发了，模型会对着「{{material}}」凭空编。
    if "{{material}}" not in system:
        raise wiki.WikiBuildError(
            "prompts/wiki_concepts_v1.md 里没有 {{material}} 占位符 —— "
            "素材没地方插进去。")
    system = system.replace("{{material}}", material)
    obj, usage = llm.chat_json(system, material)
    got = _normalize_concepts(obj.get("concepts") or [], articles, log)
    # 按「点名了几篇文章」排序取前 N —— 点名多的才是真有语料支撑的概念。
    # **提示词只是建议，护栏必须在代码**：提示词里写了「15-25 个」，实测模型
    # 完全不遵守，有一批吐了 **284 个**；不封顶的话 7 批合计 465 个概念，
    # 直接冲出 Wiki 的可用区间（100~200 页）。
    got.sort(key=lambda c: -len(c["article_ids"]))
    kept = got[:CONCEPTS_PER_BATCH]
    if log:
        log("    本批 %d 篇 → 模型给 %d 个，留 %d 个（点名多的优先）"
            % (len(articles), len(got), len(kept)))
    return kept


def _normalize_concepts(raw: list, articles: list[dict], log: Callable[[str], None]) -> list[dict]:
    """把 LLM 返回的概念整理成 [{name, name_key, definition, article_ids}]。

    **必须做校验，不能照抄**：编号越界 / 名字为空 / 一篇都没点到，都要处理 ——
    概念页是知识页，出现一个指向不存在文章的编号，页面上就是死引用。

    `name_key` 走归一化（大小写、连字符、空格统一），用来判「这两个是不是同一个
    概念」：LLM 在不同批次会把同一个概念写成 `AI Agent` / `AI-Agent`，按原名判重
    会把它们分裂成两个几乎一样的节点，概念的网就多一条假边。
    """
    by_no = {i: a for i, a in enumerate(articles, 1)}
    out, index = [], {}
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = normalize_name(str(c.get("name") or ""))
        if not name or len(name) > 20:
            continue
        ids = []
        for x in (c.get("articles") or []):
            try:
                a = by_no.get(int(x))
            except (TypeError, ValueError):
                a = None
            if a and a["id"] not in ids:
                ids.append(a["id"])
        key = name_key(name)
        if key in index:
            prev = out[index[key]]
            prev["article_ids"] = list(dict.fromkeys(prev["article_ids"] + ids))
            if len(str(c.get("definition") or "")) > len(prev["definition"]):
                prev["definition"] = str(c.get("definition") or "").strip()
            continue
        index[key] = len(out)
        out.append({"name": name, "name_key": key,
                    "definition": str(c.get("definition") or "").strip(),
                    "article_ids": ids})
    return out


def discover(conn, *, batch: int = DISCOVER_BATCH, limit: int | None = None,
             log: Callable[[str], None] = print) -> dict[str, int]:
    """全库跑一遍概念发现，落库。返回统计。

    幂等：同一个概念名再次被发现时**合并成员**而不是新建一条
    （概念名有唯一索引，重复插入会直接报错 —— 那是刻意的护栏）。
    """
    rows = conn.execute(
        """SELECT a.id, a.title, a.summary FROM articles a
           WHERE a.status <> 'failed' AND length(coalesce(a.summary, '')) > 80
           ORDER BY a.collected_at DESC""" + (" LIMIT %d" % limit if limit else "")
    ).fetchall()
    articles = [dict(r) for r in rows]
    if not articles:
        raise wiki.WikiBuildError("库里没有摘要够长的文章，概念发现无从下手。")

    batches = [articles[i:i + batch] for i in range(0, len(articles), batch)]
    log("概念发现：%d 篇素材，分 %d 批（每批 %d 篇）" % (len(articles), len(batches), batch))
    found: dict[str, dict] = {}      # name_key -> 概念（跨批合并用）
    for bi, b in enumerate(batches, 1):
        log("  第 %d/%d 批…" % (bi, len(batches)))
        for c in _discover_batch(b, log):
            prev = found.get(c["name_key"])       # 按归一化名跨批合并
            if prev:
                prev["article_ids"] = list(dict.fromkeys(prev["article_ids"] + c["article_ids"]))
                if len(c["definition"]) > len(prev["definition"]):
                    prev["definition"] = c["definition"]
            else:
                found[c["name_key"]] = c
        time.sleep(0.5)          # 别把供应商打限流；发现阶段不着急

    log("\n共发现 %d 个不重复概念，落库…" % len(found))
    return _save_concepts(conn, list(found.values()), log)


def _save_concepts(conn, concepts: list[dict], log=print) -> dict:
    """落库概念与成员，**并算质心**（增量生长靠它判断新文章属于谁）。

    **只有 1 篇文章在谈的概念直接不落库**：Wiki 页面是「这个概念在库里被怎么谈」，
    一篇素材撑不起一页 —— 那样的页面只会是一篇文章的摘要，还会把概念的网撑脏。
    这个门槛是客观的（数得出来），不靠模型自觉。
    """
    stats = {"new": 0, "merged": 0, "members": 0,
             "dropped_no_member": 0, "dropped_too_few": 0}
    ready = []
    for c in concepts:
        ids = [a for a in c["article_ids"] if a]
        if not ids:
            stats["dropped_no_member"] += 1
            continue
        if len(ids) < MIN_MEMBERS:
            stats["dropped_too_few"] += 1
            continue
        ready.append((c, ids))
    if stats["dropped_too_few"]:
        log("  丢掉 %d 个「只有 1 篇在谈」的概念（撑不起一页）"
            % stats["dropped_too_few"])
    with db.tx() as w:
        now = db.now_iso()
        for c, ids in ready:
            exist = w.execute("SELECT id, slug FROM wiki_concepts WHERE name_key = ?",
                              (c["name_key"],)).fetchone()
            if exist:
                cid = exist["id"]
                stats["merged"] += 1
                # 定义取更长那个（更可能是写全了的那个），name 保留先出现的写法
                w.execute(
                    """UPDATE wiki_concepts
                       SET definition = CASE WHEN length(?) > length(coalesce(definition,''))
                                            THEN ? ELSE definition END,
                           updated_at = ?
                       WHERE id = ?""",
                    (c["definition"], c["definition"], now, cid))
            else:
                cur = w.execute(
                    """INSERT INTO wiki_concepts (slug, name, name_key, definition, role,
                                                 status, created_at, updated_at)
                       VALUES (?,?,?,?,'found','active',?,?)""",
                    (concept_slug(c["name"]), c["name"], c["name_key"],
                     c["definition"], now, now))
                cid = int(cur.lastrowid or 0)
                if not cid:
                    raise wiki.WikiBuildError("概念落库拿不到自增 id。")
                stats["new"] += 1
            for aid in ids:
                cur2 = w.execute(
                    """INSERT OR IGNORE INTO wiki_concept_articles
                       (concept_id, article_id, role, created_at) VALUES (?,?,'confirmed',?)""",
                    (cid, aid, now))
                if cur2.rowcount:
                    stats["members"] += 1
            _recompute(w, cid)
    return stats


def _recompute(w, concept_id: int) -> None:
    """重算某个概念的成员数与质心（成员可能变了，质心就得跟着变）。

    质心 = 成员文章画像的均值再L2 归一，**与话题质心算法一致**（structure.py 同款），
    这样新文章的向量能直接跟它比相似度。
    """
    rows = w.execute(
        """SELECT p.vec FROM wiki_concept_articles ca
           JOIN article_profiles p ON p.article_id = ca.article_id
           WHERE ca.concept_id = ?""", (concept_id,)).fetchall()
    vecs = []
    for r in rows:
        v = np.frombuffer(r["vec"], dtype=np.float32)
        if v.size == config.EMBED_DIM:
            vecs.append(v)
    if not vecs:
        return
    cen = np.mean(vecs, axis=0)
    n = np.linalg.norm(cen)
    if n > 0:
        cen = cen / n
    w.execute("UPDATE wiki_concepts SET article_count=?, centroid=?, updated_at=? WHERE id=?",
              (len(vecs), cen.astype(np.float32).tobytes(), db.now_iso(), concept_id))


# ---------------------------------------------------------------- 向量补全成员

def assign(conn, *, min_sim: float = ASSIGN_MIN_SIM, top_n: int = ASSIGN_TOP_N,
           max_members: int = MAX_MEMBERS, log: Callable[[str], None] = print) -> dict:
    """用向量近邻**补全概念成员**：LLM 只点名了它看到的那几篇，真实的成员更多。

    这是「库会长出知识」的关键：新文章进来时不必重新让 LLM 读一遍，
    只要跟概念质心算一次相似度，就能判断它该不该进这个概念的页面。

    **判定是转置的：以文章为基准选概念，不是以概念为基准选文章。**
    反过来（「每个概念取 top-N 篇」）实测会把每个概念都塞满 16 篇 ——
    因为全库 p50 相似度就有 0.75，每个概念都找得到 16 篇「够像」的。
    转置之后问的是「这篇文章最像哪几个概念」，答案天然收敛。

    LLM 点名的成员（role=confirmed）不被覆盖 —— 那是它读摘要之后的判断，
    比向量准；向量只负责「找漏掉的」。
    """
    log("向量补全成员（每篇最多 %d 个概念，相似度下限 %.2f）…" % (top_n, min_sim))
    prof = conn.execute(
        """SELECT p.article_id, p.vec FROM article_profiles p
           JOIN articles a ON a.id = p.article_id WHERE a.status <> 'failed'"""
    ).fetchall()
    if not prof:
        raise wiki.WikiBuildError("article_profiles 是空的，先跑 scripts/structure.py profiles")
    aids = [r["article_id"] for r in prof]
    X = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in prof]).copy()
    X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-12)

    concepts = conn.execute(
        "SELECT id, name, centroid FROM wiki_concepts WHERE status='active' AND centroid IS NOT NULL"
    ).fetchall()
    if not concepts:
        raise wiki.WikiBuildError("wiki_concepts 是空的，先跑 `wiki_build.py concepts discover`。")

    # 概念质心矩阵。**每个质心必须重新归一**：从 sqlite 读出来的 blob 是只读的，
    # 直接 /= 会报 "output array is read-only"（实测踩过）。
    cs = []
    for c in concepts:
        v = np.frombuffer(c["centroid"], dtype=np.float32).astype(np.float32, copy=True)
        if v.size != config.EMBED_DIM:
            continue
        cs.append(v / max(float(np.linalg.norm(v)), 1e-12))
    cids = [c["id"] for c in concepts]
    sims = X @ np.vstack(cs).T# [文章数 x 概念数]

    # 已有成员：分 confirmed（LLM 点名的，永不动）与 suggested（向量补的，可挤位）
    existing = {}
    by_sim = {}
    for r in conn.execute("SELECT concept_id, article_id, role, sim FROM wiki_concept_articles"):
        existing.setdefault(r["concept_id"], set()).add(r["article_id"])
        if r["role"] != "confirmed":
            by_sim.setdefault(r["concept_id"], {})[r["article_id"]] = float(r["sim"] or 0.0)

    now = db.now_iso()
    added = kicked = 0
    with db.tx() as w:
        for i, aid in enumerate(aids):
            for j in np.argsort(-sims[i])[:top_n]:
                score = float(sims[i][j])
                if score < min_sim:
                    break
                cid = cids[j]
                if aid in existing.get(cid, ()):
                    continue
                # **动态挤位**：名额满了就拿「这个概念里分数最低的那个向量成员」比一下。
                # 新文章更相关 → 挤掉它；不如它 → 不加。
                # 这样页面里的向量成员**永远是当下最相关的那几篇**，
                # 而不是「先到先得」留下的早期低分成员（这是纯名额制的真问题）。
                pool = by_sim.get(cid) or {}
                if len(pool) >= max_members:
                    worst_aid, worst_sim = min(pool.items(), key=lambda kv: kv[1])
                    # **迟滞量**：要高出最低分 0.01 才算「明显更相关」。
                    # 没有它的话：挤一条 → 质心重算 → 全库分数微移 → 边界文章又翻转
                    # → 下次 assign 凭空再挤几百条（实测 0 篇新文章仍挤 231 条），
                    # pending 永远在涨，人会以为又有新文章而重复花 token。
                    if score <= worst_sim + ASSIGN_HYSTERESIS:
                        continue
                    w.execute("DELETE FROM wiki_concept_articles "
                              "WHERE concept_id=? AND article_id=?", (cid, worst_aid))
                    pool.pop(worst_aid, None)
                    kicked += 1
                w.execute(
                    """INSERT OR REPLACE INTO wiki_concept_articles
                       (concept_id, article_id, sim, role, created_at)
                       VALUES (?,?,?,'suggested',?)""",
                    (cid, aid, score, now))
                existing.setdefault(cid, set()).add(aid)
                pool[aid] = score
                added += 1
                # 成员变了 → 这一页该重新编译。**挤位也必须记 pending**：
                # 不记的话 sync 会说「0 个待更新」，而页面里其实已经换过成员了
                # （成员换了、正文没换 = 页面与成员表不一致）。
                w.execute("UPDATE wiki_concepts SET pending = pending + 1 WHERE id = ?",
                          (cid,))
        for cid in cids:
            _recompute(w, cid)
    log("补进 %d 条成员关系%s。"
        % (added, "，其中 %d 条挤掉了分数更低的旧成员" % kicked if kicked else ""))
    return {"added": added, "kicked": kicked, "concepts": len(cids)}


# ---------------------------------------------------------------- 织网

def link(conn, *, min_jaccard: float = LINK_MIN_JACCARD,
         max_degree: int = LINK_MAX_DEGREE,
         log: Callable[[str], None] = print) -> dict:
    """概念互链：共享文章的成员 → LLM 写清「这两个概念怎么关联」。

    **与话题页的 links 有本质区别**：那边是模型随口说的「相关话题」，
    那边没有依据；这里的每条边都必须回答「它们因为什么相关」——
    候选边来自成员重叠（客观、可复算），关系说明来自模型（人话）。
    """
    concepts = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, definition, article_count FROM wiki_concepts WHERE status='active'")}
    members = {cid: {r["article_id"] for r in conn.execute(
        "SELECT article_id FROM wiki_concept_articles WHERE concept_id=?", (cid,))}
        for cid in concepts}
    edges = []
    for a in concepts:
        for b in concepts:
            if a >= b:
                continue
            inter = members[a] & members[b]
            if not inter:
                continue
            j = len(inter) / max(1, len(members[a] | members[b]))
            if j >= min_jaccard:
                edges.append((a, b, len(inter), j))
    edges.sort(key=lambda e: -e[2])
    # **度数上限**：按共享篇数从多到少收，每个概念最多连 max_degree 条。
    # 不加的话宽泛概念会连几十条边（hub），网的语义就废了。
    degree = {}
    kept = []
    for e in edges:
        a, b = e[0], e[1]
        if degree.get(a, 0) >= max_degree or degree.get(b, 0) >= max_degree:
            continue
        kept.append(e)
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    if len(kept) < len(edges):
        log("  去掉 %d 条边（每个概念最多连 %d 条，避免 hub）"
            % (len(edges) - len(kept), max_degree))
    edges = kept
    log("候选边 %d 条（成员 Jaccard ≥ %.2f）" % (len(edges), min_jaccard))
    if not edges:
        return {"edges": 0, "links": 0}

    # 一次调用写全部关系说明（省 token：概念名+定义很短）
    desc = _describe_links(edges, concepts, log)
    now = db.now_iso()
    n = 0
    with db.tx() as w:
        # **先清概念页的全部出边再写**，出边只认本次候选边这一种来源：
        # ① 编译时模型随口写的「相关话题」边是幻觉（㉕实测：FDE → 道家思想，毫无关系），
        #    每次编译都会重新长出来；② 上轮织网的边，本轮成员变了就不再成立。
        # 只删概念页（concept_id 非空）的出边，话题页的 wiki_links 不受影响。
        # （㉓ 那次是临时 SQL 清的，没进代码，本轮 145 页编译后残留 115 条幻觉边。）
        w.execute("DELETE FROM wiki_links WHERE src_page_id IN "
                  "(SELECT id FROM wiki_pages WHERE concept_id IS NOT NULL)")
        for (a, b, shared, j), text in zip(edges, desc):
            for src, dst in ((a, b), (b, a)):
                page = w.execute("SELECT id FROM wiki_pages WHERE concept_id=?",
                                 (src,)).fetchone()
                other = w.execute("SELECT slug FROM wiki_concepts WHERE id=?", (dst,)).fetchone()
                if not page or not other:
                    continue          # 源或目标还没编译出页面 → 先记不着
                w.execute(
                    """INSERT OR REPLACE INTO wiki_links (src_page_id, dst_slug, label)
                       VALUES (?,?,?)""", (page["id"], other["slug"], text))
                n += 1
        # **必须用事务连接 w 统计**，不能用外层只读 conn ——
        # 外层连接看不到本事务里刚插入的边，count 会全是 0
        # （实测：织出284 条边、568 条记录，页面「互链数」却全是 0）。
        for cid in concepts:
            cnt = w.execute(
                """SELECT COUNT(*) n FROM wiki_links l JOIN wiki_pages p ON p.id = l.src_page_id
                   WHERE p.concept_id = ?""", (cid,)).fetchone()["n"]
            w.execute("UPDATE wiki_concepts SET link_count=?, updated_at=? WHERE id=?",
                      (cnt, now, cid))
    log("写入互链 %d 条（无向，每条存两次）" % n)
    return {"edges": len(edges), "links": n}


def _describe_links(edges, concepts, log=print, batch: int = 30) -> list[str]:
    """让 LLM 为每条候选边写一句关系说明。失败就返回等长的空列表（边仍然建，只是没有说明）。

    **必须分批**：333 条边一次发出去，模型光输出就超长（实测 finish_reason=length，
    结果一条说明都没生成）。按 batch 条一批，最后拼齐。
    """
    out: list[str] = []
    for i in range(0, len(edges), batch):
        part = edges[i:i + batch]
        out.extend(_describe_one_batch(part, concepts, log, i))
    return out


def _describe_one_batch(edges, concepts, log, offset=0) -> list[str]:
    lines = []
    for i, (a, b, shared, j) in enumerate(edges, offset + 1):
        ca, cb = concepts[a], concepts[b]
        lines.append("%d. 概念A「%s」（%s）｜概念B「%s」（%s）｜共有文章 %d 篇"
                     % (i, ca["name"], (ca["definition"] or "")[:40],
                        cb["name"], (cb["definition"] or "")[:40], shared))
    try:
        obj, usage = llm.chat_json(
            "你给概念之间的关系写一句话说明。输出 json 格式：{\"relations\":[\"一句话\"]}，"
            "数组长度必须与输入行数相同。只说素材能支撑的关系，不要编造因果。"
            "\n\n".join(lines),
            "逐行写：这两个概念为什么相关（因为谈同一件事 / 因为互为前提 / 因为一个引出另一个…）。"
            "每句 25 字内，不重复原文定义。")
        rs = obj.get("relations") or []
        if len(rs) != len(edges):
            log("  [警告] 模型给了 %d 条说明、候选边有 %d 条 —— 说明留空" % (len(rs), len(edges)))
            return [""] * len(edges)
        return [str(x).strip()[:80] for x in rs]
    except llm.LLMError as e:
        log("  [警告] 关系说明没生成：%s（边仍然建立）" % str(e).split("\n")[0])
        return [""] * len(edges)


# ---------------------------------------------------------------- 增量生长

def scan_growth(conn, *, min_sim: float = ASSIGN_MIN_SIM,
                log: Callable[[str], None] = print) -> dict:
    """扫描**入库晚于上次编译**的文章，把命中的概念的 pending 加一。

    这是 Karpathy 说的「复利」在工程上的落点：新文章不会自动编译成页
    （那要花钱、也会把已读好的页冲掉），而是**把受影响的页标记为待更新**，
    由人（或者一个命令）在合适的时候重编译 —— 知识因此持续累积，而不是每次推倒重来。
    """
    log("扫描新文章…")
    rows = conn.execute(
        """SELECT p.article_id, p.vec, a.collected_at FROM article_profiles p
           JOIN articles a ON a.id = p.article_id
           WHERE a.status <> 'failed' ORDER BY a.collected_at""").fetchall()
    profiles = {r["article_id"]: (np.frombuffer(r["vec"], dtype=np.float32), r["collected_at"])
                for r in rows}
    concepts = conn.execute(
        """SELECT id, name, centroid, compiled_at FROM wiki_concepts
           WHERE status='active' AND centroid IS NOT NULL""").fetchall()

    hit = {}
    got_by_vector = {}
    for r in conn.execute(
            """SELECT concept_id, COUNT(*) n FROM wiki_concept_articles
               WHERE role <> 'confirmed' GROUP BY concept_id"""):
        got_by_vector[r["concept_id"]] = r["n"]

    with db.tx() as w:
        for c in concepts:
            cen = np.frombuffer(c["centroid"], dtype=np.float32)
            if cen.size != config.EMBED_DIM:
                continue
            cen = cen / max(float(np.linalg.norm(cen)), 1e-12)
            n_pending = 0
            for aid, (vec, collected) in profiles.items():
                # **未编译过的概念不标记待更新**：它还没有页面，「待更新」没有对象。
                # （漏这条会让首次 sync 把 145 个概念全标脏—— 实测踩过：
                # compiled_at 为空 → 全部文章都算"编译后新增"。）
                if not c["compiled_at"]:
                    continue
                if collected and collected <= c["compiled_at"]:
                    continue          # 编译时已包含
                v = vec / max(float(np.linalg.norm(vec)), 1e-12)
                if float(v @ cen) >= min_sim:
                    # 只在「还没进这个概念」时才计数
                    seen = w.execute(
                        """SELECT 1 FROM wiki_concept_articles
                           WHERE concept_id=? AND article_id=?""",
                        (c["id"], aid)).fetchone()
                    if not seen:
                        w.execute(
                            """INSERT OR IGNORE INTO wiki_concept_articles
                               (concept_id, article_id, sim, role, created_at)
                               VALUES (?,?,?,?,?)""",
                            (c["id"], aid, float(v @ cen), "suggested", db.now_iso()))
                        n_pending += 1
            # **这里不做名额限制**：名额该由 assign 的挤位逻辑管（比分数、挤掉更差的），
            # grow 只负责「把新文章记进来 + 标记页面待更新」。真正的宽度约束在 assign：
            # 向量成员永远不超过 MAX_NEW_MEMBERS，且留下的都是当下分数最高的那些。
            # （曾经在这里写死「满了就跳过」，结果 0.9 分的新文章被 0.80 的老成员挡在门外；
            #  而完全不限制又会炸 —— 291 篇那次事故就是漏了这行。）
            if n_pending:
                w.execute("UPDATE wiki_concepts SET pending=? WHERE id=?", (n_pending, c["id"]))
                hit[c["name"]] = n_pending
        # 质心变了（新成员进来），要重算，否则下次扫描还是老质心
        for c in concepts:
            _recompute(w, c["id"])
    if hit:
        log("待更新的概念 %d 个：" % len(hit))
        for k, v in sorted(hit.items(), key=lambda x: -x[1]):
            log("  %-16s +%d 篇" % (k, v))
    else:
        log("没有待更新的概念。")
    return {"pending_concepts": len(hit), "detail": hit}


def concept_rows(conn) -> list[dict]:
    """概念清单（导航用）。带编译状态与互链度数。"""
    return [dict(r) for r in conn.execute(
        """SELECT c.id, c.slug, c.name, c.topic_id, c.definition, c.article_count,
                  c.link_count, c.pending, c.compiled_at, c.role,
                  (SELECT p.id FROM wiki_pages p WHERE p.concept_id = c.id) AS page_id
           FROM wiki_concepts c WHERE c.status='active'
           ORDER BY c.article_count DESC, c.name""")]


def graph(conn) -> dict:
    """概念网：节点 + 边，前端画网用。

    **只给已编译的概念当节点**：没编译的概念没有页面，进网之后点不开 ——
    一张大部分节点点不动的网比没有网更让人困惑。
    """
    nodes = [dict(r) for r in conn.execute(
        """SELECT c.id, c.slug, c.name, c.definition, c.article_count, c.link_count,
                  p.slug AS page_slug
           FROM wiki_concepts c JOIN wiki_pages p ON p.concept_id = c.id
           WHERE c.status='active'
           -- **必须有 ORDER BY**：前端力导向的初始位置按节点下标算，
           -- 顺序一变（重建数据后 SQLite 的返回顺序可能变）整个布局就跟着变，
           -- 用户每次看到的网都不一样、没法形成空间记忆。
           ORDER BY c.id""")]
    by_page = {n["page_slug"]: n["id"] for n in nodes}
    seen, uniq = set(), []
    for r in conn.execute(
            """SELECT p.concept_id, l.dst_slug, l.label
               FROM wiki_links l JOIN wiki_pages p ON p.id = l.src_page_id
               WHERE p.concept_id IS NOT NULL
               ORDER BY p.concept_id, l.dst_slug"""):
        dst = by_page.get(r["dst_slug"])
        if not dst or r["concept_id"] is None or dst == r["concept_id"]:
            continue
        k = (min(r["concept_id"], dst), max(r["concept_id"], dst))
        if k in seen:
            continue
        seen.add(k)
        uniq.append({"source": r["concept_id"], "target": dst, "label": r["label"] or ""})
    return {"nodes": nodes, "edges": uniq}


def concept_page_detail(conn, slug: str) -> dict | None:
    """一个概念页的完整数据：概念元信息 + 页面正文 + 引用 + 互链。"""
    page = conn.execute("SELECT * FROM wiki_pages WHERE slug = ?", (slug,)).fetchone()
    if not page:
        return None
    d = wiki.page_detail(conn, slug)
    if d is None:                     # page_detail 与本页查的是同一行，走不到这
        return None
    if page["concept_id"]:
        c = conn.execute(
            """SELECT id, name, topic_id, definition, article_count, link_count,
                      pending, compiled_at, name_key
               FROM wiki_concepts WHERE id = ?""", (page["concept_id"],)).fetchone()
        if c:
            c = dict(c)
            c["topic_name"] = (conn.execute("SELECT name FROM themes WHERE id = ?",
                                            (c["topic_id"],)).fetchone() or {"name": ""})["name"]
        d["concept"] = c or None
    else:
        d["concept"] = None
    # 互链：出边 + 入边，都要（页面底部「相关概念」要把指向本页的也列出来）
    out = []
    for r in conn.execute("SELECT dst_slug, label FROM wiki_links WHERE src_page_id = ?",
                          (page["id"],)):
        tgt = conn.execute(
            """SELECT c.name, c.id, p.slug FROM wiki_links l
               JOIN wiki_pages p ON p.slug = l.dst_slug
               JOIN wiki_concepts c ON c.id = p.concept_id
               WHERE l.src_page_id = ?""", (page["id"],)).fetchone()
        if tgt:
            out.append({"name": tgt["name"], "slug": tgt["slug"], "label": r["label"] or "",
                        "concept_id": tgt["id"]})
    for r in conn.execute(
            """SELECT l.src_page_id, l.label FROM wiki_links l
               JOIN wiki_pages p ON p.id = l.src_page_id
               WHERE l.dst_slug = ? AND p.concept_id IS NOT NULL""", (slug,)):
        sp = conn.execute(
            """SELECT c.name, p.slug FROM wiki_pages p JOIN wiki_concepts c ON c.id = p.concept_id
               WHERE p.id = ?""", (r["src_page_id"],)).fetchone()
        if sp:
            out.append({"name": sp["name"], "slug": sp["slug"], "label": r["label"] or "",
                        "concept_id": None})
    seen, uniq = set(), []
    for x in out:
        if x["slug"] in seen:
            continue
        seen.add(x["slug"])
        uniq.append(x)
    d["related"] = uniq
    return d
# ---------------------------------------------------------------- 概念页编译

def load_prompt(path: str | None, default_name: str) -> tuple[str, str]:
    """读外置提示词，返回 (正文, 版本标识 '文件名#内容指纹')。

    指纹用**内容**算而不是文件修改时间：时间会变（重装、git checkout 都会动），
    内容不会。页面要能回答「这一页是哪一版提示词编译的」，靠的是内容本身。
    """
    p = pathlib.Path(path) if path else (config.PROMPT_DIR / default_name)
    if not p.is_file():
        raise wiki.WikiBuildError("找不到提示词文件：%s" % p)
    body = p.read_text(encoding="utf-8")
    tag = hashlib.sha1(body.encode("utf-8")).hexdigest()[:8]
    return body, "%s#%s" % (p.name, tag)


def build_page(conn, concept_id: int, *, dry_run: bool = False,
               max_chars: int | None = None, prompt_path: str | None = None,
               log: Callable[[str], None] = print) -> dict:
    """编译一个概念的知识页。流程与话题页一致（取材→提示词→LLM→校验→写库），
    只是**取材来源不同**：话题页按 article_themes 取，概念页按 wiki_concept_articles 取。

    这就是两种形态的分工：
      概念页「Rerank」 —— 库里所有谈 Rerank 的文章，围绕**一个概念**织起来
      话题页「RAG 与 GraphRAG 实践」 —— 某话题下所有文章，围绕**一批素材**归纳
    """
    row = conn.execute(
        "SELECT id, slug, name, definition, article_count FROM wiki_concepts WHERE id = ?",
        (concept_id,)).fetchone()
    if row is None:
        raise wiki.WikiBuildError("没有 id=%s 的概念。" % concept_id)

    rows = [dict(r) for r in conn.execute(
        """SELECT a.id, a.title, a.summary, a.source, a.published_at, a.category, a.tags,
                  COALESCE(ca.sim, 0) AS weight, ca.role
           FROM wiki_concept_articles ca JOIN articles a ON a.id = ca.article_id
           WHERE ca.concept_id = ? AND a.status <> 'failed'
           ORDER BY (ca.role = 'confirmed') DESC, ca.sim DESC""", (concept_id,))]
    if not rows:
        raise wiki.WikiBuildError("概念「%s」还没有成员文章，先跑 concepts assign。" % row["name"])

    # **成员表是完整的，页面是聚焦的**（这两件事必须分开，见 MAX_NEW_MEMBERS 的说明）：
    # 一个概念可能有 45 篇在谈它（LLM 点了 31 篇 + 向量补了 16 篇），
    # 但一页读 45 篇就变回综述了。这里按「confirmed 优先、其次分数高」取前N 篇，
    # 剩下的在页面头部以「共 N 篇在谈」如实报出来，不假装只有 16 篇。
    total_members = len(rows)
    if total_members > MAX_MEMBERS:
        conf = [r for r in rows if r["role"] == "confirmed"]
        rest = [r for r in rows if r["role"] != "confirmed"]
        if len(conf) >= MAX_MEMBERS:
            rows = conf[:MAX_MEMBERS]
        else:
            rows = conf + rest[:MAX_MEMBERS - len(conf)]

    blocks, dropped, total = wiki.material_from_rows(conn, rows, max_chars=max_chars)
    if not blocks:
        raise wiki.WikiBuildError("概念「%s」的成员都取不到正文。" % row["name"])

    template, prompt_version = load_prompt(prompt_path, "wiki_compile_v1.md")
    log("概念：%s（%d 位成员，入选素材 %d 篇）"
        % (row["name"], row["article_count"], len(blocks)))
    log("素材：%d 篇 / %d 字；被截掉 %d 篇" % (len(blocks), total, len(dropped)))
    for d in dropped:
        log("  [截断] %s %s（%d 字）—— %s" % (d["id"], d["title"][:28], d["chars"], d["reason"]))

    user = (template
            .replace("{{topic}}", row["name"])
            .replace("{{topic_summary}}", row["definition"] or "（无）")
            .replace("{{n_articles}}", str(len(blocks)))
            .replace("{{material}}", "\n\n".join(b["text"] for b in blocks)))

    if dry_run:
        return {"slug": row["slug"], "concept": row["name"], "dry_run": True,
                "source_count": len(blocks), "chars": total,
                "dropped": [d["id"] for d in dropped], "prompt_chars": len(user),
                "prompt_version": prompt_version, "model": config.LLM_MODEL,
                "content_md": "", "refs": [], "usage": {}}

    reason = llm.missing_reason()
    if reason:
        raise wiki.WikiBuildError(reason + "\n（也可以先用 --dry-run 看取材质量，不调模型。）")

    log("调用模型 %s …（素材 %d 字，约 %d token）"
        % (config.LLM_MODEL, total, total // 2))
    obj, usage = llm.chat_json(
        system=("你是一个知识编译器：把给定的文章素材编译成一篇结构化的中文知识页。"
                "只使用素材内的信息，每条具体论断都要挂 [Rn] 角标，输出严格的 JSON。"),
        user=user)
    page = wiki._validate(obj, blocks, log=log)

    md = "\n\n".join("## %s\n\n%s" % (s["heading"], s["body"].strip())
                     for s in page["sections"])
    result = {"slug": row["slug"], "topic": row["name"], "concept_id": row["id"],
              "dry_run": False, "title": page["title"], "summary": page["summary"],
              "source_count": len(blocks), "chars": total,
              "members_total": total_members,
              "dropped": [d["id"] for d in dropped],
              "content_md": md, "refs": page["refs"], "links": page["links"],
              "warnings": page["warnings"], "usage": usage,
              "prompt_version": prompt_version, "model": config.LLM_MODEL}
    # 概念页**没有话题**（concept_id 才是它的归属）。传 {"id": None} 而不是 row ——
    # row 的 id 是 wiki_concepts.id，直接传会被 _persist 写成 topic_id，
    # 于是这个概念页挂到「同号的那个话题」名下（实测踩过：c-rerank 挂到了 topic 31）。
    wiki._persist({"id": None}, result)
    with db.tx() as w:
        w.execute("UPDATE wiki_concepts SET compiled_at=?, pending=0 WHERE id=?",
                  (db.now_iso(), row["id"]))
    log("已落盘：%s（引用 %d 篇）" % (row["slug"], len(page["refs"])))
    return result

# ---------------------------------------------------------------- 归属话题

def attach_topics(conn, *, log: Callable[[str], None] = print) -> dict:
    """把每个概念归到一个话题（概念质心 vs 39 个话题质心，取最高）。

    **为什么必须有这一步**：概念是横跨素材的术语（Rerank 出现在 AI 技术和企业
    AI 两个领域的文章里），但用户要按「几个大概方向」浏览库 —— 概念必须落到
    方向里才找得到。归属只看质心相似度，LLM 不参与（它的点名是"哪些文章在谈"，
    不是"这个概念属于哪个方向"，两回事）。

    每次成员变化后质心会重算，归属可能变 —— 所以这是个可重复跑的命令。
    """
    topics = conn.execute(
        """SELECT id, name, centroid FROM themes
           WHERE status='active' AND level=3 AND centroid IS NOT NULL""").fetchall()
    if not topics:
        raise wiki.WikiBuildError("themes 里没有话题质心，先跑 scripts/structure.py apply")
    concepts = conn.execute(
        "SELECT id, name, centroid FROM wiki_concepts WHERE status='active' AND centroid IS NOT NULL"
    ).fetchall()
    if not concepts:
        return {"attached": 0}

    tc = []
    for t in topics:
        v = np.frombuffer(t["centroid"], dtype=np.float32).astype(np.float32, copy=True)
        if v.size != config.EMBED_DIM:
            continue
        tc.append((t["id"], t["name"], v / max(float(np.linalg.norm(v)), 1e-12)))

    changed = 0
    now = db.now_iso()
    with db.tx() as w:
        for k in concepts:
            v = np.frombuffer(k["centroid"], dtype=np.float32).astype(np.float32, copy=True)
            if v.size != config.EMBED_DIM:
                continue
            v /= max(float(np.linalg.norm(v)), 1e-12)
            best = max(tc, key=lambda t: float(v @ t[2]))
            w.execute("UPDATE wiki_concepts SET topic_id=?, updated_at=? WHERE id=?",
                      (best[0], now, k["id"]))
            changed += 1
    log("归属完成：%d 个概念 → %d 个话题" % (changed, len(tc)))
    return {"attached": changed, "topics": len(tc)}
