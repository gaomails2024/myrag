# -*- coding: utf-8 -*-
"""LLM 客户端（PRD §5.7）：给「知识编译 Wiki」用，走 OpenAI 兼容协议。

**为什么单独一个模块**：到 3.1 为止 MyRag 后端不调任何大模型（判类/摘要都在
Skill 侧由 Agent 做），这是第一次。把它独立出来、只在调用时才检查配置，
才能保证「没配 key 也不影响检索与入库」—— 万一这个供应商挂了或要换，
只有 wiki 编译这一条路受影响。

**为什么不自己写 HTTP**：httpx 已在 requirements.txt 里、server/verify.py 也
在用同一个客户端，没有理由再引一个 SDK 或手搓 urllib。

错误一律翻译成人话：这个模块的读者是用户本人，报错要能直接告诉他「该改哪」，
而不是抛一个 httpx 的堆栈。**但 key 本身绝不进报错信息。**
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from . import config

# 重试次数（不含首次调用）。只对「瞬时故障」重试：429 与 5xx。
# 401/403/404 是配置错了，重试多少次都一样，所以立刻失败。
MAX_RETRIES = 2
RETRY_SLEEP = 2.0        # 秒；指数退避 base


class LLMError(RuntimeError):
    """编译失败。调用方收到它时**必须放弃写入**，不留半成品页。"""


def missing_reason() -> str | None:
    """没配好就说清楚缺什么；配好了返回 None。

    单独抽出来是因为「为什么这次不编译」需要在大纲阶段就报给用户，
    而不是等真正发请求时才 401。
    """
    if not config.LLM_BASE_URL:
        return ("还没配 LLM 地址。请在 .env 里加一行：\n"
                "    MYRAG_LLM_BASE_URL=https://api.deepseek.com/v1\n"
                "（任何 OpenAI 兼容端点都行：DeepSeek / Qwen / GPT…）")
    if not config.LLM_API_KEY:
        return ("还没配 LLM 密钥。请在 .env 里加一行：\n"
                "    MYRAG_LLM_API_KEY=<你的 key>")
    return None


def _explain(status: int, body: str) -> str:
    """把 HTTP 状态码翻译成「该改哪」。body 只取前 200 字便于排错。"""
    hint = {
        400: "请求被拒。多半是 base_url 写错了（要带 /v1 这类后缀），或该模型不支持 JSON 输出。",
        401: "密钥无效或没权限。去供应商后台确认 key 是不是被撤销、复制时有没有多带空格。",
        403: "没有访问这个模型的权限。换一个你有权限的模型名，或确认账号已开通该服务。",
        404: "地址或模型名不对。注意两处：base_url 是否完整、这个供应商**有没有这个模型名**。",
        413: "素材太长。调小 .env 里的 MYRAG_LLM_MAX_CHARS，或用 --max-chars 少放几篇。",
        429: "被限流或余额不足。看供应商后台的额度与限流策略，过几分钟再试。",
    }
    head = hint.get(status, "供应商返回了错误状态 %d" % status)
    # 400 里混着一种很容易误判的情况：**提示词里没有 "json" 这个词**。
    # DeepSeek 的 json_object 模式硬性要求提示词提到 json，否则直接拒整个请求 ——
    # 而报错原文（英文）完全看不出是提示词的问题，很容易被当成「模型不支持 JSON」。
    if "must contain the word" in (body or "").lower() and "json" in (body or "").lower():
        head = ("提示词里必须出现 \"json\" 这个词 —— 这是 DeepSeek 的 json_object 模式"
                "的硬性要求，不满足会拒整个请求。（当前提示词里没有它）")
    return "%s\n供应商返回：%s" % (head, (body or "").strip()[:200])


def chat_json(system: str, user: str, *, temperature: float = 0.3,
              max_tokens: int | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """发一次对话，要求返回 JSON。返回 (解析后的 dict, token 用量)。

    为什么要「要求 JSON」而不是让它自由写 md 再用正则抠引用号：
    自由文本里模型会把引用写成「（见 3）」「[3]」「第三篇」各种形状，
    正则一旦对不上，页面上那些角标就成了死链 —— 而引用可点回原文是这一页
    唯一不可妥协的价值。结构化输出把「哪个结论出自哪篇」的映射交给代码强校验。
    """
    reason = missing_reason()
    if reason:
        raise LLMError(reason)
    # 上面已经判过配置，但编译器不知道 —— 这里再收一次窄，
    # 免得下面每一处拼接都变成「可能在 None 上调 rstrip」。
    base_url = config.LLM_BASE_URL
    api_key = config.LLM_API_KEY
    if not base_url or not api_key:       # 兜住「判完之后配置被改掉」这种极小概率
        raise LLMError(missing_reason() or "LLM 配置不完整。")

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
        "User-Agent": "MyRag/%s" % config.VERSION,
    }
    payload = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    # 推理开关：只在明确要 off/on 时才带上这个字段（DeepSeek 系），
    # auto 表示「不发」—— OpenAI / Qwen 等标准端点不认它，发过去会 400。
    if config.LLM_THINKING in ("off", "on"):
        payload["thinking"] = {"type": "disabled" if config.LLM_THINKING == "off"
                               else "enabled"}

    last: str = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=config.LLM_TIMEOUT, headers=headers) as c:
                r = c.post(url, json=payload)
        except httpx.TimeoutException:
            last = ("等了 %d 秒还没等到响应。要么素材太长（调小 MYRAG_LLM_MAX_CHARS），"
                    "要么供应商当时不在线。" % config.LLM_TIMEOUT)
        except httpx.HTTPError as e:
            last = "连不上 %s：%s: %s" % (url, type(e).__name__, e)
        else:
            if r.status_code == 200:
                try:
                    return _parse(r.json(), payload["max_tokens"])
                except LLMError as e:
                    # 解析失败是**模型侧偶发**（偶发截断/偶发坏 JSON），
                    # 重试通常就好了 —— 不重试等于把一次抖动当成永久失败。
                    # 但 HTTP 4xx 那类配置错误上面已经立刻 raise 了，走不到这里。
                    last = str(e)
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_SLEEP * (2 ** attempt))
                        continue
                    raise
            last = _explain(r.status_code, r.text)
            if r.status_code not in (429, 500, 502, 503, 504):
                raise LLMError(last)      # 配置类错误，重试没有意义

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_SLEEP * (2 ** attempt))

    raise LLMError("重试 %d 次仍失败。%s" % (MAX_RETRIES, last))


def _parse(resp: dict[str, Any], max_tokens: int) -> tuple[dict[str, Any], dict[str, int]]:
    """拆出 choices[0].message.content 并解析成 dict。

    模型有时会在 JSON 外面裹一层 ```json 围栏，或前后带一句说明 —— 两种都见过，
    属于「格式没对齐但内容是好的」，剥掉围栏再试；实在不像 JSON 才报错。
    """
    try:
        content = resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError("模型返回里找不到 choices[0].message.content，"
                       "这个端点可能不是标准 OpenAI 兼容格式。")
    usage = resp.get("usage") or {}
    meta = {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "reasoning_tokens": int((usage.get("completion_tokens_details") or {})
                                    .get("reasoning_tokens") or 0),
            "max_tokens": max_tokens}

    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # 先看模型是不是**因为写不下**才停的 —— 这是最常见的一种「JSON 不完整」，
    # 而且它和「格式乱」要开的药完全不同。实测踩过：deepseek-flash 在 31 篇素材
    # 上先花 7999 token 推理，max_tokens=8000 被吃光，正文一个字没输出。
    finish = (resp.get("choices") or [{}])[0].get("finish_reason")
    if finish == "length":
        raise LLMError(
            "输出被模型截断了（finish_reason=length）：它把 max_tokens=%d 用完了还没写完，"
            "所以 JSON 是半截的。\n"
            "调大 MYRAG_LLM_MAX_TOKENS（现在 %d），"
            "或把 MYRAG_LLM_THINKING 设为 off（推理会先吃掉一大块预算）。"
            % (meta["max_tokens"], config.LLM_MAX_TOKENS))
    try:
        obj = json.loads(text)
    except ValueError:      # JSONDecodeError 是 ValueError 的子类，一句就够
        raise LLMError(
            "模型返回的不是合法 JSON，前 200 字长这样：\n%s\n"
            "（这是模型侧的偶发问题，已自动重试过；仍不行就调低 temperature，"
            "或换个大模型。）"
            % text[:200])
    if not isinstance(obj, dict):
        raise LLMError("模型返回的 JSON 顶层不是对象（应为 {...}），"
                       "前 120 字：%s" % text[:120])
    return obj, meta