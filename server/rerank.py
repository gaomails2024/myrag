"""Reranker 精排（PRD §8.4）。

**为什么需要它**：BGE-M3 是 bi-encoder —— 问题与文档**分别**编码后比向量距离。
快，但判断粗：只能回答「这段话和问题语义**像不像**」，回答不了
「这段话到底**答没答到点上**」。于是初检 Top-N 里那些「语义相近但没用」的段会
原样进入 prompt，LLM 照单全收、反被带偏。

cross-encoder 把 (问题, 段落) **拼在一起**过一次模型，才能做后一个判断。代价是
慢（不能预计算文档侧向量），所以只对少量候选做，不做全库。

**三条约束**（PRD §8.4，实现时不可打折）：

1. **只作用于检索，不改入库路径** —— 因此新增它**不需要 reindex**
2. **懒加载**（同 embed.py）：首次调用时才载权重，服务启动不为它拖慢
3. **降级**：权重缺失 / 加载失败 / 推理异常，一律**跳过精排并记日志**，
   绝不能因为精排把检索整体搞挂

依赖与 BGE-M3 同属 FlagEmbedding，无需新增 pip 包；只是多一份本地权重。
"""

import logging
import threading

from . import config

log = logging.getLogger("myrag.rerank")

_model = None
_load_failed = False      # 失败只记一次：反复重试只会让每次检索都卡在超时上
_lock = threading.Lock()


def get_model():
    """懒加载单例。加载失败返回 None（调用方据此跳过精排）。"""
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _lock:
        if _model is not None or _load_failed:
            return _model
        try:
            from FlagEmbedding import FlagReranker

            path = config.resolve_rerank_path()
            # use_fp16：cross-encoder 是本流程最贵的一步，M 系列芯片有 fp16 单元，
            # 用它换速度。精度影响在评测集上看不出来（对比见 eval/reports/）。
            _model = FlagReranker(path, use_fp16=True)
            log.info("reranker 就绪：%s", path)
        except Exception as e:                        # noqa: BLE001 —— 什么错都要降级
            _load_failed = True
            log.warning(
                "reranker 不可用，本次起跳过精排（检索照常，只是没有重排）：%s", e)
    return _model


def status() -> dict:
    """给 /api/health 之类的地方报状态用。"""
    return {
        "enabled": config.RERANK_ENABLED,
        "model": config.RERANK_MODEL_ID,
        "top_n": config.RERANK_TOP_N,
        "loaded": _model is not None,
        "failed": _load_failed,
    }


def _clip(text, n=None):
    """压平空白并截断待打分文本。

    两件事：
      · 换行/连续空白压成单空格 —— cross-encoder 不关心原始排版，压平能省无谓 token；
      · 截断到 RERANK_MAX_CHARS —— **这是延迟的主要来源**。cross-encoder 耗时几乎
        正比于序列长度，而完整段落可达 1024 token；实测不截断时整次检索从 0.6s
        涨到 4.7s，截断后回到可接受区间。
    """
    t = " ".join((text or "").split())
    return t[: n or config.RERANK_MAX_CHARS]


def rerank_docs(query, docs, top_n=None):
    """按与 query 的相关度给 docs 重排，返回 `(降序下标列表, 对应分数)`。

    不可用（未启用 / 未加载 / 推理失败）时返回 None —— 调用方据此保留原顺序。
    **只对前 top_n 条打分**：cross-encoder 无法预计算，全量过一遍代价太高，
    而排序的意义本来就在头部。

    **分数要一起返回**，不只是顺序：它是 0–1 的**绝对相关度**（normalize 后走
    sigmoid），与 RRF 分完全不同 —— RRF 分只反映「两路排在什么位置」，
    **不反映相关度**（实测：库里没有答案的 query 也能拿到 0.0275，
    比有答案 query 的最低分 0.0152 还高）。所以只有这个分数才可能用来判断
    「该不该告诉用户没找到」。
    """
    if not config.RERANK_ENABLED or not docs:
        return None

    n = min(len(docs), top_n or config.RERANK_TOP_N)
    if n <= 1:
        return None

    model = get_model()
    if model is None:
        return None

    try:
        pairs = [[query, _clip(d)] for d in docs[:n]]
        scores = model.compute_score(pairs, normalize=True)
        if isinstance(scores, (int, float)):          # 单条时返回标量，统一成列表
            scores = [scores]
        order = sorted(range(n), key=lambda i: -float(scores[i]))
        return order, [float(scores[i]) for i in order]
    except Exception as e:                            # noqa: BLE001
        log.warning("rerank 推理失败，保留原顺序：%s", e)
        return None


def warmup() -> dict:
    """预热（供 start.sh / 健康检查调用，把首次加载耗时挪出用户等待）。"""
    get_model()
    return status()
