# -*- coding: utf-8 -*-
"""MyRag 云端只读版：把本机知识库以只读镜像共享给朋友。

模块：
- cconfig.py      云端配置（口令 / API / 端口），环境变量驱动
- embed_api.py    查询向量走 API（server.embed.encode_query 的替换品，dense-only）
- rerank_api.py   精排走 API（server.rerank.rerank_docs 的替换品，失败自动跳过）
- app.py          FastAPI 入口：口令门槛 + 只读连接 + 复用本地版只读接口
"""
