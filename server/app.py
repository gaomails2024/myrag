# -*- coding: utf-8 -*-
"""MyRag 本地后端：FastAPI 入口与路由（PRD §9）。

Base http://127.0.0.1:8765，全部 JSON。
**本后端不提供抓取接口** —— 抓取在 Skill 侧脚本完成，后端只接收抓取结果。
"""

import logging
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from . import config, db, embed, ingest, search, verify

# 异常必须留**堆栈**，不能只把「类名: 消息」丢给 HTTP 响应。
#
# 教训（2026-09-22）：一次入库返回 500，响应里只有
# `PermissionError: Sensitive content access was denied.`，而日志里**什么都没有** ——
# 看起来像权限问题，实际是 WorkBuddy 注入的 sitecustomize.py 删除守卫（见 SKILL.md 第 0 步）。
# 因为没有堆栈，只能靠写探针去猜，白花了 10 分钟以上。
# **加上下面这几行，下次一行日志就能定位。**
#
# 输出会落 data/server.log（start.sh 里是 `>>"$LOG" 2>&1`）。
log = logging.getLogger("myrag")

CST = timezone(timedelta(hours=8))

_state = {"started_at": None, "model_ready": False, "warmup": None}

_md = MarkdownIt("commonmark", {"linkify": False, "breaks": False}).enable("table")
_DISPLAY_IMG = re.compile(r"\.\./\.\./media/")


def _warmup():
    try:
        _state["warmup"] = embed.warmup()
        _state["model_ready"] = True
    except Exception as e:
        _state["warmup"] = {"error": "%s: %s" % (type(e).__name__, e)}


@asynccontextmanager
async def lifespan(_app):
    config.ensure_dirs()
    db.init_db()
    with db.tx() as conn:
        seeded = db.seed_categories(conn)
        seeded_rules = db.seed_rules(conn)
        swept = ingest.purge_orphans(conn)
    if seeded:
        print("[startup] 已写入出厂默认分类 %d 个（可在工作台「分类设置」里改）" % seeded)
    if seeded_rules:
        print("[startup] 已写入出厂入库规则 %d 条（可在工作台「入库规则」里改）" % seeded_rules)
    if swept["vec"] or swept["sparse"]:
        print("[startup] 清理孤儿向量：vec=%d sparse=%d" % (swept["vec"], swept["sparse"]))
    _state["started_at"] = db.now_iso()
    threading.Thread(target=_warmup, daemon=True).start()
    yield


app = FastAPI(title="MyRag", version=config.VERSION, lifespan=lifespan)

config.ensure_dirs()
app.mount("/raw", StaticFiles(directory=str(config.RAW_DIR)), name="raw")
app.mount("/media", StaticFiles(directory=str(config.MEDIA_DIR)), name="media")


# ================================================================ 请求模型

class CheckUrlsReq(BaseModel):
    url_canons: list[str] = Field(default_factory=list)


class IngestReq(BaseModel):
    stage_token: str
    category: str | None = None
    title: str | None = None
    source: str | None = None
    author: str | None = None
    published_at: str | None = None
    url: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    review_flag: int | None = 0
    review_due: str | None = None
    app_card: dict | None = None
    concepts: list[dict] | None = None


class IngestTextReq(BaseModel):
    title: str | None = None
    url: str | None = None
    category: str | None = None
    raw_content: str | None = None
    published_at: str | None = None
    source: str | None = None


class PatchReq(BaseModel):
    category: str | None = None
    tags: list[str] | None = None
    summary: str | None = None
    title: str | None = None
    verdict: str | None = None
    boundary: str | None = None
    deploy_note: str | None = None
    repo_url: str | None = None
    review_flag: int | None = None
    review_due: str | None = None


# ================================================================ 基础

@app.get("/api/health")
def health():
    with db.conn_ctx() as conn:
        c = db.counts(conn)
    return {"status": "ok", "version": config.VERSION, "counts": c,
            "model_ready": _state["model_ready"], "started_at": _state["started_at"],
            "db": str(config.DB_PATH), "warmup": _state["warmup"]}


@app.post("/api/check-urls")
def check_urls(req: CheckUrlsReq):
    """批量查重：只收 url_canon（原始分享链接带追踪参数，直接比会漏）。"""
    dups = []
    with db.conn_ctx() as conn:
        for uc in req.url_canons:
            r = db.find_by_canon(conn, uc)
            if r:
                dups.append({"url_canon": uc, "id": r["id"]})
    return {"dups": dups}


