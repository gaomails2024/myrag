# -*- coding: utf-8 -*-
"""检索：dense 近邻 + sparse 词权重内积 + RRF 融合（PRD §8）。

- sparse 矩阵常驻内存（启动/入库后从 SQLite 构建），不做逐条 Python 内积
- 查询侧同样走 BGE-M3 的 sparse 输出，全程无分词步骤
- 有过滤条件时，KNN 取全量再在 Python 侧按允许集合筛（保证不漏，代价可控：
  万级段数 × 1024 维线性扫描为毫秒级）
"""

import re
import threading

import numpy as np
from scipy.sparse import csr_matrix

from . import config, db, embed, rerank

_lock = threading.Lock()
_index = None
_index_version = 0
_index_built_at = -1


def bump_index() -> None:
    """入库/删除后调用，下次检索时重建 sparse 矩阵。"""
    global _index_version
    with _lock:
        _index_version += 1


def _build_index(conn):
    rows = conn.execute(
        "SELECT chunk_id, weights FROM chunk_sparse ORDER BY chunk_id").fetchall()
    vocab, indptr, indices, data, cids = {}, [0], [], [], []
    for r in rows:
        w = db.jloads(r["weights"], {})
        for k, v in w.items():
            c = vocab.get(k)
            if c is None:
                c = len(vocab)
                vocab[k] = c
            indices.append(c)
            data.append(float(v))
        indptr.append(len(indices))
        cids.append(int(r["chunk_id"]))
    ncols = max(1, len(vocab))
    matrix = csr_matrix(
        (np.asarray(data, dtype=np.float32),
         np.asarray(indices, dtype=np.int32),
         np.asarray(indptr, dtype=np.int64)),
        shape=(len(cids), ncols))
    return {"matrix": matrix,
            "chunk_ids": np.asarray(cids, dtype=np.int64),
            "vocab": vocab}


def get_index(conn):
    global _index, _index_built_at
    if _index is not None and _index_built_at == _index_version:
        return _index
    with _lock:
        if _index is None or _index_built_at != _index_version:
            _index = _build_index(conn)
            _index_built_at = _index_version
    return _index


# ---------------------------------------------------------------- 过滤

def _filter_sql(category, tag, date_from, date_to):
    where, params = ["a.status <> 'failed'"], []
    if category:                     # 分类可配置，不做白名单校验
        where.append("a.category = ?")
        params.append(category)
    if tag:
        where.append("EXISTS (SELECT 1 FROM json_each(a.tags) WHERE json_each.value = ?)")
        params.append(tag)
    if date_from:
        where.append("substr(COALESCE(a.published_at, a.collected_at), 1, 10) >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        where.append("substr(COALESCE(a.published_at, a.collected_at), 1, 10) <= ?")
        params.append(str(date_to)[:10])
    return " AND ".join(where), params


def _allowed_chunks(conn, fsql, params):
    rows = conn.execute(
        "SELECT c.id FROM chunks c JOIN articles a ON a.id = c.article_id WHERE " + fsql,
        params).fetchall()
    return set(int(r["id"]) for r in rows)


# ---------------------------------------------------------------- 两路

def _dense_topk(conn, vec, k, allowed):
    blob = np.asarray(vec, dtype=np.float32).tobytes()
    # 有过滤时取全量（上限防呆），保证被过滤掉的命中不会挤掉合法命中
    kk = min(max(len(allowed), config.TOP_PER_ROUTE), config.SPARSE_KNN_CAP) \
        if allowed is not None else k
    rows = conn.execute(
        "SELECT chunk_id, distance FROM chunk_vecs WHERE embedding MATCH ? AND k = %d"
        % int(kk), (blob,)).fetchall()
    out = []
    for r in rows:
        cid = int(r["chunk_id"])
        if allowed is not None and cid not in allowed:
            continue
        out.append(cid)
        if len(out) >= k:
            break
    return out


def _sparse_topk(conn, qweights, k, allowed):
    # 【试过并已否掉的改动 · 2026-09-21】给 query 的每个词乘「本库 IDF」
    #
    # 动机：「Agent 记忆」搜不准 —— 单搜「记忆」时目标文章两路都排 #1，
    #   加上「Agent」后 sparse 路直接掉出前 20（本库 15% 的段含 Agent、2% 含记忆）。
    #   看起来就是经典的「高频词淹没稀有词」，而 BGE-M3 的 sparse 权重学自通用
    #   语料、不知道词在本库的常见度 —— 乘个库内 IDF 似乎是标准解法。
    #
    # 实测结果：**整体变差，已回滚**。同一套 37 条评测集：
    #   MRR 0.880 → 0.842、Recall@1 0.611 → 0.567、nDCG@10 0.855 → 0.834，
    #   而那个 case 只从 #14 挪到 #12。
    #
    # 为什么错：「Agent 记忆」里的「Agent」**是用户意图的一部分**（他要的就是
    #   Agent 相关的记忆），不是需要压制的噪声。降它的权，等于把有效的限定条件
    #   当成干扰丢掉 —— 于是别的多词 query 一起被拖下水。
    #   理论正确 ≠ 在这个库上正确；这类改动**必须跑评测再定**。
    idx = get_index(conn)
    vocab, matrix, cids = idx["vocab"], idx["matrix"], idx["chunk_ids"]
    if matrix.shape[0] == 0:
        return []
    cols, vals = [], []
    for t, w in qweights.items():
        c = vocab.get(t)
        if c is not None:
            cols.append(c)
            vals.append(float(w))
    if not cols:
        return []
    q = csr_matrix(
        (np.asarray(vals, dtype=np.float32),
         (np.zeros(len(cols), dtype=np.int32), np.asarray(cols, dtype=np.int32))),
        shape=(1, matrix.shape[1]))
    scores = np.asarray((matrix @ q.T).todense()).ravel()
    out = []
    for i in np.argsort(-scores):
        s = float(scores[i])
        if s <= 0:
            break
        cid = int(cids[i])
        if allowed is not None and cid not in allowed:
            continue
        out.append((cid, s))
        if len(out) >= k:
            break
    return out


def _rrf(lists, k):
    """score = Σ 1/(k + rank)，k=60（PRD §8.1）。"""
    score, ranks = {}, {}
    for li, lst in enumerate(lists):
        for r, cid in enumerate(lst, 1):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + r)
            ranks.setdefault(cid, {})[li] = r
    return sorted(score.items(), key=lambda x: -x[1]), ranks


