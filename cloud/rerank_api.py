# -*- coding: utf-8 -*-
"""精排云端版：rerank_docs 走 rerank API（server/rerank.py 的 drop-in 替换）。

**必须遵守 server/rerank.py 的三条契约**（search.py 按此写的，不能打折）：
1. 返回 `(降序下标列表, 对应分数)`，分数是 0–1 的绝对相关度；
2. 不可用 / 失败一律返回 **None**，调用方保留 RRF 原序——
   精排只是增强项，绝不能因为它把检索整体搞挂；
3. 只对头部 top_n 条打分，不做全量。

密钥未配置时直接返回 None（等同本地没下 reranker 权重的降级路径）。
"""

import logging

import requests

from server import config

from . import cconfig

log = logging.getLogger("myrag.cloud.rerank")


def _clip(text, n=None):
    """与 server/rerank.py 的 _clip 同口径：压平空白 + 截断（省 API 费用的主要手段）。"""
    t = " ".join((text or "").split())
    return t[: n or config.RERANK_MAX_CHARS]


def rerank_docs(query, docs, top_n=None):
    """按与 query 的相关度重排 docs，返回 (order, scores)；不可用返回 None。"""
    if not docs:
        return None
    n = min(len(docs), top_n or config.RERANK_TOP_N)
    if n <= 1:
        return None
    if not cconfig.API_KEY:
        return None

    try:
        r = requests.post(
            cconfig.API_BASE + "/rerank",
            json={"model": cconfig.RERANK_API_MODEL,
                  "query": query,
                  "documents": [_clip(d) for d in docs[:n]],
                  "top_n": n,
                  "return_documents": False},
            headers={"Authorization": "Bearer %s" % cconfig.API_KEY},
            timeout=cconfig.API_TIMEOUT)
        if r.status_code >= 400:
            log.warning("精排 API 失败 %s，保留原顺序：%s", r.status_code, r.text[:200])
            return None
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("精排 API 不可用，保留原顺序：%s", e)
        return None

    results = data.get("results") or []
    order, scores = [], []
    for item in results:
        try:
            idx = int(item["index"])
            if 0 <= idx < n:
                order.append(idx)
                scores.append(float(item["relevance_score"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(order) < 2:
        return None
    return order, scores