# ================================================================ 入库

def _ingest_response(res):
    status = 200
    if not res.get("ok") and res.get("fail_reason") in (
            "stage_not_found", "empty_content", "too_large"):
        status = 400
    return JSONResponse(content=res, status_code=status)


@app.post("/api/ingest")
def api_ingest(req: IngestReq):
    """读 staging 磁盘副本 → 分配 id → 落原文与配图 → 切段+向量化+写库。

    幂等：同一 stage_token 或同一 url_canon 重复提交 → dup=true + HTTP 200（不返 409）。
    """
    payload = req.model_dump()
    try:
        with db.tx() as conn:
            res = ingest.do_ingest(conn, payload)
    except Exception as e:
        log.exception("/api/ingest 失败（payload 见上一条请求日志）")
        raise HTTPException(status_code=500,
                            detail={"ok": False, "fail_reason": "internal",
                                    "detail": "%s: %s" % (type(e).__name__, e)})
    return _ingest_response(res)


@app.post("/api/ingest-text")
def api_ingest_text(req: IngestTextReq):
    """辅路径：直接提交正文文本，不抓取、不判类、summary 留空（PRD §9 / §10.3）。"""
    try:
        with db.tx() as conn:
            res = ingest.do_ingest_text(conn, req.model_dump())
    except Exception as e:
        log.exception("/api/ingest-text 失败")
        raise HTTPException(status_code=500,
                            detail={"ok": False, "fail_reason": "internal",
                                    "detail": "%s: %s" % (type(e).__name__, e)})
    return _ingest_response(res)


# ================================================================ 分类配置（PRD §5.5）

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9]{1,3}$")


class CategoryCreateReq(BaseModel):
    key: str
    name: str
    prefix: str
    criteria: str = ""
    principles: str = ""
    retrieval: str = "rag"
    features: list[str] = Field(default_factory=list)
    is_default: int = 0
    sort_order: int = 0
    display: str = ""


class CategoryPatchReq(BaseModel):
    name: str | None = None
    prefix: str | None = None
    criteria: str | None = None
    principles: str | None = None
    retrieval: str | None = None
    features: list[str] | None = None
    is_default: int | None = None
    sort_order: int | None = None
    display: str | None = None


def _bad_features(features) -> list:
    return [f for f in (features or []) if f not in config.FEATURE_NAMES]


@app.get("/api/categories")
def api_categories():
    """分类配置 + 每类文章数。工作台页签、详情改类按钮、Skill 判类都由它驱动。"""
    with db.conn_ctx() as conn:
        cats = db.get_categories(conn)
        used = {r["category"]: r["c"] for r in conn.execute(
            "SELECT category, COUNT(*) c FROM articles GROUP BY category")}
    for c in cats:
        c["count"] = used.get(c["key"], 0)
    return {
        "total": len(cats), "items": cats,
        "retrieval_modes": [{"key": k, "name": config.RETRIEVAL_NAMES[k]}
                            for k in config.RETRIEVAL_MODES],
        "features": [{"key": k, "name": v} for k, v in config.FEATURE_NAMES.items()],
        "display_modes": [{"key": m, "name": config.DISPLAY_NAMES[m]}
                          for m in config.DISPLAY_MODES],
    }


