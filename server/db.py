# -*- coding: utf-8 -*-
"""SQLite 连接、schema 初始化、通用查询辅助（PRD §5.1）。

约定：
- 每个请求一个连接（FastAPI 会把同步端点丢进线程池，连接不可跨线程复用）
- 每次都装载 sqlite-vec 扩展，否则 chunk_vecs 查不了
- 写操作用 with conn: 显式事务
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import sqlite_vec

from . import config

CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    """当前时刻（+08:00）。"""
    return datetime.now(CST).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@contextmanager
def conn_ctx():
    """只读/短事务用。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx():
    """写事务：异常自动回滚。"""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """建目录 + 执行 schema（幂等）+ 轻量迁移。"""
    config.ensure_dirs()
    sql = config.SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _connect()
    try:
        conn.executescript(sql)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    """给老库补新列。

    为什么必须有：schema.sql 用的是 `CREATE TABLE IF NOT EXISTS` —— 表已经存在时，
    写在建表语句里的新列**不会**被加上。升级版本（或开源用户更新）全靠这里。
    只做加列，绝不做破坏性变更。
    """
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
    except Exception:
        return
    if cols and "display" not in cols:
        conn.execute("ALTER TABLE categories ADD COLUMN display TEXT DEFAULT ''")


# ---------------------------------------------------------------- 通用助手

def rows_to_dicts(rows) -> list:
    return [dict(r) for r in rows]


def jloads(value, default):
    """容忍脏数据的 JSON 解析。"""
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def jdumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def log_ingest(conn, url, article_id, action, detail) -> None:
    conn.execute(
        "INSERT INTO ingest_log (url, article_id, action, detail, at) VALUES (?,?,?,?,?)",
        (url, article_id, action, jdumps(detail) if not isinstance(detail, str) else detail, now_iso()),
    )


def counts(conn) -> dict:
    """按分类配置统计：{<key>: 篇数, ..., 'total': N}。

    以 categories 表为准动态生成键 —— 分类可增删改，这里不能写死。
    存量里若存在已删除分类的文章，也一并计入（不静默丢弃计数）。
    """
    out = {"total": 0}
    for c in get_categories(conn):
        out.setdefault(c["key"], 0)
    for r in conn.execute(
        "SELECT category, COUNT(*) c FROM articles WHERE status <> 'failed' GROUP BY category"
    ):
        out[r["category"]] = out.get(r["category"], 0) + r["c"]
        out["total"] += r["c"]
    return out


# ---------------------------------------------------------------- 分类配置（PRD §5.5）

def seed_categories(conn) -> int:
    """首次启动播种出厂默认分类；已存在配置则一字不动（绝不覆盖用户改过的）。"""
    n = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
    if n:
        return 0
    now = now_iso()
    for c in config.DEFAULT_CATEGORIES:
        conn.execute(
            """INSERT INTO categories
               (key, name, prefix, criteria, retrieval, features, is_default, sort_order,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (c["key"], c["name"], c["prefix"], c.get("criteria", ""),
             c.get("retrieval", "rag"), jdumps(c.get("features") or []),
             int(c.get("is_default", 0)), int(c.get("sort_order", 0)), now, now))
    return len(config.DEFAULT_CATEGORIES)


def get_categories(conn) -> list:
    """全部分类，按 sort_order 排；features 已解析成 list。"""
    rows = conn.execute("SELECT * FROM categories ORDER BY sort_order, key").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["features"] = jloads(d.get("features"), [])
        out.append(d)
    return out


def category_map(conn) -> dict:
    """{key: 分类 dict}。"""
    return {c["key"]: c for c in get_categories(conn)}


def default_category(conn):
    """判不准时落到哪一类：is_default=1 优先，否则取第一个。表空则返回 None。"""
    cats = get_categories(conn)
    if not cats:
        return None
    for c in cats:
        if c["is_default"]:
            return c
    return cats[0]


def find_by_stage_token(conn, stage_token: str):
    """幂等：同一个 stage_token 是否已经 ingest 过。"""
    if not stage_token:
        return None
    row = conn.execute(
        """SELECT article_id, url FROM ingest_log
           WHERE action = 'ingest' AND json_extract(detail, '$.stage_token') = ?
           ORDER BY id LIMIT 1""",
        (stage_token,),
    ).fetchone()
    return row


def find_by_canon(conn, url_canon: str):
    if not url_canon:
        return None
    return conn.execute(
        "SELECT id FROM articles WHERE url_canon = ?", (url_canon,)
    ).fetchone()


def allocate_id(conn, prefix: str, date_str: str) -> str:
    """编号分配（PRD §5.3），必须在写 articles 的同一事务内调用。

    n = 该前缀该日已有条数；候选 = n+1 补零两位；冲突则 n++ 重试，最多 50 次。
    仍冲突 → 抛异常，不静默改写编号。
    """
    like = "%s-%s-%%" % (prefix, date_str)
    n = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE id LIKE ?", (like,)
    ).fetchone()["c"]
    for _ in range(config.ID_MAX_RETRY):
        n += 1
        cand = "%s-%s-%02d" % (prefix, date_str, n)
        exists = conn.execute("SELECT 1 FROM articles WHERE id = ?", (cand,)).fetchone()
        if not exists:
            return cand
    raise RuntimeError("编号分配失败：%s-%s 下 50 次重试均冲突" % (prefix, date_str))


# ---------------------------------------------------------------- 入库规则（PRD §5.6）

def seed_rules(conn) -> int:
    """播种出厂规则；已存在的一律不动（用户改过的不覆盖）。"""
    now = now_iso()
    n = 0
    for d in config.RULE_DEFS:
        if not conn.execute("SELECT 1 FROM rules WHERE key = ?", (d["key"],)).fetchone():
            conn.execute("INSERT INTO rules (key, value, updated_at) VALUES (?,?,?)",
                         (d["key"], d["default"], now))
            n += 1
    return n


def get_rules(conn) -> dict:
    """{key: value}。库里缺的键用出厂默认补上（只读，不回写）。"""
    out = {d["key"]: d["default"] for d in config.RULE_DEFS}
    for r in conn.execute("SELECT key, value FROM rules"):
        out[r["key"]] = r["value"]
    return out
