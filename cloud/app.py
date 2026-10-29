# -*- coding: utf-8 -*-
"""MyRag 云端只读版（cloud.app）。

定位：把本机知识库以「只读镜像」共享给朋友——能搜、能看，不能改。

与本地版（server.app）的关系，三条铁律：
1. **不改 server/ 一行**：读接口的实现直接 import 自 server.app（FastAPI 的路由
   装饰器返回原函数，重新注册即可）；只有模型相关的 embed/rerank 换成 API 版。
2. **数据库只读打开**：patch 掉 db._connect，用 SQLite `mode=ro` URI——
   即使将来误注册了写接口，SQLite 这层也写不进去。
3. **一切写接口物理不存在**：不注册 ingest/patch/delete/tag-merge 等任何路由。

访问控制：共享口令 → HttpOnly Cookie。口令来自 MYRAG_CLOUD_PASSWORD，
**未配置直接拒绝启动**（绝不内置默认口令）。

运行（云端项目根目录）：
    MYRAG_CLOUD_PASSWORD=xxx uvicorn cloud.app:app --host 0.0.0.0 --port 8766
"""

import hmac
import logging
import sqlite3
import time
from contextlib import asynccontextmanager

import sqlite_vec
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server import config, db, search

from . import cconfig, embed_api, rerank_api

log = logging.getLogger("myrag.cloud")

# ================================================================ 三个 patch（都必须在注册路由前生效）

