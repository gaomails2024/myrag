#!/usr/bin/env python3
"""非微信生态网页抓取 —— 普通站点的正文 / 元数据 / 配图（零依赖）。

与 `wx_fetch.py` 的分工（2026-10-19）：
  · `wx_fetch.py`：微信页面结构（`#js_content` / `picture_page_info_list` / `js_name` / `var ct`）
  · 本脚本    ：普通网页结构（`article` / `main` / 最长块、og / JSON-LD、懒加载图）
  由 `route_urls.py` 决定谁上，两个脚本互不猜对方的页面结构。

**落盘与 URL 归一化一律复用 `wx_fetch`**（`save_stage` / `canonical_url` / `http_get`）：
2026-10-07 的教训是 `render_fetch.py` 当初自己另写一套解析，结果走渲染的 9 篇文章
全部 0 图。同一件事有两套实现，任何一边改版都会在另一边留下分叉 —— 不重演。

**输出字段与 `wx_fetch.py` 完全一致**（`kind` 固定 `web`），所以判类 / OCR / 入库
全链路不需要为新脚本改任何一行。

用法：
    python3 web_fetch.py --urls-file /tmp/myrag_urls.txt \
        --stage-root ~/MyRag/data/_stage

失败原因（微信路径共有的略，这里是本脚本特有的）：
    login_required  需要登录才能看 —— 汇报时单列，可用 Browser Skill 登录后重试
    paywall         付费墙 / 会员专享 —— **不做绕过**（不做不合规抓取）
    blocked         反爬 / 人机验证（403/429、Cloudflare 挑战页）—— 不做绕过
    need_render     JS 渲染页 —— 交 `scripts/render_fetch.py`
    parse_failed    抓到页面但定位不到正文
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 解析 / 落盘 / 常量全部复用微信脚本 —— 只复用「基础设施」，不复用页面结构规则。
import wx_fetch as wf

# 正文有效性：文字够多就算正文；文字很少但有图也算（用户 2026-10-19：
# 「非微信不存在图集这个概念，里面有什么就抓什么」）。
MIN_WEB_TEXT = 200
MIN_WEB_TEXT_WITH_IMG = 20

# 拦截页特征。**只在页面本身文字很少时才采信** —— 正常长文的页脚里也有「登录」
# 「订阅」这类词，按关键词裸判会把好文章判死。
LOGIN_HINTS = ("登录后查看", "请先登录", "登录后继续", "请登录后", "登录才能",
               "sign in to continue", "log in to continue", "please log in",
               "login to view", "sign in to view")
PAYWALL_HINTS = ("付费阅读", "订阅后查看", "订阅后阅读", "会员专享", "购买后查看",
                 "付费内容", "subscribe to read", "subscribe to continue",
                 "for subscribers only", "this content is for subscribers")
BLOCK_HINTS = ("access denied", "just a moment", "cf-browser-verification",
               "enable javascript and cookies to continue", "请求过于频繁",
               "人机验证", "安全验证", "cloudflare ray id")

# 失效页（404 / 已删除 / 已下线）。**必须在正文判定之前识别** ——
# 实测 skillhub「Skill Not Found」页：61 字 + 1 张占位图，会被「有图就算内容」的
# 宽松门槛当成文章收进来，入库就是一条永远打不开的空壳。
NOT_FOUND_HINTS = ("页面不存在", "页面未找到", "内容不存在", "内容已删除", "已下线",
                   "找不到该", "找不到了", "未找到该",
                   "page not found", "not found", "does not exist",
                   "no longer available")


# ---------------------------------------------------------------- 小工具

def _visible_text(page) -> str:
    """页面可见文字（剥脚本 / 样式 / 标签）。"""
    s = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", page or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html_mod.unescape(s)).strip()


def _score(frag) -> int:
    """候选块评分：文字 + 图 × 60。

    纯按文字取最长块会把「导航条 + 侧边栏 + 正文」当成正文，按图加权则让
    「文字 0 字 + 21 张图」的图集页有机会赢。60 是个经验值：1 张图 ≈ 60 字。
    """
    return wf._text_len(frag) + 60 * len(re.findall(r"<img\b", frag or "", re.I))


def _jsonld(page):
    """取出页面里所有 JSON-LD 对象（含 @graph 展开）。解析失败就跳过，不抛。"""
    out = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         page or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:                       # noqa: BLE001 - 坏 JSON-LD 是常态
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                continue
            graph = it.get("@graph")
            if isinstance(graph, list):
                out.extend([x for x in graph if isinstance(x, dict)])
            else:
                out.append(it)
    return out


def _meta(page, url):
    """标题 / 站点名 / 作者 / 发布时间。优先级：og → JSON-LD → 常规标签 → 域名。"""
    ld = _jsonld(page)

    def ld_str(keys, sub="name"):
        for it in ld:
            for k in keys:
                v = it.get(k)
                if isinstance(v, list) and v:
                    v = v[0]
                if isinstance(v, dict):
                    v = v.get(sub)
                if isinstance(v, str) and v.strip():
                    return html_mod.unescape(v).strip()
        return None

    def clean(v):
        return re.sub(r"<[^>]+>", "", html_mod.unescape(v)).strip() if v else None

    title = clean(wf.pick([r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']*)',
                           r'<meta\s+name=["\']twitter:title["\']\s+content=["\']([^"\']*)',
                           r"<title[^>]*>(.*?)</title>"], page)) \
        or ld_str(["headline", "name"])

    source = clean(wf.pick([r'<meta\s+property=["\']og:site_name["\']\s+content=["\']([^"\']*)',
                            r'<meta\s+name=["\']application-name["\']\s+content=["\']([^"\']*)'],
                           page)) \
        or ld_str(["publisher"])

    author = clean(wf.pick([r'<meta\s+property=["\']og:article:author["\']\s+content=["\']([^"\']*)',
                            r'<meta\s+name=["\']author["\']\s+content=["\']([^"\']*)'], page)) \
        or ld_str(["author"])

    published = clean(wf.pick([r'<meta\s+property=["\']article:published_time["\']\s+content=["\']([^"\']*)',
                               r'<meta\s+name=["\']pubdate["\']\s+content=["\']([^"\']*)',
                               r'<meta\s+itemprop=["\']datePublished["\']\s+content=["\']([^"\']*)'],
                              page)) \
        or ld_str(["datePublished"])
    if not published:
        m = re.search(r'<time\b[^>]*\bdatetime=["\']([^"\']+)["\']', page or "", re.I)
        if m:
            published = m.group(1)

    if not source:
        # 站点名取不到就落域名：**不编造**，让下游一眼看出这是降级值。
        source = urllib.parse.urlsplit(url).netloc

    return {"title": title, "source": source, "author": author, "published_at": published}


def _strip_ui(frag):
    """剥掉与正文无关的区块（导航 / 页眉 / 页脚 / 侧栏 / 表单）。

    只按 **标签名** 剥，不按 class 猜 —— `.nav`、`.comment` 这类类名在不同站点上
    含义完全相反，按类名删会把正文整段删掉。
    """
    for tag in ("nav", "header", "footer", "aside", "form", "script", "style", "noscript"):
        frag = re.sub(r"<%s\b.*?</%s>" % (tag, tag), " ", frag or "", flags=re.S | re.I)
        frag = re.sub(r"<%s\b[^>]*/?>" % tag, " ", frag, flags=re.I)
    return frag


def _normalize_imgs(frag, base_url):
    """把图片地址统一成**绝对地址**并落到 `data-src`（`html_to_markdown` 只认它和 `src`）。

    处理两件事，都是普通网页的常态、微信页面没有的：
      ① **懒加载属性**：`data-original`（jQuery lazyload）、`data-actualsrc`（知乎）、
         `data-lazy-src`、`data-echo`；
      ② **相对路径**：`src="/user/repo/raw/main/x.png"`（GitHub README 就是这种）。
         不补全的话 `save_stage` 下载会报 `unknown url type` —— 实测 51 张图只成功 7 张。
    """
    def fix(m):
        tag = m.group(0)
        for attr in ("data-src", "data-original", "data-actualsrc",
                     "data-lazy-src", "data-echo", "src"):
            mm = re.search(r'\b%s\s*=\s*["\']([^"\']+)["\']' % attr, tag, re.I)
            if not mm:
                continue
            src = html_mod.unescape(mm.group(1)).strip()
            if not src or src.startswith("data:"):
                continue                       # base64 占位图，跳过
            absu = urllib.parse.urljoin(base_url, src)
            if not absu.lower().startswith(("http://", "https://")):
                continue
            # 先删掉标签里原有的 data-src（可能是相对路径的占位值），
            # 否则会留下两个 data-src，`re.search` 取到第一个又变成坏的。
            tag2 = re.sub(r'\s+data-src\s*=\s*["\'][^"\']*["\']', "", tag, flags=re.I)
            return re.sub(r"\s*/?>\s*$", ' data-src="%s">' % absu, tag2)
        return tag

    return re.sub(r"<img\b[^>]*>", fix, frag or "", flags=re.I)


def _pick_body(page):
    """定位正文块，返回 (HTML, 来源说明) 或 (None, None)。

    取法按可靠性从高到低：
      1) `<article>`       语义标签最可靠（博客、新闻站主流）
      2) `<main>` / `role="main"`
      3) 评分最高的 `<div>` / `<section>`
      4) `<body>` 兜底（剥掉 UI 标签）

    **与 `wx_fetch.extract_generic` 的区别**：那边是「非微信长文尽力而为」，
    有 200 字门槛、短正文直接判死；这里用户要「页面里有什么就抓什么」，
    纯图集页（0 字 + N 图）也必须抓得下来，所以门槛改成「有字或有图」，
    并在 notes 里如实标注正文取自哪一层 —— 兜底抓到的要让人看得见。
    """
    def best_of(pattern, label):
        blocks = [m.group(1) for m in re.finditer(pattern, page or "", re.S | re.I)]
        if not blocks:
            return None, None
        best = max(blocks, key=lambda b: _score(b))
        return (best, label) if _score(best) >= MIN_WEB_TEXT_WITH_IMG else (None, None)

    for pattern, label in ((r"<article\b[^>]*>(.*?)</article>", "<article>"),
                           (r"<main\b[^>]*>(.*?)</main>", "<main>"),
                           (r'<div\b[^>]*role=["\']main["\'][^>]*>(.*?)</div>', 'role="main"')):
        body, how = best_of(pattern, label)
        if body:
            return body, how

    best, best_sc, best_tag = None, 0, None
    for m in re.finditer(r"<(div|section)\b[^>]*>(.*?)</\1>", page or "", re.S | re.I):
        sc = _score(m.group(2))
        if sc > best_sc:
            best, best_sc, best_tag = m.group(2), sc, "<%s>" % m.group(1).lower()
    if best and best_sc >= MIN_WEB_TEXT_WITH_IMG:
        return best, best_tag

    m = re.search(r"<body\b[^>]*>(.*?)</body>", page or "", re.S | re.I)
    if m and _score(m.group(1)) >= MIN_WEB_TEXT_WITH_IMG:
        return m.group(1), "<body>（未定位到正文容器）"
    return None, None


def _not_found(visible, text_len):
    """页面是不是 404 / 已删除 / 已下线。是则返回说明，否则 None。

    只在**正文有效文字很少**时判定：正常文章的正文里出现「找不到」不算数。
    """
    if text_len >= MIN_WEB_TEXT:
        return None
    head = visible[:1500].lower()
    for h in NOT_FOUND_HINTS:
        if h in head:
            return "页面不存在或已删除（命中「%s」）—— 链接本身失效，不是抓取失败" % h
    return None


def _diagnose(page, visible, text_len):
    """抓不到正文时，判断**是哪一种失败** —— 不同原因对应完全不同的下一步。"""
    head = visible[:3000]
    low = head.lower()

    # 拦截页都很短。长页面里的「登录/订阅/验证」多半是页脚或正文用词，不能采信。
    if len(visible) < 5000:
        for h in LOGIN_HINTS:
            if h in head or h.lower() in low:
                return ("login_required",
                        "页面要求登录后才能看（不是链接错了）。汇报时单列这一类："
                        "可用 Browser Skill 打开登录后重试")
        for h in PAYWALL_HINTS:
            if h in head or h.lower() in low:
                return ("paywall",
                        "页面有付费墙 / 会员限制，按约定不做绕过（不做不合规抓取）")
        for h in BLOCK_HINTS:
            if h in head or h.lower() in low:
                return ("blocked",
                        "站点返回了反爬 / 人机验证页，按约定不做绕过。稍后再试或换浏览器打开")

    if wf.looks_like_spa(page):
        return ("need_render",
                "页面是 JS 渲染的（静态 HTML 里只有外壳），交 render_fetch.py 渲染后再取")
    return ("parse_failed",
            "抓到了页面（%dB）但定位不到正文，可能要登录或页面结构特殊"
            % len((page or "").encode("utf-8", "ignore")))


# ---------------------------------------------------------------- 解析

def parse_web(page, url, body_html=None):
    """把页面解析成 wx_fetch 同构的结果 dict（不落盘，便于单测）。

    `body_html`：调用方（渲染器）已用 DOM 精确取好的正文块。传了就用它 ——
    普通站点的静态 HTML 里 `<div>` 嵌套很深，正则定位必然截断；渲染后由浏览器
    给出的块才完整。不传则自己按 `article` / `main` / 最长块 定位。
    """
    meta = _meta(page, url)
    if body_html is not None:
        body, how = body_html, "浏览器渲染后的 DOM"
    else:
        body, how = _pick_body(page)

    md, img_urls, notes = "", [], []
    if body:
        frag = _normalize_imgs(_strip_ui(body), url)
        md, img_urls, notes = wf.html_to_markdown(frag)
        if how:
            notes.append("正文取自 %s" % how)

    text_len = wf._text_len(body or "")
    visible = _visible_text(page)

    # **失效页优先于「有没有内容」**：404 页也带标题和一张占位图，宽松门槛会把
    # 它收成空壳（实测 skillhub 那条「Skill Not Found」61 字 + 1 图）。
    gone = _not_found(visible, text_len)
    if gone:
        return {"ok": False, "kind": "web", "fail_reason": "not_found",
                "detail": gone, "title": meta["title"], "source": meta["source"]}

    ok = text_len >= MIN_WEB_TEXT or (bool(img_urls) and text_len >= MIN_WEB_TEXT_WITH_IMG) \
        or (bool(img_urls) and not text_len)

    if not ok:
        reason, detail = _diagnose(page, visible, text_len)
        return {"ok": False, "kind": "web", "fail_reason": reason, "detail": detail,
                "title": meta["title"], "source": meta["source"]}

    if text_len < MIN_WEB_TEXT:
        notes.append("正文文字少（%d 字），主要内容是 %d 张图" % (text_len, len(img_urls)))

    return {"ok": True, "kind": "web",
            "title": meta["title"], "source": meta["source"],
            "author": meta["author"], "published_at": meta["published_at"],
            "md": md, "img_urls": img_urls, "notes": notes,
            "fail_reason": None, "detail": None}


# ---------------------------------------------------------------- 单条抓取

def fetch_one(url, stage_root, sleep_before):
    if sleep_before:
        time.sleep(sleep_before)
    t0 = time.time()
    out = {"url": url, "url_canon": wf.canonical_url(url), "ok": False,
           "title": None, "source": None, "author": None, "published_at": None,
           "chars": 0, "images": 0, "embedded": [], "token": None, "fail_reason": None,
           "kind": "web"}

    if not re.match(r"^https?://", url or ""):
        out["fail_reason"] = "bad_url"
        out["detail"] = "只接受 http/https 链接"
        return out
    if wf.is_file_url(url):
        out["fail_reason"] = "unsupported_file"
        out["detail"] = "文件直链不适合按网页存：请存它的网页版，或直接下载文件"
        return out

    try:
        # 非微信**不带 Referer**：微信图床才需要，普通站点带了没用还可能被当成盗链。
        _status, page = wf.http_get(url)
    except urllib.error.HTTPError as e:
        # 重定向到登录页 = 登录墙。**不能报 network**（用户会以为是网络问题）：
        # 实测飞书 wiki 未登录时 302 到 accounts.feishu.cn/accounts/page/login。
        loc = (e.headers.get("Location") or "") if getattr(e, "headers", None) else ""
        if e.code in (301, 302, 303, 307, 308) and re.search(
                r"login|signin|sign-in|passport|sso|accounts\.", loc, re.I):
            out["fail_reason"] = "login_required"
            out["detail"] = ("站点把请求重定向到登录页（%s）—— 需要登录后才能看；"
                             "汇报时单列，可用 Browser Skill 登录后重试"
                             % loc.split("?")[0])
            return out
        if e.code in (401, 403):
            out["fail_reason"] = "blocked" if e.code == 403 else "login_required"
            out["detail"] = "HTTP %s：站点拒绝访问%s" % (
                e.code, "（反爬/防盗链）" if e.code == 403 else "（需要登录）")
        elif e.code == 429:
            out["fail_reason"] = "blocked"
            out["detail"] = "HTTP 429：请求被限流，稍后再试"
        else:
            out["fail_reason"] = "network"
            out["detail"] = "HTTP %s" % e.code
        return out
    except Exception as e:                      # noqa: BLE001
        out["fail_reason"] = "network"
        out["detail"] = "%s: %s" % (type(e).__name__, e)
        return out

    r = parse_web(page, url)
    if not r["ok"]:
        out["fail_reason"] = r["fail_reason"]
        out["detail"] = r["detail"]
        return out

    token, md, img_rel = wf.save_stage(
        stage_root, url, out["url_canon"], r["kind"], r["title"], r["source"],
        r["author"], r["published_at"], r["md"], r["img_urls"], r["notes"],
        fetched_by="static-web")

    out.update({"ok": True, "title": r["title"], "source": r["source"],
                "author": r["author"], "published_at": r["published_at"],
                "chars": len(md), "images": len(img_rel), "token": token,
                "embedded": r["notes"],
                "elapsed": round(time.time() - t0, 1)})
    return out


def main():
    ap = argparse.ArgumentParser(description="MyRag 非微信网页抓取（零依赖）")
    ap.add_argument("--urls-file", required=True, help="一行一条链接，# 开头与空行忽略")
    ap.add_argument("--stage-root", default=wf.DEFAULT_STAGE_ROOT)
    ap.add_argument("--sleep", type=float, default=2.0, help="每条之间的间隔秒数")
    args = ap.parse_args()

    urls = []
    with open(args.urls_file, encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if u and not u.startswith("#"):
                urls.append(u)

    results = []
    for i, u in enumerate(urls, 1):
        r = fetch_one(u, args.stage_root, args.sleep if i > 1 else 0)
        r["idx"] = i
        results.append(r)
        if r["ok"]:
            wf.log("  [%d/%d] OK   %s  %d字 %d图"
                   % (i, len(urls), (r["title"] or r["url"])[:40], r["chars"], r["images"]))
        else:
            wf.log("  [%d/%d] FAIL %s  %s" % (i, len(urls), r["fail_reason"], r["url"][:60]))

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