# ---------------------------------------------------------------- 片段

_MD_NOISE = re.compile(r"```.*?```|`([^`]*)`|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)|"
                       r"^[#>\-\*\|\s]+|[*_]{1,3}", re.S | re.M)


def snippet(text: str, limit: int = 180) -> str:
    t = _MD_NOISE.sub(lambda m: (m.group(1) or m.group(2) or ""), text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[:limit] + "…"


# ---------------------------------------------------------------- 全文兜底（fulltext 类）

def _fulltext_fallback(conn, q, fsql, fparams, seen, room):
    """retrieval=fulltext 的分类：不建向量索引，只在标题/摘要/标签上做 LIKE 兜底召回。

    为什么要有这一步：那类文章一个 chunk 都没有，两路召回（dense/sparse）永远
    碰不到它们，用户会觉得「存了却搜不到」。命中排在 RAG 命中之后，并标
    match_type=fulltext，让界面能说清这条是怎么来的。
    """
    if room <= 0 or not q:
        return []
    like = "%" + q + "%"
    rows = conn.execute(
        """SELECT a.id, a.title, a.category, a.source, a.published_at, a.collected_at,
                  a.url, a.summary
           FROM articles a JOIN categories c ON c.key = a.category
           WHERE %s AND c.retrieval = 'fulltext'
             AND (a.title LIKE ? OR a.summary LIKE ? OR a.tags LIKE ?)
           ORDER BY COALESCE(a.published_at, a.collected_at) DESC
           LIMIT ?""" % fsql,
        list(fparams) + [like, like, like, int(room)]).fetchall()
    out = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append({
            "article_id": r["id"], "title": r["title"], "category": r["category"],
            "source": r["source"], "published_at": r["published_at"],
            "collected_at": r["collected_at"], "url": r["url"],
            "score": 0.0, "match_type": "fulltext",
            "matched_chunks": [{
                "heading_path": "全文匹配",
                "snippet": r["summary"] or r["title"],
                "rank_dense": None, "rank_sparse": None,
            }],
        })
    return out


# ---------------------------------------------------------------- 主入口

def search(conn, q, mode="hybrid", category=None, tag=None,
           date_from=None, date_to=None, limit=None):
    limit = limit or config.SEARCH_TOP_N
    q = (q or "").strip()
    out = {"query": q, "mode": mode, "total": 0, "hits": []}
    if not q:
        return out

    fsql, fparams = _filter_sql(category, tag, date_from, date_to)
    allowed = _allowed_chunks(conn, fsql, fparams) if fparams else None

    ranked = []
    # allowed 为空集 ≠ 没结果：fulltext 类文章本就没有任何分段，要靠下面的
    # LIKE 兜底召回，所以这里不能提前 return（PRD §5.5）。
    if allowed is None or allowed:
        qdense, qsparse = embed.encode_query(q)

        lists, names = [], []
        if mode in ("hybrid", "dense"):
            dl = _dense_topk(conn, qdense, config.TOP_PER_ROUTE, allowed)
            if dl:
                lists.append(dl)
                names.append("dense")
        if mode in ("hybrid", "sparse"):
            sl = _sparse_topk(conn, qsparse, config.TOP_PER_ROUTE, allowed)
            if sl:
                lists.append([c for c, _ in sl])
                names.append("sparse")

        if lists:
            ordered, ranks = _rrf(lists, config.RRF_K)
            di = names.index("dense") if "dense" in names else None
            si = names.index("sparse") if "sparse" in names else None

            top = ordered[: max(limit * 3, 30)]
            cids = [c for c, _ in top]
            info = {}
            for r in conn.execute(
                    """SELECT c.id, c.article_id, c.heading_path, c.text,
                              a.title, a.category, a.source, a.published_at, a.collected_at, a.url
                       FROM chunks c JOIN articles a ON a.id = c.article_id
                       WHERE c.id IN (%s)""" % ",".join("?" * len(cids)), cids):
                info[int(r["id"])] = dict(r)

            # ---- 精排（PRD §8.4）----
            # RRF 只能保证「两路都排在前面」，判断不了「答没答到点上」。cross-encoder
            # 无法预计算文档侧向量，所以只对头部候选过一遍，其余保持原序 ——
            # 排序的意义本来就在头部。
            n_rr = min(len(top), config.RERANK_TOP_N)
            rr_map = {}                         # chunk_id → 精排相关度（0–1）
            if n_rr > 1:
                head = top[:n_rr]
                texts = []
                for cid, _ in head:
                    row = info.get(cid) or {}
                    # 带上标题与标题链：只看段落文字，reranker 也难判断"答到点上没有"
                    texts.append("%s ｜ %s\n%s" % (row.get("title") or "",
                                                   row.get("heading_path") or "",
                                                   row.get("text") or ""))
                res = rerank.rerank_docs(q, texts)
                if res:                         # None = 不可用/失败 → 保留原顺序
                    order, scores = res
                    top = [head[i] for i in order] + top[n_rr:]
                    rr_map = {head[i][0]: scores[k] for k, i in enumerate(order)}

            hits = {}
            for cid, sc in top:
                row = info.get(cid)
                if not row:
                    continue
                aid = row["article_id"]
                h = hits.get(aid)
                if h is None:
                    h = {"article_id": aid, "title": row["title"], "category": row["category"],
                         "source": row["source"], "published_at": row["published_at"],
                         "collected_at": row["collected_at"], "url": row["url"],
                         # score = RRF 排名融合分（**只反映"两路排在哪"，不反映相关度**）
                        # rerank_score = 0–1 的绝对相关度，未开精排时为 None。
                        # 界面上判断「该不该提示没找到」要用后者，前者会误判。
                        "score": round(sc, 4),
                        "rerank_score": (round(rr_map[cid], 4) if cid in rr_map else None),
                        "match_type": "rag", "matched_chunks": []}
                    hits[aid] = h
                if len(h["matched_chunks"]) < 3:
                    rk = ranks.get(cid, {})
                    h["matched_chunks"].append({
                        "heading_path": row["heading_path"],
                        "snippet": snippet(row["text"]),
                        "rank_dense": rk.get(di) if di is not None else None,
                        "rank_sparse": rk.get(si) if si is not None else None,
                    })
            # **不要按 score 再排一次**：score 是 RRF 排名分，重排会把上面的精排结果
            # 覆盖掉。dict 的插入顺序就是 top 的顺序（已排好）。不开精排时两者等价：
            # top 本就按 RRF 降序，同一篇文章的多个段只有第一个会建档。
            ranked = list(hits.values())[:limit]

    seen = set(h["article_id"] for h in ranked)
    ft = _fulltext_fallback(conn, q, fsql, fparams, seen,
                            max(0, limit - len(ranked)))

    out["hits"] = ranked + ft
    out["total"] = len(out["hits"])
    out["fulltext_hits"] = len(ft)
    return out


def index_stats(conn):
    """给 /api/stats 用：sparse 矩阵规模。"""
    idx = get_index(conn)
    m = idx["matrix"]
    return {"sparse_rows": int(m.shape[0]), "sparse_cols": int(m.shape[1]),
            "sparse_nnz": int(m.nnz)}