# --- patch 1：数据库只读打开（mode=ro + 不设 WAL——备份快照没有 WAL）。
# 注意不能调 PRAGMA journal_mode=WAL：那是写操作，只读连接上会报错。
def _connect_ro() -> sqlite3.Connection:
    if not config.DB_PATH.exists():
        raise RuntimeError(
            "数据库不存在：%s。云端库是快照，请先在本机跑 scripts/cloud_push.sh 推送"
            % config.DB_PATH)
    conn = sqlite3.connect("file:%s?mode=ro" % config.DB_PATH,
                           uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


db._connect = _connect_ro

# --- patch 2/3：查询向量与精排换 API 版。search.py 在调用时才查模块属性，
# 所以替换模块属性即可生效，search 排序逻辑零改动。
import server.embed as _embed_mod      # noqa: E402
import server.rerank as _rerank_mod    # noqa: E402

_embed_mod.encode_query = embed_api.encode_query
_rerank_mod.rerank_docs = rerank_api.rerank_docs

# 复用本地版的只读接口实现（写接口不在此列）
from server.app import (api_article_detail, api_articles, api_categories,  # noqa: E402
                        api_concepts, api_rules, api_structure, api_stats,
                        api_tags, api_wiki_concepts, api_wiki_detail,
                        api_wiki_graph, api_wiki_list)

_state = {"started_at": None}


def _startup_checks():
    if not cconfig.PASSWORD:
        raise RuntimeError(
            "未配置访问口令：环境变量 MYRAG_CLOUD_PASSWORD（或写进云端 .env）。"
            "公网服务没有口令等于把整个知识库裸奔，拒绝启动。")
    with db.conn_ctx() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
    print("[cloud] 库里 %d 篇文章，口令已配置，向量服务 %s" %
          (n, "已配置" if cconfig.API_KEY else "未配置（搜索将走关键词降级模式）"))
    _state["started_at"] = db.now_iso()


@asynccontextmanager
async def lifespan(_app):
    _startup_checks()
    yield


app = FastAPI(title="MyRag Cloud", version=config.VERSION, docs_url=None,
              redoc_url=None, openapi_url=None, lifespan=lifespan)

app.mount("/media", StaticFiles(directory=str(config.MEDIA_DIR)), name="media")
app.mount("/raw", StaticFiles(directory=str(config.RAW_DIR)), name="raw")


# ================================================================ 访问口令

class LoginReq(BaseModel):
    password: str


# 登录失败限速：同一 IP 10 分钟内最多错 10 次（公网口令爆破的基本闸门）
_login_fails = {}          # ip -> [失败时间戳, ...]
_LOGIN_WINDOW = 600
_LOGIN_MAX_FAILS = 10


def _too_many_fails(ip: str) -> bool:
    now = time.time()
    stamps = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_fails[ip] = stamps
    return len(stamps) >= _LOGIN_MAX_FAILS


def _record_fail(ip: str) -> None:
    _login_fails.setdefault(ip, []).append(time.time())


_LOGIN_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>个人知识库 · 访问口令</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f4f2ee;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.card{background:#fff;border-radius:16px;padding:40px 36px;width:320px;
box-shadow:0 2px 16px rgba(0,0,0,.06);text-align:center}
h1{font-size:20px;margin:0 0 6px;color:#3d3a34}
p{font-size:13px;color:#8a857c;margin:0 0 22px}
input{width:100%;box-sizing:border-box;padding:11px 14px;border:1px solid #d8d3ca;
border-radius:10px;font-size:15px;outline:none}
input:focus{border-color:#a89f8d}
button{margin-top:14px;width:100%;padding:11px;border:0;border-radius:10px;
background:#6b6456;color:#fff;font-size:15px;cursor:pointer}
button:hover{background:#57513f}
#err{margin-top:14px;color:#b3543f;font-size:13px;min-height:18px}
</style></head><body><div class="card">
<h1>个人知识库</h1><p>私有共享，请输入访问口令</p>
<input id="pw" type="password" autofocus placeholder="访问口令">
<button onclick="go()">进入</button><div id="err"></div></div>
<script>
function go(){
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:document.getElementById('pw').value})})
  .then(r=>r.json().then(j=>({s:r.status,j})))
  .then(({s,j})=>{if(s===200){location.href='/'}else{
    document.getElementById('err').textContent=j.detail||'口令不对'}});
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script></body></html>"""


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    # /robots.txt 按惯例必须免口令可读：爬虫不带口令，读不到等于没写
    if path not in ("/login", "/api/login", "/robots.txt") and \
            request.cookies.get(cconfig.COOKIE_NAME) != cconfig.TOKEN:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "需要访问口令"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    resp = await call_next(request)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.post("/api/login")
def api_login(req: LoginReq, request: Request):
    ip = request.client.host if request.client else "?"
    if _too_many_fails(ip):
        raise HTTPException(status_code=429, detail="错误次数太多，10 分钟后再试")
    if not hmac.compare_digest((req.password or "").encode("utf-8"),
                               cconfig.PASSWORD.encode("utf-8")):
        _record_fail(ip)
        raise HTTPException(status_code=401, detail="口令不对")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(cconfig.COOKIE_NAME, cconfig.TOKEN,
                    max_age=30 * 24 * 3600, httponly=True, samesite="lax",
                    path="/")
    return resp


@app.get("/login")
def login_page():
    return HTMLResponse(_LOGIN_PAGE)


# 全站拒绝收录。**只是君子协定**（守规矩的爬虫遵守，恶意爬虫不理）——
# 真防线是口令门槛：没有口令，任何页面内容都拿不到。
_ROBOTS_TXT = """User-agent: *
Disallow: /
"""


@app.get("/robots.txt")
def robots_txt():
    return Response(_ROBOTS_TXT, media_type="text/plain")


# ================================================================ 只读接口

@app.get("/api/health")
def api_health():
    """云端健康检查：无模型概念，embed 走 API。"""
    with db.conn_ctx() as conn:
        c = db.counts(conn)
    return {"status": "ok", "version": config.VERSION + "-cloud",
            "counts": c, "started_at": _state["started_at"],
            "db": str(config.DB_PATH),
            "embed_api": {"configured": bool(cconfig.API_KEY),
                          "model": cconfig.EMBED_MODEL}}


@app.get("/api/search")
def api_search(q: str = "", mode: str = "hybrid", category: str | None = None,
               tag: str | None = None, date_from: str | None = None,
               date_to: str | None = None, limit: int = 20):
    """搜索。向量服务不可用时降级为关键词 LIKE，并如实标注 keyword_fallback。"""
    if mode not in ("hybrid", "dense", "sparse"):
        mode = "hybrid"
    limit = max(1, min(int(limit or 20), 100))
    q = (q or "").strip()
    with db.conn_ctx() as conn:
        try:
            return search.search(conn, q, mode, category, tag,
                                 date_from, date_to, limit)
        except embed_api.EmbedApiError as e:
            log.warning("向量服务不可用，降级关键词搜索：%s", e)
            out = _like_search(conn, q, limit)
            out["degraded"] = str(e)
            return out


def _like_search(conn, q: str, limit: int) -> dict:
    """关键词降级：标题/摘要/标签 LIKE。结构与正常结果一致，match_type=fulltext。"""
    if not q:
        return {"query": q, "mode": "keyword_fallback", "total": 0, "hits": []}
    like = "%" + q + "%"
    rows = conn.execute(
        """SELECT a.id, a.title, a.category, a.source, a.published_at,
                  a.collected_at, a.url, a.summary
           FROM articles a
           WHERE a.status <> 'failed'
             AND (a.title LIKE ? OR a.summary LIKE ? OR a.tags LIKE ?)
           ORDER BY COALESCE(a.published_at, a.collected_at) DESC
           LIMIT ?""", (like, like, like, limit)).fetchall()
    hits = [{"article_id": r["id"], "title": r["title"], "category": r["category"],
             "source": r["source"], "published_at": r["published_at"],
             "collected_at": r["collected_at"], "url": r["url"],
             "score": 0.0, "rerank_score": None, "match_type": "fulltext",
             "matched_chunks": [{"heading_path": "关键词匹配（降级模式）",
                                 "snippet": r["summary"] or r["title"] or "",
                                 "rank_dense": None, "rank_sparse": None}]}
            for r in rows]
    return {"query": q, "mode": "keyword_fallback", "total": len(hits),
            "hits": hits, "fulltext_hits": len(hits)}


# 以下全部 re-register 自 server.app 的只读实现
app.get("/api/categories")(api_categories)
app.get("/api/rules")(api_rules)
app.get("/api/concepts")(api_concepts)
app.get("/api/articles")(api_articles)
app.get("/api/articles/{article_id}")(api_article_detail)
app.get("/api/structure")(api_structure)
app.get("/api/wiki/concepts")(api_wiki_concepts)
app.get("/api/wiki/graph")(api_wiki_graph)
app.get("/api/wiki")(api_wiki_list)
app.get("/api/wiki/{slug}")(api_wiki_detail)
app.get("/api/tags")(api_tags)
app.get("/api/stats")(api_stats)


# ================================================================ 工作台

@app.get("/")
def index():
    if not config.INDEX_HTML.exists():
        return JSONResponse({"status": "ok", "note": "web/index.html 缺失"})
    return FileResponse(str(config.INDEX_HTML))
