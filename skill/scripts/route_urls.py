#!/usr/bin/env python3
"""链接路由 —— 判断一条链接该交给哪个抓取脚本（零依赖）。

为什么单独一个脚本（2026-10-19）：
  · `wx_fetch.py` 只懂微信页面结构（`#js_content` / `picture_page_info_list` / `js_name`）；
  · `web_fetch.py` 只懂普通网页结构（`article` / `main` / `og` / JSON-LD）；
  · 两边都不该去猜「这条链接是不是我的」—— 猜错的代价是拿错误的结构去解析：
    微信页面用通用定位会 0 图，普通网页套 `js_content` 规则会 0 字。

  路由集中在这里，以后要加新生态（知乎 / 小红书…）只改这一个文件，
  两个抓取脚本一行都不用动。

用法：
    python3 route_urls.py --urls-file /tmp/myrag_urls.txt

输出 JSON 数组，每链接一条：
    [{"idx": 1, "url": "…", "route": "wechat", "script": "wx_fetch.py", "reason": "公众号文章"}]

route 取值与去向：
    wechat   → wx_fetch.py          （公众号文章 / 图片合辑，原有流程，一行不改）
    video    → 视频号转写链路        （**不抓**，见 SKILL.md 第 1.5 步）
    file     → 拒收                  （pdf/zip/mp4…按网页存没有意义）
    web      → web_fetch.py          （普通网页，静态拿不到再交 render_fetch.py）
    bad_url  → 拒收                  （不是 http/https）

路由口径（用户 2026-10-19 拍板）：
  · 微信生态 = `weixin.qq.com` 域名族（公众号 `mp.weixin.qq.com`、视频号
    `weixin.qq.com/sph`、`channels.weixin.qq.com`）。
  · **白名单之外一律按普通网页处理** —— 非微信是开放集合、不可能穷举，
    默认走 web 才不会漏；猜错成微信的代价（0 字 0 图）比放过一条大得多。
"""

import argparse
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用 wx_fetch 的判定与常量 —— 路由与抓取对「什么是文件直链」必须口径一致，
# 各写一份迟早会一边改了另一边没改。
import wx_fetch as wf


def route(url):
    """返回 (route, script, reason)。script 为 None 表示这条不走抓取脚本。"""
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return "bad_url", None, "不是 http/https 链接"

    parts = urllib.parse.urlsplit(u)
    host = (parts.netloc or "").lower().split(":")[0]
    path = parts.path or "/"

    if wf.is_file_url(u):
        ext = os.path.splitext(path)[1] or "文件"
        return "file", None, "文件直链（%s）：改存网页版，或直接下载文件" % ext

    if host == "mp.weixin.qq.com":
        return "wechat", "wx_fetch.py", "公众号文章 / 图片合辑"

    if host == "weixin.qq.com" or host.endswith(".weixin.qq.com"):
        if path.startswith("/sph") or host.startswith("channels."):
            return "video", None, "视频号：没有可抓的正文，走转写链路（SKILL.md 第 1.5 步）"
        # 微信生态里的其他页面（少见）：仍交 wx_fetch，它自己判定是文章还是非文章页。
        return "wechat", "wx_fetch.py", "微信生态页面（由 wx_fetch 判定页面类型）"

    return "web", "web_fetch.py", "普通网页"


def main():
    ap = argparse.ArgumentParser(description="链接路由：微信生态 / 视频号 / 文件直链 / 普通网页")
    ap.add_argument("--urls-file", required=True, help="一行一条链接，# 开头与空行忽略")
    args = ap.parse_args()

    rows, idx = [], 0
    with open(args.urls_file, encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if not u or u.startswith("#"):
                continue
            idx += 1
            r, s, why = route(u)
            rows.append({"idx": idx, "url": u, "route": r,
                         "script": s, "reason": why})
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
