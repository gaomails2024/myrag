# -*- coding: utf-8 -*-
"""BGE-M3 封装（PRD §7.1）：懒加载单例，一次编码同时产 dense + sparse。

- 权重已缓存时全程不联网（调用方通过 HF_HUB_OFFLINE=1 强制）
- use_fp16=False：本机为 CPU 推理（实测 1191 token × 8 段 = 4.09s，峰值 933MB）
- 维度必须与 schema 的 chunk_vecs FLOAT[1024] 一致
"""

import threading

import numpy as np

from . import config

_lock = threading.Lock()
# 推理串行锁：BGE-M3 实例不是线程安全的，预热线程与请求线程并发调用会段错误
# （已实测复现：后台预热未完成时来一个搜索请求，进程直接挂掉、无 traceback）。
# 本系统是单用户本地工具，串行推理不损失可用性。
_infer_lock = threading.Lock()
_model = None
_tokenizer = None


def get_tokenizer():
    """独立取 tokenizer（切段时算 token 数用，不必加载整个模型）。"""
    global _tokenizer
    if _tokenizer is None:
        with _lock:
            if _tokenizer is None:
                from transformers import AutoTokenizer

                _tokenizer = AutoTokenizer.from_pretrained(config.resolve_model_path())
    return _tokenizer


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from FlagEmbedding import BGEM3FlagModel

                _model = BGEM3FlagModel(config.resolve_model_path(), use_fp16=False)
    return _model


def count_tokens(text: str) -> int:
    """按 BGE-M3 tokenizer 计 token（含特殊 token），与编码时的截断口径一致。"""
    if not text:
        return 0
    return len(get_tokenizer().encode(text, add_special_tokens=True))


def encode(texts, batch_size=None, max_length=None):
    """返回 (dense: np.ndarray[n, 1024] float32, sparse: list[dict[str, float]])

    sparse 的 key 是 BGE-M3 的 token id 字符串——不做任何分词（PRD §7.1、§8.1）。
    """
    if not texts:
        return np.zeros((0, config.EMBED_DIM), dtype=np.float32), []

    model = get_model()
    with _infer_lock:
        out = model.encode(
            list(texts),
            batch_size=batch_size or config.EMBED_BATCH,
            max_length=max_length or config.CHUNK_MAX_TOKENS,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,  # ColBERT 多向量一期不启用（PRD §7.1）
        )
    dense = np.asarray(out["dense_vecs"], dtype=np.float32)
    sparse = [
        {str(k): float(v) for k, v in d.items()} for d in out["lexical_weights"]
    ]
    return dense, sparse


def encode_query(text: str):
    dense, sparse = encode([text])
    return dense[0], sparse[0]


def warmup() -> dict:
    """启动预热：加载模型并编码一次，返回实测信息。"""
    import time

    t0 = time.time()
    get_model()
    t_load = time.time() - t0
    t1 = time.time()
    dense, sparse = encode(["预热"])
    t_enc = time.time() - t1
    return {
        "load_sec": round(t_load, 2),
        "encode_sec": round(t_enc, 2),
        "dim": int(dense.shape[1]) if dense.size else 0,
        "sparse_tokens": len(sparse[0]) if sparse else 0,
    }
