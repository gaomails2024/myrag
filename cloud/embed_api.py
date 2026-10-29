# -*- coding: utf-8 -*-
"""查询向量云端版：encode_query 走 HTTP API（server/embed.py 的 drop-in 替换）。

与本地版的关键差异（改动前必须知道）：

1. **只产 dense，不产 sparse**。sparse 词权重需要 BGE-M3 的完整前向计算，
   任何 embedding API 都不返回它。返回空 dict `[]`——search._sparse_topk
   对空权重天然兼容（拿不到列直接返回空列表），检索自动降为 dense 单路 + RRF。
2. **向量空间必须与本地一致**：本地库里的 1024 维向量是 BGE-M3（FP32、归一化）
   算的，所以 API 必须用同款模型（BAAI/bge-m3）；拿到向量后再做一次归一化
   （API 已归一化时是幂等无操作），保证 L2 距离排序与本地语义排序一致。
3. **失败必须报人话、可被上层捕获降级**：EmbedApiError 是 cloud/app.py
   关键词兜底的触发信号，不许静默吞掉。
"""

import logging

import numpy as np
import requests

from . import cconfig

log = logging.getLogger("myrag.cloud.embed")


class EmbedApiError(RuntimeError):
    """向量服务不可用/返回异常。调用方（cloud/app.py）据此降级为关键词搜索。"""


def _post(path: str, payload: dict) -> dict:
    if not cconfig.API_KEY:
        raise EmbedApiError("未配置向量服务密钥（.env 里加 SILICONFLOW_API_KEY）")
    try:
        r = requests.post(
            cconfig.API_BASE + path, json=payload,
            headers={"Authorization": "Bearer %s" % cconfig.API_KEY},
            timeout=cconfig.API_TIMEOUT)
    except requests.RequestException as e:
        raise EmbedApiError("向量服务连接失败：%s" % e)
    if r.status_code == 401:
        raise EmbedApiError("向量服务密钥无效（401），检查 SILICONFLOW_API_KEY")
    if r.status_code == 429:
        raise EmbedApiError("向量服务限流（429），稍后再试")
    if r.status_code >= 400:
        raise EmbedApiError("向量服务错误 %s：%s" % (r.status_code, r.text[:200]))
    try:
        return r.json()
    except ValueError:
        raise EmbedApiError("向量服务返回了非 JSON 内容")


def encode_query(text: str):
    """返回 (dense: np.ndarray[1024] float32 已归一化, sparse: {})。

    签名与 server/embed.py 的 encode_query 一致，sparse 恒为空 dict
    （见模块 docstring 第 1 条）。
    """
    text = (text or "").strip()
    if not text:
        # search.search 对空 query 提前返回，正常走不到这里；防御性给零向量
        return np.zeros(1024, dtype=np.float32), {}

    data = _post("/embeddings", {
        "model": cconfig.EMBED_MODEL,
        "input": [text],
        "encoding_format": "float",
    })
    try:
        vec = data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError):
        raise EmbedApiError("向量服务返回格式异常（拿不到 embedding）")

    a = np.asarray(vec, dtype=np.float32)
    if a.shape[0] != 1024:
        raise EmbedApiError(
            "向量维度不符：API 返回 %d 维，库里的向量是 1024 维（BGE-M3）。"
            "检查 MYRAG_EMBED_MODEL 是否为 BAAI/bge-m3" % a.shape[0])
    n = float(np.linalg.norm(a))
    if n > 0:
        a = a / n          # 与本地 BGE-M3 输出对齐；API 已归一化时为幂等
    return a, {}
