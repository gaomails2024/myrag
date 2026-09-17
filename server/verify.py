# -*- coding: utf-8 -*-
"""GitHub 核查（PRD §6.3）：只拉客观数据，不做主观结论。

- license / language / stars / open_issues ← GET /repos/{owner}/{repo}
- last_commit ← pushed_at，created_date ← created_at
- last_release ← GET /repos/{owner}/{repo}/releases/latest（404 则空）
- 结论（verdict）由人填或由 Agent 建议，本模块不生成
- 每次核查都写 evidence（来源 URL + 核实时间 + 原始字段）
"""

import re
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db

CST = timezone(timedelta(hours=8))

REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)", re.I)
ANY_REPO_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)", re.I)

RESERVED = {"orgs", "topics", "search", "features", "sponsors", "marketplace",
            "collections", "about", "pricing", "enterprise"}


def parse_repo(url):
    if not url:
        return None
    u = url.strip()
    if not u.startswith("http"):
        u = "https://github.com/" + u.strip("/")
    m = REPO_RE.match(u)
    if not m:
        return None
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    if owner.lower() in RESERVED or repo.lower() in ("", "."):
        return None
    return owner, repo


def _headers():
    h = {"Accept": "application/vnd.github+json", "User-Agent": "MyRag/%s" % config.VERSION}
    if config.GITHUB_TOKEN:
        h["Authorization"] = "Bearer " + config.GITHUB_TOKEN
    return h


def fetch_repo(owner, repo):
    out = {"ok": False, "error": None, "data": {},
           "source_url": "https://github.com/%s/%s" % (owner, repo),
           "api": "%s/repos/%s/%s" % (config.GITHUB_API, owner, repo)}
    try:
        with httpx.Client(timeout=config.GITHUB_TIMEOUT, headers=_headers(),
                          follow_redirects=True) as c:
            r = c.get(out["api"])
            if r.status_code == 404:
                out["error"] = "repo_not_found"
                return out
            if r.status_code in (403, 429):
                out["error"] = "rate_limited"
                return out
            if r.status_code != 200:
                out["error"] = "http_%d" % r.status_code
                return out

            d = r.json()
            lic = d.get("license") or {}
            out["data"] = {
                "full_name": d.get("full_name"),
                "repo_url": d.get("html_url"),
                "license": lic.get("spdx_id") or lic.get("name"),
                "language": d.get("language"),
                "stars": d.get("stargazers_count"),
                "forks": d.get("forks_count"),
                "open_issues": d.get("open_issues_count"),
                "last_commit": d.get("pushed_at"),
                "created_date": d.get("created_at"),
                "archived": d.get("archived"),
                "description": d.get("description"),
                "topics": d.get("topics") or [],
                "homepage": d.get("homepage"),
                "default_branch": d.get("default_branch"),
            }

            lr = c.get("%s/releases/latest" % out["api"])
            if lr.status_code == 200:
                j = lr.json()
                out["data"]["last_release"] = j.get("tag_name")
                out["data"]["last_release_at"] = j.get("published_at")
            else:
                out["data"]["last_release"] = None
                out["data"]["last_release_at"] = None
            out["ok"] = True
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def _repo_from_raw(conn, article_id):
    """回退：原文里第一个 GitHub 链接。"""
    row = conn.execute("SELECT raw_path FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row or not row["raw_path"]:
        return None
    p = config.DATA_DIR / row["raw_path"]
    if not p.exists():
        return None
    m = ANY_REPO_RE.search(p.read_text(encoding="utf-8"))
    return parse_repo(m.group(0)) if m else None


def verify_article(conn, article_id: str) -> dict:
    """重新核查（仅带 repo_card 能力的分类），刷新 app_cards 的客观字段与 evidence。"""
    row = conn.execute(
        """SELECT a.category, c.repo_url
           FROM articles a LEFT JOIN app_cards c ON c.article_id = a.id
           WHERE a.id = ?""", (article_id,)).fetchone()
    if not row:
        return {"ok": False, "fail_reason": "not_found"}
    cat = db.category_map(conn).get(row["category"]) or {}
    if "repo_card" not in (cat.get("features") or []):
        return {"ok": False, "fail_reason": "no_repo_card_feature"}

    parsed = parse_repo(row["repo_url"]) or _repo_from_raw(conn, article_id)
    if not parsed:
        return {"ok": False, "fail_reason": "no_repo_url"}

    owner, repo = parsed
    res = fetch_repo(owner, repo)
    now = datetime.now(CST).isoformat(timespec="seconds")
    evidence = {"source_url": res["source_url"], "api": res["api"],
                "verified_at": now, "error": res["error"],
                "raw": res["data"]}

    if not res["ok"]:
        conn.execute(
            """INSERT INTO app_cards (article_id, repo_url, evidence, verified_at)
               VALUES (?,?,?,?)
               ON CONFLICT(article_id) DO UPDATE SET
                 repo_url = excluded.repo_url,
                 evidence = excluded.evidence,
                 verified_at = excluded.verified_at""",
            (article_id, res["source_url"], db.jdumps(evidence), now))
        return {"ok": False, "fail_reason": res["error"], "evidence": evidence}

    d = res["data"]
    conn.execute(
        """INSERT INTO app_cards
             (article_id, repo_url, license, language, stars, last_commit,
              last_release, created_date, evidence, verified_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(article_id) DO UPDATE SET
             repo_url = excluded.repo_url,
             license = excluded.license,
             language = excluded.language,
             stars = excluded.stars,
             last_commit = excluded.last_commit,
             last_release = excluded.last_release,
             created_date = excluded.created_date,
             evidence = excluded.evidence,
             verified_at = excluded.verified_at""",
        (article_id, d.get("repo_url") or res["source_url"], d.get("license"),
         d.get("language"), d.get("stars"), d.get("last_commit"),
         d.get("last_release"), d.get("created_date"),
         db.jdumps(evidence), now))

    # License / Star 等字段变了，元数据段要重建，否则搜不到新值
    from . import ingest as ingest_mod

    ingest_mod.upsert_meta_chunk(conn, article_id)

    return {"ok": True, "article_id": article_id, "verified_at": now, "data": d,
            "evidence": evidence}
