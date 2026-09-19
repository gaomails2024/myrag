#!/usr/bin/env python3
"""JS 渲染页面抓取 —— 处理 `wx_fetch.py` 报出的 `need_render`。

**为什么单独一个脚本，而不是塞进 wx_fetch.py**：
`wx_fetch.py` 的硬约束是**零依赖**（Skill 侧要能在不装任何包的机器上跑）。
渲染必须要 playwright，两者不能混。所以分工是：
**wx_fetch 只负责识别**（发现「有壳无内容」就报 `need_render`），**渲染在这里做**。

产出的 stage 目录结构与 wx_fetch **完全一致**，所以后续步骤（判类 → OCR → 入库）
一行都不用改 —— 这是复用 `wx_fetch` 里现成的解析与落盘逻辑换来的。

用法：

    .venv/bin/python scripts/render_fetch.py <URL>
    .venv/bin/python scripts/render_fetch.py <URL> --stage-root /path/to/_stage
    .venv/bin/python scripts/render_fetch.py <URL> --wait 4000      # 多等一会儿再取

前置（一次性，约 180MB）：`.venv/bin/python -m playwright install chromium`
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skill" / "scripts"))

try:
    import wx_fetch as wf          # 复用它的解析/落盘/常量，不重复实现
except Exception as e:             # noqa: BLE001
    sys.exit("加载 wx_fetch 失败（它在 skill/scripts/ 下，应与本仓库一起存在）：%s" % e)


def render_html(url, wait_ms, timeout_ms):
    """用真实浏览器打开页面，返回**渲染后**的 HTML。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("缺少 playwright：.venv/bin/pip install playwright\n"
                 "然后：.venv/bin/python -m playwright install chromium")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:                      # noqa: BLE001
            msg = str(e)
            if "Executable doesn't exist" in msg:
                sys.exit("Chromium 未安装或版本不匹配。跑一次：\n"
                         "  .venv/bin/python -m playwright install chromium")
            sys.exit("启动浏览器失败：%s" % msg[:300])
        try:
            page = browser.new_page(user_agent=wf.UA, viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # domcontentloaded 之后正文往往还没挂上来，等网络静默 + 缓冲
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:                       # noqa: BLE001
                pass                                # 有长轮询的站点永远不 idle，不致命
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()


def main():
    ap = argparse.ArgumentParser(description="渲染 JS 页面并抓正文（wx_fetch 的 need_render 后续）")
    ap.add_argument("url")
    ap.add_argument("--stage-root", default=str(ROOT / "data" / "_stage"))
    ap.add_argument("--wait", type=int, default=2500, help="网络静默后再等多少毫秒（默认 2500）")
    ap.add_argument("--timeout", type=int, default=45000)
    args = ap.parse_args()

    url = args.url.strip()
    if not url.startswith("http"):
        sys.exit("只接受 http/https 链接")

    t0 = time.perf_counter()
    html = render_html(url, args.wait, args.timeout)
    elapsed = time.perf_counter() - t0

    title = wf.pick([r'<meta\s+property="og:title"\s+content="([^"]*)"',
                     r"<title[^>]*>(.*?)</title>"], html)
    source = wf.pick([r'<meta\s+property="og:site_name"\s+content="([^"]*)"'], html)
    body = wf.extract_generic(html)
    md, img_urls, notes = wf.html_to_markdown(body or "")

    if len(md) < wf.MIN_BODY_CHARS:
        print("✗ 渲染后仍取不到正文（%d 字）—— 页面可能要求登录，或正文在 iframe 里" % len(md))
        print("  渲染耗时 %.1fs，HTML %d 字节" % (elapsed, len(html)))
        sys.exit(1)

    # ---- 落 stage（结构与 wx_fetch 完全一致，后续步骤不用改）----
    token = hashlib.md5((wf.canonical_url(url) + str(time.time())).encode()).hexdigest()[:8]
    sdir = os.path.join(args.stage_root, token)
    idir = os.path.join(sdir, "img")
    os.makedirs(idir, exist_ok=True)

    slots, dropped = {}, []
    for i, iu in enumerate(img_urls, 1):
        try:
            st, data = wf.http_get(iu, referer=None, binary=True)
            if st != 200 or len(data) < 100:
                continue
            ext = "png"
            if data[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            elif data[:4] == b"GIF8":
                ext = "gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                ext = "webp"
            # L1 过滤：与 wx_fetch 同一套规则（PRD §6.4）
            if ext == "gif":
                dropped.append((i, "动图"))
                continue
            size = wf._img_size(data)
            if size:
                w, h = size
                if min(w, h) < wf.MIN_IMG_SIDE:
                    dropped.append((i, "%dx%d 过小" % (w, h)))
                    continue
                if max(w, h) / max(1, min(w, h)) > wf.MAX_IMG_RATIO:
                    dropped.append((i, "%dx%d 长条" % (w, h)))
                    continue
            name = "%02d.%s" % (i, ext)
            with open(os.path.join(idir, name), "wb") as f:
                f.write(data)
            slots[i] = name
        except Exception:                           # noqa: BLE001
            pass

    def _repl(m):
        k = int(m.group(1)) + 1          # 占位符是 0-based，slots 是 1-based
        return "![](img/%s)" % slots[k] if k in slots else ""

    md = re.sub(r"\x00IMG(\d+)\x00", _repl, md)
    if title:
        md = "# %s\n\n%s" % (title, md)

    payload = {
        "url": url, "url_canon": wf.canonical_url(url), "kind": "web_rendered",
        "title": title, "source": source, "author": None, "published_at": None,
        "content_md": md, "images": ["img/" + slots[k] for k in sorted(slots)],
        "embedded": notes,
    }
    with open(os.path.join(sdir, "payload.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with open(os.path.join(sdir, "content.md"), "w", encoding="utf-8") as f:
        f.write(md)

    print("OK  [web_rendered] %d字 %d图 token=%s  渲染 %.1fs"
          % (len(md), len(slots), token, elapsed))
    if dropped:
        print("    - 已过滤 %d 张无信息配图" % len(dropped))
    print("    stage: %s" % sdir)
    print("    下一步：判类 → OCR（scripts/ocr_images.py --stage %s）→ 入库" % token)


if __name__ == "__main__":
    main()