@app.post("/api/categories")
def api_category_create(req: CategoryCreateReq):
    key = (req.key or "").strip().lower()
    name = (req.name or "").strip()
    prefix = (req.prefix or "").strip().upper()
    if not _KEY_RE.match(key):
        raise HTTPException(status_code=400, detail={
            "ok": False, "detail": "key 需为 2–16 位小写字母/数字/下划线，且以字母开头"
                                   "（它写进编号与数据，入库后不建议改）"})
    if not name:
        raise HTTPException(status_code=400, detail={"ok": False, "detail": "名称不能为空"})
    if not _PREFIX_RE.match(prefix):
        raise HTTPException(status_code=400, detail={
            "ok": False, "detail": "编号前缀只能是 1–3 位字母或数字"})
    if req.retrieval not in config.RETRIEVAL_MODES:
        raise HTTPException(status_code=400, detail={
            "ok": False, "detail": "检索方式只能是 %s" % " / ".join(config.RETRIEVAL_MODES)})
    if (req.display or "") not in config.DISPLAY_MODES:
        raise HTTPException(status_code=400, detail={
            "ok": False, "detail": "展示样式取值不合法"})
    bad = _bad_features(req.features)
    if bad:
        raise HTTPException(status_code=400, detail={
            "ok": False, "detail": "不支持的能力项：%s" % ", ".join(bad)})

    is_default = 1 if req.is_default else 0
    now = db.now_iso()
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM categories WHERE key = ?", (key,)).fetchone():
            raise HTTPException(status_code=400, detail={
                "ok": False, "detail": "key「%s」已存在" % key})
        if conn.execute("SELECT 1 FROM categories WHERE prefix = ?", (prefix,)).fetchone():
            raise HTTPException(status_code=400, detail={
                "ok": False, "detail": "前缀「%s」已被其它分类占用" % prefix})
        if is_default:
            conn.execute("UPDATE categories SET is_default = 0")
        conn.execute(
            """INSERT INTO categories
               (key, name, prefix, criteria, principles, retrieval, features,
                is_default, sort_order, display, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, name, prefix, req.criteria or "", req.principles or "",
             req.retrieval, db.jdumps(req.features or []), is_default,
             int(req.sort_order or 0), req.display or "", now, now))
        db.log_ingest(conn, None, None, "category_add",
                      {"key": key, "name": name, "prefix": prefix,
                       "retrieval": req.retrieval, "features": req.features})
    return {"ok": True, "key": key, "name": name}


@app.patch("/api/categories/{key}")
def api_category_update(key: str, req: CategoryPatchReq):
    """改分类。key 不可改（它已被写进文章编号）；prefix 只影响**未来**的新编号。"""
    body = {k: v for k, v in req.model_dump().items() if v is not None}
    if not body:
        raise HTTPException(status_code=400, detail="no fields")
    if "name" in body and not str(body["name"]).strip():
        raise HTTPException(status_code=400, detail={"ok": False, "detail": "名称不能为空"})
    if "prefix" in body:
        body["prefix"] = str(body["prefix"]).strip().upper()
        if not _PREFIX_RE.match(body["prefix"]):
            raise HTTPException(status_code=400, detail={
                "ok": False, "detail": "编号前缀只能是 1–3 位字母或数字"})
    if "retrieval" in body and body["retrieval"] not in config.RETRIEVAL_MODES:
        raise HTTPException(status_code=400, detail={
            "ok": False, "detail": "检索方式只能是 %s" % " / ".join(config.RETRIEVAL_MODES)})
    if "display" in body and (body["display"] or "") not in config.DISPLAY_MODES:
        raise HTTPException(status_code=400, detail={
            "ok": False, "detail": "展示样式取值不合法"})
    if "features" in body:
        bad = _bad_features(body["features"])
        if bad:
            raise HTTPException(status_code=400, detail={
                "ok": False, "detail": "不支持的能力项：%s" % ", ".join(bad)})

    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM categories WHERE key = ?", (key,)).fetchone():
            raise HTTPException(status_code=404, detail="category not found")
        if "prefix" in body:
            dup = conn.execute("SELECT key FROM categories WHERE prefix = ? AND key <> ?",
                               (body["prefix"], key)).fetchone()
            if dup:
                raise HTTPException(status_code=400, detail={
                    "ok": False, "detail": "前缀「%s」已被「%s」占用" % (body["prefix"], dup["key"])})
        if body.get("is_default"):
            conn.execute("UPDATE categories SET is_default = 0")

        sets, params = [], []
        for k, v in body.items():
            if k == "features":
                v = db.jdumps(v)
            elif k == "is_default":
                v = int(bool(v))
            elif k == "name":
                v = str(v).strip()
            sets.append("%s = ?" % k)
            params.append(v)
        sets.append("updated_at = ?")
        params.append(db.now_iso())
        conn.execute("UPDATE categories SET %s WHERE key = ?" % ", ".join(sets),
                     params + [key])
        db.log_ingest(conn, None, None, "category_update",
                      {"key": key, "fields": sorted(body.keys())})
    return {"ok": True, "key": key}


@app.delete("/api/categories/{key}")
def api_category_delete(key: str, confirm: str = Query(default="")):
    """删分类。该类下还挂着文章就拒绝 —— 不做静默迁移（那等于替用户改数据归属）。"""
    if confirm != "yes":
        raise HTTPException(status_code=400, detail="need confirm=yes")
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM categories WHERE key = ?", (key,)).fetchone():
            raise HTTPException(status_code=404, detail="category not found")
        n = conn.execute("SELECT COUNT(*) c FROM articles WHERE category = ?",
                         (key,)).fetchone()["c"]
        if n:
            raise HTTPException(status_code=400, detail={
                "ok": False, "detail": "该分类下还有 %d 篇文章，先把它们改成别的分类再删" % n})
        total = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
        if total <= 1:
            raise HTTPException(status_code=400, detail={
                "ok": False, "detail": "至少要保留一个分类"})
        conn.execute("DELETE FROM categories WHERE key = ?", (key,))
        db.log_ingest(conn, None, None, "category_delete", {"key": key})
    return {"ok": True, "key": key}


# ================================================================ 入库规则（PRD §5.6）

class RulePatchReq(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


@app.get("/api/rules")
def api_rules():
    """可配置的入库规则（业务规则）。

    **只有业务规则**：摘要多长、标签几个、什么情况进待复核 —— 这些改错了只是
    "结果不合口味"。技术规则（抓取解析、URL 规范化、切段参数）不暴露，因为
    改错了是系统直接坏掉（抓不到正文 / 查重失效）。
    """
    with db.conn_ctx() as conn:
        cur = db.get_rules(conn)
    items = []
    for d in config.RULE_DEFS:
        v = cur.get(d["key"], d["default"])
        items.append({
            "key": d["key"], "name": d["name"], "type": d.get("type", "text"),
            "hint": d.get("hint", ""), "default": d["default"],
            "value": v, "changed": v != d["default"],
        })
    return {"total": len(items), "items": items}


@app.patch("/api/rules")
def api_rules_update(req: RulePatchReq):
    known = {d["key"]: d for d in config.RULE_DEFS}
    bad = [k for k in req.values if k not in known]
    if bad:
        raise HTTPException(status_code=400, detail={
            "ok": False, "detail": "未知规则：%s" % ", ".join(bad)})

    cleaned = {}
    for k, v in req.values.items():
        v = "" if v is None else str(v).strip()
        if known[k].get("type") == "int":
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail={
                    "ok": False, "detail": "「%s」必须是整数" % known[k]["name"]})
            if iv < 1 or iv > 10000:
                raise HTTPException(status_code=400, detail={
                    "ok": False, "detail": "「%s」超出合理范围（1–10000）" % known[k]["name"]})
            v = str(iv)
        elif not v:
            raise HTTPException(status_code=400, detail={
                "ok": False, "detail": "「%s」不能为空" % known[k]["name"]})
        cleaned[k] = v

    now = db.now_iso()
    with db.tx() as conn:
        for k, v in cleaned.items():
            conn.execute(
                """INSERT INTO rules (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                (k, v, now))
        db.log_ingest(conn, None, None, "rules_update", {"keys": sorted(cleaned.keys())})
    return {"ok": True, "updated": sorted(cleaned.keys())}


# ================================================================ 检索

@app.get("/api/search")
def api_search(q: str = "", mode: str = "hybrid", category: str | None = None,
               tag: str | None = None, date_from: str | None = None,
               date_to: str | None = None, limit: int = 20):
    if mode not in ("hybrid", "dense", "sparse"):
        mode = "hybrid"
    limit = max(1, min(int(limit or 20), 100))
    with db.conn_ctx() as conn:
        return search.search(conn, q, mode, category, tag, date_from, date_to, limit)


@app.get("/api/concepts")
def api_concepts(q: str | None = None):
    """概念列表（工作台模块 4：技术类视图用）。PRD §9 未列，为视图需要补充。"""
    with db.conn_ctx() as conn:
        sql = """SELECT c.name, c.definition, c.article_id, c.related,
                        a.title AS article_title, a.category
                 FROM concepts c LEFT JOIN articles a ON a.id = c.article_id"""
        params = []
        if q:
            sql += " WHERE (c.name LIKE ? OR c.definition LIKE ?)"
            params = ["%" + q + "%", "%" + q + "%"]
        sql += " ORDER BY c.name"
        rows = [dict(r) for r in conn.execute(sql, params)]
        for r in rows:
            r["related"] = db.jloads(r.get("related"), [])
        return {"total": len(rows), "items": rows}


# ================================================================ 文章

def _article_row_to_item(r) -> dict:
    d = dict(r)
    d["tags"] = db.jloads(d.get("tags"), [])
    d["attachments"] = db.jloads(d.get("attachments"), [])
    return d


@app.get("/api/articles")
def api_articles(category: str | None = None, tag: str | None = None,
                 q: str | None = None, review_flag: int | None = None,
                 queue: str | None = None, order: str = "desc",
                 page: int = 1, page_size: int = 20):
    """列表。queue=review → review_flag=1 或 review_due 已到期（工作台模块 3 用）。

    order: desc（最新在前，默认）/ asc（最早在前），按**入库时间**排序。
    """
    where, params = ["a.status <> 'failed'"], []
    if category:
        where.append("a.category = ?")
        params.append(category)
    if tag:
        where.append("EXISTS (SELECT 1 FROM json_each(a.tags) WHERE json_each.value = ?)")
        params.append(tag)
    if q:
        where.append("(a.title LIKE ? OR a.summary LIKE ? OR a.source LIKE ?)")
        like = "%" + q + "%"
        params += [like, like, like]
    if review_flag is not None:
        where.append("a.review_flag = ?")
        params.append(int(review_flag))
    if queue == "review":
        where.append("(a.review_flag = 1 OR (a.review_due IS NOT NULL AND a.review_due <= ?))")
        params.append(db.now_iso())

    fsql = " AND ".join(where)
    direction = "ASC" if str(order or "desc").lower() == "asc" else "DESC"
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 200))

    with db.conn_ctx() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM articles a WHERE " + fsql, params).fetchone()["c"]
        rows = conn.execute(
            """SELECT a.*, c.repo_url, c.license, c.stars, c.verdict, c.verified_at,
                      (SELECT COUNT(*) FROM chunks ch WHERE ch.article_id = a.id) AS chunk_count,
                      (SELECT COUNT(*) FROM concepts cn WHERE cn.article_id = a.id) AS concept_count
               FROM articles a LEFT JOIN app_cards c ON c.article_id = a.id
               WHERE %s
               ORDER BY a.collected_at %s, a.id %s
               LIMIT ? OFFSET ?""" % (fsql, direction, direction),
            params + [page_size, (page - 1) * page_size]).fetchall()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_article_row_to_item(r) for r in rows]}


