# -*- coding: utf-8 -*-
"""知识结构：语义目录 + 文章挂载（PRD §5.7）。

为什么是「目录」而不是「划分」
--------------------------------
第一版试过把 752 篇互斥地切成 20 簇（KMeans），失败的原因是形态选错：

· **划分**回答「这篇属于哪一堆」，靠相似度切边界。而库里的主题本来就是连续谱
  （战略↔AI↔组织互相渗透），边界必然模糊 —— 实测同一件事被切成 3 块、
  121 篇的大筐里塞了四种不相干的内容，**看完看不到逻辑**。
· **目录**回答「这库里有哪些主题、谁包含谁」，靠语义从属：领域 > 板块 > 话题。
  上下级是包含关系而非相似关系，不需要切边界，所以天然有层次。

所以这里的分工是：
· **层级与命名靠 Agent/人工**（catalog.json，56 个节点）—— 这部分要读懂内容，
  不是算法能给的；
· **文章归属靠向量**（话题向量 = 话题名 + 说明 + 关键词 编码）—— 一篇可挂多个
  话题，不互斥。边界模糊这个问题因此不存在：没有任何地方需要「切一刀」。

第一版试过的两条路都留了痕，结论见 PROJECT.md §4 与 TESTS.md §9：
聚类划分（average linkage 链式合并 601/752 → 换 KMeans 仍不均衡）、
以及「标题+摘要+标签+概念+正文」单独编码的画像（距离挤在 0.42–0.51，轮廓 0.047，
根因之一是标签碎片化：实测 2148 个 tag / 752 篇）。

明确不做的事：不重判 `category`、不迁移任何已有数据、不动原文与 chunk 级向量索引。
主题表全是**派生数据**：原文与向量都在，随时可整表删除重建。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from . import config, db, embed

# 画像构造规则也算版本的一部分：改了聚合方式或回退路径，
# 旧向量与新向量就不可比，必须整表重算（PROFILE_MODEL_ID 变了会自动触发）。
PROFILE_MODEL_ID = "bge-m3/core3-v1"

# 画像怎么从 chunk 向量里来：
#   all   = 全部 chunk 的均值。稳，但把一篇文章的多个主题平均掉了 —— 用它做
#           细粒度话题归属时准确率只有约 67%（实测：「规模化踩坑」拿到的代表篇
#           是路线图、「制造业」拿到的是白皮书）。
#   core3 = 先用全量均值定方向，再取最贴该方向的那 3 个 chunk 求均值 ——
#           相当于「用文章的主干段落代表文章」，保留主旨、去掉边缘段落。
PROFILE_MODE = "core3"
CORE_CHUNKS = 3

BODY_PARAS = 2                 # 回退路径：原文开头取几段
BODY_CHARS = 300               # 每段截多少字
TAGS_IN_PROFILE = 8
CONCEPTS_IN_PROFILE = 10

# 挂载规则：一篇最多挂几个话题、相似度下限多高。
# 为什么设下限：话题向量是「话题名+说明+关键词」的文本，和文章正文的向量分布
# 不同（前者短、后者长），相似度整体偏高（实测 best 相似度中位数 0.67）——
# 不设下限会把每篇文章都硬塞进 TOP_N 个话题，看起来热闹但没有信息量。
TOPIC_TOP_N = 2
TOPIC_MIN_SIM = 0.60

# 伪相关反馈：文本话题向量只用来粗拉候选，话题的真实向量由候选文章投票决定。
# 为什么必须两阶段（实测）：只用文本向量时，话题区分度不够 —— 话题说明写的是
# 通用词（「落地」「规模化」「价值」），几乎沾边所有同域文章，于是「规模化瓶颈与
# 踩坑」这类覆盖面广的话题会吸走 34% 的库。改成用最像它的 20 篇来定义它之后，
# 话题才真正「由它自己的文章定义」。
COARSE_K = 20

TOP_ARTICLES_PER_TOPIC = 3     # 话题卡上展示几篇代表


def _l2(mat: np.ndarray) -> np.ndarray:
    """按行 L2 归一化。cosine 相似度 = 归一化后的点积。"""
    n = np.linalg.norm(mat, axis=-1, keepdims=True)
    np.maximum(n, 1e-12, out=n)
    return mat / n


# ---------------------------------------------------------------- 取素材

def _article_meta(conn) -> dict:
    return {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, category, title, summary, published_at, collected_at "
        "FROM articles")}


def _tags_of(row) -> list:
    return [t.strip() for t in db.jloads(row["tags"], [])
            if isinstance(t, str) and t.strip()]


def _words_of(conn) -> dict:
    """{article_id: [标签…, 概念名…]} —— 目录关键词的抓手（概念层同义重复严重，
    这里只用来观察，不参与向量计算）。"""
    out = {}
    for r in conn.execute("SELECT id, tags FROM articles"):
        out[r["id"]] = _tags_of(r)
    for r in conn.execute("SELECT article_id, name FROM concepts"):
        if r["name"]:
            out.setdefault(r["article_id"], []).append(r["name"].strip())
    return out


def _body_of(row) -> str:
    """回退路径用的正文：原文开头 BODY_PARAS*BODY_CHARS 字（无 chunks 的文章）。"""
    if not row["raw_path"]:
        return ""
    try:
        return (config.DATA_DIR / row["raw_path"]).read_text(
            encoding="utf-8")[:BODY_PARAS * BODY_CHARS].strip()
    except OSError:
        return ""      # 原文缺失：画像会短，但不该中断全库


def _profile_of(chunks: list) -> np.ndarray:
    """把一篇的 chunk 向量压成一个画像向量（见 PROFILE_MODE 的取舍说明）。"""
    M = np.vstack(chunks)
    if PROFILE_MODE == "all" or len(chunks) <= CORE_CHUNKS:
        return _l2(M.mean(axis=0, keepdims=True))[0]
    cen = _l2(M.mean(axis=0, keepdims=True))[0]
    top = np.argsort(-(M @ cen))[:CORE_CHUNKS]
    return _l2(M[top].mean(axis=0, keepdims=True))[0]


def profile_text(title: str, summary: str, tags: list, body: str,
                  concepts: list) -> str:
    """回退路径专用：给**没有 chunks 的文章**编码（retrieval=fulltext 的分类）。

    主路径不用它 —— 主路径是 chunk 向量均值，见 build_profiles 的说明。
    """
    parts = [title or "", summary or ""]
    if tags:
        parts.append("关键词：" + "、".join(tags[:TAGS_IN_PROFILE]))
    if concepts:
        parts.append("概念：" + "、".join(concepts[:CONCEPTS_IN_PROFILE]))
    if body:
        parts.append(body)
    return "\n".join(p for p in parts if p).strip()


# ---------------------------------------------------------------- 画像向量

def build_profiles(conn, force: bool = False, log=print) -> dict:
    """算 / 补齐 `article_profiles`。

    **主路径 = 该文章所有 chunk 向量的均值**，而不是给每篇单独编码一次。两条原因，
    都是实测踩出来的：

    ① 零编码成本：chunk 向量早就算好了（14598 个），752 篇的聚合是毫秒级；
       逐篇编码要 500 秒（8.3 分钟实测）。
    ② 效果更好：把「标题+摘要+标签+概念+正文」拼起来单独编码出的画像，两两距离
       挤在 0.42–0.51（区分度低），轮廓 0.047；换成 chunk 均值后距离摊到
       0.21–0.31（区分度高），簇语义明显更干净。
       原因之一是 tags 已碎片化（实测 2148 个 tag / 752 篇，每个 tag 平均只覆盖
       0.35 篇），把它们塞进画像等于往每个向量里掺噪声。

    **没有 chunks 的文章走回退**：retrieval=fulltext 的分类本来就不建索引，
    直接拿原文开头编码（库里只有几十篇，成本可忽略）。

    增量：只算库里没有的、或 model_id 不匹配的（换聚合方式必须全部重算，
    model_id 就是为此存在的）。算出空向量的篇目跳过，绝不拿零向量污染。
    """
    have = {} if force else {
        r["article_id"]: r["model_id"]
        for r in conn.execute("SELECT article_id, model_id FROM article_profiles")}

    vecs: dict = {}
    for r in conn.execute("SELECT chunk_id, embedding FROM chunk_vecs"):
        vecs[int(r["chunk_id"])] = np.frombuffer(r["embedding"], dtype=np.float32)
    agg: dict = {}
    for r in conn.execute("SELECT article_id, id FROM chunks ORDER BY article_id, ordinal"):
        v = vecs.get(int(r["id"]))
        if v is not None:
            agg.setdefault(r["article_id"], []).append(v)

    todo = {}          # article_id -> vec (1d)
    for aid, vs in agg.items():
        if have.get(aid) != PROFILE_MODEL_ID:
            todo[aid] = _profile_of(vs)

    words = _words_of(conn)
    fb_ids, fb_texts = [], []
    for r in conn.execute(
            "SELECT id, title, summary, tags, raw_path FROM articles "
            "WHERE status <> 'failed' ORDER BY id"):
        if r["id"] in agg or have.get(r["id"]) == PROFILE_MODEL_ID:
            continue
        text = profile_text(r["title"], r["summary"], _tags_of(r),
                            _body_of(r), words.get(r["id"], []))
        if text:
            fb_ids.append(r["id"])
            fb_texts.append(text)

    if fb_texts:
        t0 = datetime.now()
        for i in range(0, len(fb_texts), config.EMBED_BATCH):
            dense, _ = embed.encode(fb_texts[i:i + config.EMBED_BATCH])
            for j, v in enumerate(_l2(np.asarray(dense, dtype=np.float32))):
                todo[fb_ids[i + j]] = v
            log("  回退编码 %d/%d（无 chunks，用时 %ds）"
                % (min(i + config.EMBED_BATCH, len(fb_texts)), len(fb_texts),
                   (datetime.now() - t0).seconds))

    if not todo:
        return {"chunkmean": len(agg), "encoded_fallback": len(fb_ids),
                "computed": 0}

    keys = sorted(todo)
    now = db.now_iso()
    with db.tx() as w:
        if force:
            # 必须和写入同一个事务：conn_ctx 的连接关闭时会回滚未提交的事务，
            # 清理写在那边等于没写。
            w.execute("DELETE FROM article_profiles")
        w.executemany(
            "INSERT INTO article_profiles (article_id, vec, model_id, built_at) "
            "VALUES (?,?,?,?) ON CONFLICT(article_id) DO UPDATE SET "
            "vec=excluded.vec, model_id=excluded.model_id, built_at=excluded.built_at",
            [(aid, todo[aid].astype(np.float32).tobytes(), PROFILE_MODEL_ID, now)
             for aid in keys])
    return {"chunkmean": len(agg), "encoded_fallback": len(fb_ids),
            "computed": len(keys)}


def _load_matrix(conn):
    """读回画像向量，返回 (ids, X已归一化)。顺带排除 status='failed'。"""
    rows = conn.execute(
        "SELECT p.article_id, p.vec FROM article_profiles p "
        "JOIN articles a ON a.id = p.article_id "
        "WHERE a.status <> 'failed' ORDER BY p.article_id").fetchall()
    if not rows:
        return [], np.zeros((0, config.EMBED_DIM), dtype=np.float32)
    ids = [r["article_id"] for r in rows]
    X = _l2(np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows]))
    return ids, X


# ---------------------------------------------------------------- 目录挂载

def load_catalog(catalog: dict) -> list:
    """把目录摊平成三级节点列表，并做结构校验。

    校验不是形式主义：**层级（level/parent）是唯一表达「谁包含谁」的地方**，
    目录写错层级不会报错，只会让目录树悄悄变成一堆平铺的话题 —— 那正是第一版
    失败的原因（没有层次）。所以这里宁可报错。
    """
    nodes = []
    for d in catalog.get("domains") or []:
        for key in ("key", "name"):
            if not (d.get(key) or "").strip():
                raise RuntimeError("领域缺少 %s：%r" % (key, d.get("name")))
        if not (d.get("boards") or []):
            raise RuntimeError("领域「%s」下没有任何板块 —— 领域必须至少挂一个板块，"
                               "否则目录就没有层次" % d["name"])
        nodes.append({"kind": "domain", "key": d["key"], "name": d["name"],
                      "summary": d.get("summary") or "",
                      "keywords": d.get("keywords") or [],
                      "parent": None, "level": 1, "members": []})
        for b in d["boards"]:
            if not (b.get("topics") or []):
                raise RuntimeError("板块「%s」下没有任何话题" % b.get("name"))
            nodes.append({"kind": "board", "key": b["key"], "name": b["name"],
                          "summary": b.get("summary") or "",
                          "keywords": b.get("keywords") or [],
                          "parent": d["key"], "level": 2, "members": []})
            for t in b["topics"]:
                for key in ("key", "name"):
                    if not (t.get(key) or "").strip():
                        raise RuntimeError("板块「%s」下的话题缺少 %s"
                                           % (b.get("name"), key))
                nodes.append({"kind": "topic", "key": t["key"], "name": t["name"],
                              "summary": t.get("summary") or "",
                              "keywords": t.get("keywords") or [],
                              "parent": b["key"], "level": 3, "members": []})
    if not nodes:
        raise RuntimeError("目录是空的")
    seen = set()
    for n in nodes:
        if n["key"] in seen:
            raise RuntimeError("节点 key 重复：%s" % n["key"])
        seen.add(n["key"])
    return nodes


def _art_row(meta: dict, aid: str, sim: float) -> dict:
    """成员列表里的一行（文章基本信息 + 它与该节点的相似度）。"""
    m = meta[aid]
    return {"id": aid, "title": m["title"] or "", "category": m["category"],
            "date": (m["published_at"] or m["collected_at"] or "")[:10],
            "sim": round(float(sim), 3)}


def node_text(node: dict) -> str:
    """节点向量用的文本（领域 / 板块 / 话题统一）—— 名称 + 说明 + 关键词。"""
    return "\n".join([node["name"], node["summary"],
                      "、".join(node["keywords"][:8])]).strip()




def mount_catalog(conn, catalog: dict, log=print) -> dict:
    """挂载：每个板块内先决出一个话题，再跨板块取最好的 TOPIC_TOP_N 个。

    **两个坑，都是实测踩出来的**：

    ① 全局竞争不行：库里「企业 AI」这一块有 400 多篇、内容高度同质，伪相关反馈
    的粗排对**每一个**相邻话题都选出同一批核心文章（企业 AI 时代路线图、Agentic
    Enterprise 白皮书…），质心趋同 → 话题之间开始抢同一批文章。实测 39 个话题里
    17 个的代表篇明显错（「规模化踩坑→路线图」「Agent 架构→趋势评论」
    「制造业→白皮书」）。**按板块限定竞争范围**就能解决：同一批核心文章不会
    再被所有相邻话题同时选中。

    ② 但也不能让「领域」当路由闸门：先试了「领域 → 板块 → 话题 逐层 argmax」，
    结果 D2/D3/D4 三个领域的话题**全是 0 篇** ——「企业 AI 落地」这个词组的向量
    覆盖了库里的整个 AI 语义空间，把其他三个领域直接饿死。
    所以领域与板块只负责「展示」和「划定竞争范围」，裁决权交给话题：
    板块内先各自决出一个最优话题，再跨板块取全局最好的 TOPIC_TOP_N 个。
    """
    nodes = load_catalog(catalog)
    ids, X = _load_matrix(conn)
    if len(ids) < 8:
        raise RuntimeError("只有 %d 篇文章有画像向量，不足以挂载" % len(ids))

    kinds = {k: [i for i, n in enumerate(nodes) if n["kind"] == k]
             for k in ("domain", "board", "topic")}
    by_parent = {}
    for i, n in enumerate(nodes):
        by_parent.setdefault(n["parent"], []).append(i)
    for i, n in enumerate(nodes):
        n["members"] = []

    texts = [node_text(n) for n in nodes]
    vecs = []
    for i in range(0, len(texts), config.EMBED_BATCH):
        dense, _ = embed.encode(texts[i:i + config.EMBED_BATCH])
        vecs.append(_l2(np.asarray(dense, dtype=np.float32)))
    E = np.vstack(vecs)
    log("  节点向量 %d 个已编码（%d 领域 / %d 板块 / %d 话题）"
        % (len(nodes), len(kinds["domain"]), len(kinds["board"]),
           len(kinds["topic"])))

    # 伪相关反馈两阶段：文本向量粗拉候选 → 候选投票定节点向量 → 重算相似度
    sims = X @ E.T
    C = np.zeros_like(E)
    for j in range(len(nodes)):
        cand = np.argsort(-sims[:, j])[:COARSE_K]
        C[j] = _l2(X[cand].mean(axis=0, keepdims=True))[0]
    sims = X @ C.T

    unmounted = 0
    unmounted_ids = []
    for i, aid in enumerate(ids):
        row = sims[i]
        # 先按板块分组，组内只留最优的那个话题（否则同一板块的多个话题会瓜分）
        best_of_board = []
        for bi in kinds["board"]:
            kids = by_parent.get(nodes[bi]["key"], [])
            if not kids:
                continue
            j = max(kids, key=lambda t: row[t])
            best_of_board.append((float(row[j]), j))
        best_of_board.sort(reverse=True)
        picked = [(j, s) for s, j in best_of_board[:TOPIC_TOP_N]
                  if s >= TOPIC_MIN_SIM]
        if not picked:
            unmounted += 1
            unmounted_ids.append(aid)
            continue
        for j, s in picked:
            nodes[j]["members"].append((aid, s))

    meta = _article_meta(conn)
    for n in nodes:
        n["members"].sort(key=lambda x: -x[1])
        n["count"] = len(n["members"])
        n["articles"] = [_art_row(meta, a, s) for a, s in n["members"]]
        if n["kind"] != "topic":
            continue
        n["sim_mean"] = round(float(np.mean([s for _, s in n["members"]])), 4) \
            if n["members"] else 0.0
        # 代表篇额外带摘要：全量成员不带（payload 会大一倍，而页面点开才需要摘要）
        n["top_articles"] = [dict(a, summary=(meta[a["id"]]["summary"] or "")[:160])
                             for a in n["articles"][:TOP_ARTICLES_PER_TOPIC]]

    # 领域 / 板块的篇数**必须去重**：一篇文章可以同时挂多个话题，直接相加会把
    # 同一篇重复计入多个下级，四个领域加起来会远超全库篇数（实测超过 2 倍）。
    for n in sorted(nodes, key=lambda x: -x["level"]):
        if n["kind"] == "topic":
            continue
        merged = {}
        for s in by_parent.get(n["key"], []):
            for aid, sim in nodes[s]["members"]:
                merged[aid] = max(sim, merged.get(aid, 0.0))
        n["members"] = sorted(merged.items(), key=lambda x: -x[1])
        n["count"] = len(merged)
        n["articles"] = [_art_row(meta, a, s) for a, s in n["members"]]

    topics = [nodes[i] for i in kinds["topic"]]
    best = sims[:, kinds["topic"]].max(axis=1) if kinds["topic"] else np.zeros(len(ids))
    return {
        "built_at": db.now_iso(), "model_id": PROFILE_MODEL_ID,
        "n_articles": len(ids),
        "method": "每个板块内先决出一个话题，再跨板块取最好的 %d 个" % TOPIC_TOP_N,
        "mount": {"top_n": TOPIC_TOP_N, "min_sim": TOPIC_MIN_SIM,
                  "unmounted": unmounted,
                  "sim_p50": round(float(np.median(best)), 4),
                  "sim_p90": round(float(np.percentile(best, 90)), 4),
                  "sim_max": round(float(best.max()), 4)},
        "categories": {c["key"]: c["name"] for c in db.get_categories(conn)},
        "nodes": [{k: v for k, v in n.items() if k != "members"} for n in nodes],
        "domains": catalog["domains"],
        "overlaps": _overlaps(topics),
        # 没挂上任何话题的文章要**列出标题**，不能只给一个数字：数字看不出
        # 是哪些，用户也没法判断该不该给它们单开一个话题
        "unmounted_articles": [{
            "id": a, "title": meta[a]["title"] or "",
            "category": meta[a]["category"],
            "date": (meta[a]["published_at"] or meta[a]["collected_at"] or "")[:10],
        } for a in unmounted_ids],
    }


def _overlaps(topics: list, min_jaccard: float = 0.5) -> list:
    """话题成员重叠检测：两个话题大量共用文章，说明它们在抢同一批内容。

    为什么要自动算这个：目录是人工写的，关键词一写泛就会出现这种问题，而且
    **会自我强化** —— 伪相关反馈的粗排选出 20 篇偏了的文章，它们投票出的质心
    更偏，于是越吸越偏。实测「提示词与上下文工程」因为关键词里带了 Skill，
    代表篇全变成了 Skill 类文章。靠人抽样是碰运气，靠这个指标是确定性检查。
    """
    sets = [set(a for a, _ in t["members"]) for t in topics]
    out = []
    for a in range(len(topics)):
        if not sets[a]:
            continue
        for b in range(a + 1, len(topics)):
            if not sets[b]:
                continue
            inter, union = len(sets[a] & sets[b]), len(sets[a] | sets[b])
            j = inter / union if union else 0.0
            if j >= min_jaccard:
                out.append({"a": topics[a]["name"], "b": topics[b]["name"],
                            "jaccard": round(j, 3), "shared": inter})
    return sorted(out, key=lambda x: -x["jaccard"])


# ---------------------------------------------------------------- 落库

def apply_catalog(conn, payload: dict) -> dict:
    """把目录 + 挂载结果写入 themes / article_themes。

    **这是破坏性操作**：两张表整表重建（它们是派生数据）。所以本函数第一件事
    就是备份数据库 —— 涉及用户真实数据的功能必须有自动备份方案。
    `articles` / `chunks` / 向量索引一行不动。

    名称缺失就报错：宁可什么都不做，也不能拿占位名落库。
    """
    nodes = payload["nodes"]
    if not nodes:
        raise RuntimeError("payload 里没有目录节点")
    for n in nodes:
        if not (n.get("name") or "").strip():
            raise RuntimeError("节点 %s 没有名称（拒绝用占位名落库）" % n.get("key"))

    backup = db.backup_db("before-catalog-apply")
    ids, X = _load_matrix(conn)
    pos = {aid: i for i, aid in enumerate(ids)}
    nid = {n["key"]: i + 1 for i, n in enumerate(nodes)}   # themes.id 从 1 开始
    kind_key = {"domain": "domains", "board": "boards", "topic": "topics"}

    now = db.now_iso()
    stats = {"backup": str(backup), "domains": 0, "boards": 0, "topics": 0,
             "primary": 0, "secondary": 0, "skipped_articles": 0}

    with db.tx() as w:
        w.execute("DELETE FROM article_themes")
        w.execute("DELETE FROM themes")
        # 必须连自增序列一起清：themes.id 是 AUTOINCREMENT，只 DELETE 行的话
        # sqlite_sequence 保留旧值，下一批 id 会从历史最大值往上走 —— 而上面
        # nid 是按 1..N 编号的，两者错位会让 parent_id 全部指向错误的主题，
        # 而且不报错（最坏的一类 bug）。
        w.execute("DELETE FROM sqlite_sequence WHERE name = 'themes'")

        for n in nodes:
            cen = np.zeros(config.EMBED_DIM, dtype=np.float32)
            mem = [pos[a["id"]] for a in n.get("articles", []) if a["id"] in pos]
            if mem:
                cen = _l2(X[mem].mean(axis=0, keepdims=True))[0]
            w.execute(
                "INSERT INTO themes (id, name, summary, keywords, parent_id, level, "
                "centroid, article_count, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?, 'active', ?, ?)",
                (nid[n["key"]], n["name"].strip(), (n["summary"] or "").strip(),
                 db.jdumps(n.get("keywords") or []),
                 nid[n["parent"]] if n["parent"] in nid else None,
                 int(n["level"]), cen.astype(np.float32).tobytes(),
                 len(mem), now, now))
            stats[kind_key[n["kind"]]] += 1

        # 文章归属：**主话题 = 这篇文章在自己挂的几个话题里相似度最高的那个**（confirmed）；
        # 其余为次话题（suggested，等用户确认）——「一文多主题」是这套结构的核心，
        # 不该由系统替用户拍板。
        # 注意别写成「每个话题里的第一篇」：那是「话题的代表篇」，不是「文章的主话题」
        # （踩过：那样算出来主话题只有话题数 39 条，而不是文章数条）。
        mounted = []
        for n in nodes:
            if n["kind"] != "topic":
                continue
            for a in n["articles"]:
                if a["id"] not in pos:
                    stats["skipped_articles"] += 1
                    continue
                mounted.append((a["id"], nid[n["key"]], round(float(a["sim"]), 4), now))
        primary_of = {}
        for aid, tid, wgt, _ in mounted:
            if aid not in primary_of or wgt > primary_of[aid][0]:
                primary_of[aid] = (wgt, tid)
        rows = [(aid, tid, wgt,
                 "primary" if primary_of[aid][1] == tid else "secondary",
                 "confirmed" if primary_of[aid][1] == tid else "suggested", at)
                for aid, tid, wgt, at in mounted]
        w.executemany(
            "INSERT INTO article_themes (article_id, theme_id, weight, role, "
            "status, added_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(article_id, theme_id) DO UPDATE SET "
            "weight=excluded.weight, role=excluded.role, status=excluded.status",
            rows)
        stats["primary"] = sum(1 for r in rows if r[3] == "primary")
        stats["secondary"] = sum(1 for r in rows if r[3] == "secondary")

        topics = [n for n in nodes if n["kind"] == "topic"]
        counts = [n["count"] for n in topics] or [0]
        for k, v in (("catalog_built_at", payload["built_at"]),
                     ("model_id", payload["model_id"]),
                     ("n_articles", str(payload["n_articles"])),
                     ("n_topics", str(len(topics))),
                     ("n_primary", str(stats["primary"])),
                     ("n_secondary", str(stats["secondary"])),
                     ("unmounted", str(payload["mount"]["unmounted"])),
                     ("topic_count_min", str(min(counts))),
                     ("topic_count_max", str(max(counts))),
                     ("backup", str(backup))):
            w.execute("INSERT INTO structure_meta (key, value) VALUES (?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, str(v)))
    return stats