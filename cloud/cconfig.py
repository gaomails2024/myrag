# -*- coding: utf-8 -*-
"""云端只读版配置：访问口令、向量服务、端口。

配置来源与环境变量优先级与 server/config.py 一致：环境变量 > 项目根 .env > 默认值。
云端机器的 .env 里需要（见 env.example）：
    MYRAG_CLOUD_PASSWORD  访问口令（**必填**，没配启动直接报错——不设默认口令）
    SILICONFLOW_API_KEY   向量/精排服务密钥（不配则搜索自动降级为关键词模式）

**绝不写死密钥**：本文件只从环境变量读，密钥只存在于云端机器的 .env，不进 git。
"""

import hashlib
import os

# 先 import server.config：它负责把项目根 .env 载入 os.environ（已存在的环境变量优先）
from server import config as _server_config  # noqa: F401


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# ---------------------------------------------------------------- 访问口令
PASSWORD = _env("MYRAG_CLOUD_PASSWORD") or ""

# Cookie 令牌：由口令派生（口令不变则令牌稳定，朋友浏览器里的登录不因重启失效；
# 换口令则全体旧 Cookie 立即失效，等于一键"踢掉所有人"）。
TOKEN = hashlib.sha256(("myrag-cloud-v1:" + PASSWORD).encode("utf-8")).hexdigest() \
    if PASSWORD else ""

COOKIE_NAME = "myrag_cloud"

# ---------------------------------------------------------------- 服务
PORT = int(_env("MYRAG_CLOUD_PORT", "8766"))

# ---------------------------------------------------------------- 向量/精排 API（硅基流动，OpenAI 兼容协议）
API_KEY = _env("SILICONFLOW_API_KEY") or ""
API_BASE = (_env("MYRAG_EMBED_BASE_URL") or "https://api.siliconflow.cn/v1").rstrip("/")
EMBED_MODEL = _env("MYRAG_EMBED_MODEL", "BAAI/bge-m3")
RERANK_API_MODEL = _env("MYRAG_RERANK_API_MODEL", "BAAI/bge-reranker-v2-m3")
API_TIMEOUT = int(_env("MYRAG_EMBED_TIMEOUT", "30"))