@app.get("/api/articles/{article_id}")
def api_article_detail(article_id: str):
    with db.conn_ctx() as conn:
        row = conn.execute(
            """SELECT a.*, c.repo_url, c.license, c.language, c.stars, c.last_commit,
                      c.last_release, c.created_date, c.boundary, c.deploy_note,
                      c.verdict, c.evidence, c.verified_at
               FROM articles a LEFT JOIN app_cards c ON c.article_id = a.id
               WHERE a.id = ?""", (article_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="article not found")

        d = dict(row)
        d["tags"] = db.jloads(d.get("tags"), [])
        d["attachments"] = db.jloads(d.get("attachments"), [])
        d["evidence"] = db.jloads(d.get("evidence"), {})

        # ordinal=0 是元数据段（供检索命中 License/标题等），不算正文分段
        chunks = [dict(r) for r in conn.execute(
            """SELECT id, ordinal, heading_path, text, token_count FROM chunks
               WHERE article_id = ? AND ordinal > 0 ORDER BY ordinal""", (article_id,))]
        # 分段正文里也带 `../../media/<id>/x.png`，工作台要直接渲染就得换成 /media/
        for c in chunks:
            c["text"] = _DISPLAY_IMG.sub("/media/", c["text"] or "")
        meta = conn.execute(
            "SELECT text FROM chunks WHERE article_id = ? AND ordinal = 0",
            (article_id,)).fetchone()

        md = ""
        if d.get("raw_path"):
            p = config.DATA_DIR / d["raw_path"]
            if p.exists():
                md = p.read_text(encoding="utf-8")
        # 展示用：把 `../../media/<id>/x.png` 换成可直取的 /media/<id>/x.png
        display_md = _DISPLAY_IMG.sub("/media/", md)
        d["content_md"] = display_md
        d["content_html"] = _md.render(display_md) if display_md else ""
        d["raw_url"] = "/%s" % d["raw_path"] if d.get("raw_path") else None

        concepts = [dict(r) for r in conn.execute(
            "SELECT name, definition, related FROM concepts WHERE article_id = ?",
            (article_id,))]
        for c in concepts:
            c["related"] = db.jloads(c.get("related"), [])

        # 概念互链：concepts.name 是 UNIQUE（一个概念只挂一篇文章），所以
        # 「共同概念名」永远查不到别的文章。真正的链接要沿 related 概念名走图：
        # 本文概念的 related 名字 → 挂着这些名字的其它文章。
        related_articles = []
        names = set(c["name"] for c in concepts)
        for c in concepts:
            names.update(c.get("related") or [])
        if names:
            marks = ",".join("?" * len(names))
            related_articles = [dict(r) for r in conn.execute(
                """SELECT DISTINCT a.id, a.title, a.category, a.summary FROM articles a
                   JOIN concepts c ON c.article_id = a.id
                   WHERE c.name IN (%s) AND a.id <> ?
                   ORDER BY a.id LIMIT 20""" % marks,
                list(names) + [article_id])]

        d["chunks"] = chunks
        d["meta_chunk"] = meta["text"] if meta else None
        d["concepts"] = concepts
        d["related_articles"] = related_articles
    return d


@app.patch("/api/articles/{article_id}")
def api_patch(article_id: str, req: PatchReq):
    """人工修正：改 category / tags / summary / verdict 等，并清 review_flag。"""
    body = {k: v for k, v in req.model_dump().items() if v is not None}
    if not body:
        raise HTTPException(status_code=400, detail="no fields")

    with db.tx() as conn:
        row = conn.execute("SELECT id, category FROM articles WHERE id = ?",
                           (article_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="article not found")
        old_category = row["category"]
        cats = db.category_map(conn)

        sets, params = [], []
        if "category" in body:
            if body["category"] not in cats:
                raise HTTPException(status_code=400, detail="bad category")
            sets.append("category = ?")
            params.append(body["category"])
        if "title" in body:
            sets.append("title = ?")
            params.append(body["title"])
        if "summary" in body:
            sets.append("summary = ?")
            params.append(body["summary"])
        if "tags" in body:
            sets.append("tags = ?")
            params.append(db.jdumps(body["tags"]))
        if "review_due" in body:
            sets.append("review_due = ?")
            params.append(body["review_due"])
        # 改类默认清掉待复核标记（PRD §5.4）
        rf = body.get("review_flag", 0)
        sets.append("review_flag = ?")
        params.append(int(rf))
        sets.append("updated_at = ?")
        params.append(db.now_iso())
        conn.execute("UPDATE articles SET %s WHERE id = ?" % ", ".join(sets),
                     params + [article_id])

        new_category = body.get("category", old_category)
        ac_fields = {k: body[k] for k in
                     ("verdict", "boundary", "deploy_note", "repo_url") if k in body}
        new_feats = (cats.get(new_category) or {}).get("features") or []
        if "repo_card" in new_feats or ac_fields:
            conn.execute(
                "INSERT INTO app_cards (article_id) VALUES (?) ON CONFLICT(article_id) DO NOTHING",
                (article_id,))
        if ac_fields:
            conn.execute("UPDATE app_cards SET %s WHERE article_id = ?"
                         % ", ".join("%s = ?" % k for k in ac_fields),
                         list(ac_fields.values()) + [article_id])

        db.log_ingest(conn, None, article_id, "patch",
                      {"fields": sorted(body.keys()),
                       "category": "%s→%s" % (old_category, new_category)})
        # 修正不做反向迁移：详情页按需渲染，无需重建产物
        return {"ok": True, "id": article_id, "category": new_category}


@app.post("/api/articles/{article_id}/confirm")
def api_confirm_review(article_id: str):
    """确认复核无误：清 review_flag；带复评周期的分类顺延一个周期。

    为什么不交给前端拼 PATCH：
    待复核队列有**两个来源** —— `review_flag = 1`，以及 `review_due` 到期。
    只清 flag 对"复评到期"的条目无效（它还会因为 review_due 留在队列里），
    所以"顺延周期"这一步必须在服务端做，前端不该知道这个规则。
    """
    with db.tx() as conn:
        row = conn.execute(
            "SELECT category, review_flag, review_due FROM articles WHERE id = ?",
            (article_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="article not found")

        sets, params = ["review_flag = 0"], []
        next_due = None
        if row["review_due"]:
            next_due = (datetime.now(CST) + timedelta(days=config.REVIEW_DUE_DAYS)).isoformat(
                timespec="seconds")
            sets.append("review_due = ?")
            params.append(next_due)
        sets.append("updated_at = ?")
        params.append(db.now_iso())

        conn.execute("UPDATE articles SET %s WHERE id = ?" % ", ".join(sets),
                     params + [article_id])
        db.log_ingest(conn, None, article_id, "confirm_review",
                      {"had_flag": row["review_flag"],
                       "old_due": row["review_due"], "next_due": next_due})
    return {"ok": True, "id": article_id, "next_due": next_due}


@app.post("/api/articles/{article_id}/verify")
def api_verify(article_id: str):
    """重新核查（仅应用类），刷新 GitHub 客观字段。"""
    try:
        with db.tx() as conn:
            res = verify.verify_article(conn, article_id)
    except Exception as e:
        log.exception("/api/articles/%s/verify 失败", article_id)
        raise HTTPException(status_code=500,
                            detail={"ok": False, "fail_reason": "internal",
                                    "detail": "%s: %s" % (type(e).__name__, e)})
    if not res.get("ok"):
        return JSONResponse(content=res, status_code=400)
    return res


@app.delete("/api/articles/{article_id}")
def api_delete(article_id: str, confirm: str = Query(default="")):
    """删除（同时清原文与配图），需二次确认参数 confirm=yes。"""
    if confirm != "yes":
        raise HTTPException(status_code=400, detail="need confirm=yes")
    with db.tx() as conn:
        res = ingest.delete_article(conn, article_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail="article not found")
    return res


# ================================================================ 统计

@app.get("/api/stats")
def api_stats():
    with db.conn_ctx() as conn:
        c = db.counts(conn)
        review_pending = conn.execute(
            """SELECT COUNT(*) c FROM articles
               WHERE review_flag = 1 OR (review_due IS NOT NULL AND review_due <= ?)""",
            (db.now_iso(),)).fetchone()["c"]
        chunks = conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        concepts = conn.execute("SELECT COUNT(*) c FROM concepts").fetchone()["c"]
        # ⚠️ 这个数字**恒为 0**，别拿它当失败监控（工作台已不再展示，v2.1）：
        # 它数的是 articles 表里 status='failed' 的行，但**入库失败的文章根本进不了这张表**
        # —— 失败发生在写入之前。所以它从设计上就不是一个有效指标。
        #
        # 真实失败信息在别处，都不在这里：
        #   · 抓取阶段失败 → Skill 侧，带 fail_reason（verify_page / unsupported_file…），
        #     随 Agent 的结果清单报给用户，**不落库**
        #   · 入库接口失败 → 目前只在 HTTP 响应里，也没写 ingest_log
        # 若将来要做"失败可追溯"，正确做法是让上面两处都写进 ingest_log，
        # 而不是继续数这张表。
        failed = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE status = 'failed'").fetchone()["c"]

        days = []
        for i in range(6, -1, -1):
            d = (datetime.now(CST) - timedelta(days=i)).strftime("%Y-%m-%d")
            days.append({"date": d, "count": conn.execute(
                "SELECT COUNT(*) c FROM articles WHERE substr(collected_at,1,10) = ?",
                (d,)).fetchone()["c"]})

        idx = search.index_stats(conn)

    def _du(p):
        total = 0
        if p.exists():
            for f in p.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        return total

    dbsize = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    return {"version": config.VERSION, "counts": c, "review_pending": review_pending,
            "failed": failed, "chunks": chunks, "concepts": concepts,
            "last7": days, "last7_total": sum(d["count"] for d in days),
            "size": {"db": dbsize, "raw": _du(config.RAW_DIR),
                     "media": _du(config.MEDIA_DIR),
                     "total": dbsize + _du(config.RAW_DIR) + _du(config.MEDIA_DIR)},
            "index": idx, "model_ready": _state["model_ready"]}


# ================================================================ 工作台

@app.get("/")
def index():
    if not config.INDEX_HTML.exists():
        return JSONResponse({"status": "ok", "note": "web/index.html 尚未生成"})
    return FileResponse(str(config.INDEX_HTML))
