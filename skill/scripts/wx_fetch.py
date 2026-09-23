#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MyRag 抓取脚本 — 微信文章正文 / 元数据 / 配图。

归属：Skill 包（仓库 skill/ 目录；安装后位于 Agent 客户端的 skills 目录，
      如 ~/.workbuddy/skills/MyRag/scripts/）。不属于系统后端。
依赖：Python 标准库，零第三方包。系统自带 python3 即可运行。

产出（每个成功的链接一个目录）：
  <stage-root>/<token>/
    ├── payload.json   元数据 + content_md + 配图相对路径  → 给后端 ingest 读
    ├── content.md     正文 Markdown                        → 给 Agent 判类读
    └── img/NN.png     配图原件（原格式）

stdout：一行 JSON 数组简报（不含正文，避免撑爆 Agent 上下文）
stderr：进度日志

--canon-only：只做 URL 规范化（纯字符串计算，不联网、不抓取），输出
[{url, url_canon}]。用于抓取**之前**的查重——canon 必须在抓取前就能算出来，
否则"先查重、避免白抓"做不到。


内嵌媒体：文章内的视频 / 视频号卡片**不下载**，但会在正文里留一行
`> [视频] <链接>` 并记入 payload 的 `embedded` 字段——不许静默丢弃。
"""

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = "https://mp.weixin.qq.com/"
CST = timezone(timedelta(hours=8))
def _default_stage_root() -> str:
    """落地区的兜底路径。

    正常调用时 SKILL.md 会显式传 `--stage-root`（系统根每个人不一样，不能写死）。
    这里只在没传时兜底，顺序：MYRAG_STAGE_DIR → MYRAG_HOME/data/_stage → ~/MyRag/data/_stage。
    """
    env = os.environ.get("MYRAG_STAGE_DIR")
    if env:
        return os.path.expanduser(env)
    home = os.environ.get("MYRAG_HOME")
    base = os.path.expanduser(home) if home else os.path.expanduser("~/MyRag")
    return os.path.join(base, "data", "_stage")


DEFAULT_STAGE_ROOT = _default_stage_root()
TIMEOUT = 30
MIN_HTML_BYTES = 20 * 1024
MIN_BODY_CHARS = 200

KEEP_PARAMS = ("__biz", "mid", "idx", "sn")
DROP_PARAMS = ("mpshare", "scene", "srcid", "chksm", "sharer_shareinfo",
               "sharer_shareinfo_first", "exportkey", "pass_ticket", "ascene",
               "devicetype", "version", "lang", "nettype", "abtest_cookie",
               "fontScale", "clicktime", "enterid", "key", "uin")

# 非微信链接归一化时剔除的追踪参数 —— 同一页面因分享来源不同会被算成不同条目，
# 剔掉它们才能正确查重（PRD §5.2）。
TRACK_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                "spm", "from", "share_token", "share_source", "share_medium",
                "ref", "fbclid", "gclid", "yclid", "scene", "srcid")

# 文件直链：不适合按网页存（存下来只是一串二进制），明确拒收并说明原因。
FILE_EXTS = (".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".dmg", ".pkg", ".exe",
             ".apk", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".epub",
             ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".csv")

# ---- 配图 L1 过滤（PRD §6.4）----
# 微信文章里混着大量「无信息载体」：小图标、表情、分割线、引导关注图、动图。
# 实测它们占全库体积的 96%，而没人会看。**在下图这一环就剔掉最省事** ——
# 图已经进内存了，判断尺寸后再决定落不落盘，零额外成本（也不占磁盘）。
MIN_IMG_SIDE = 100     # 宽或高小于此值 → 图标 / 表情
MAX_IMG_RATIO = 8      # 长宽比超过此值 → 分割线 / 装饰长条

# 真·验证页的特征词。
# **不能只看 HTML 体积**：图片合辑等非文章页也可能很小，把它们当成「验证页」
# 会让用户往错误方向排查（去重新扫码/换网络，其实页面类型根本不对）。
VERIFY_HINTS = ("环境异常", "去验证", "js_verify", "verify_page", "访问过于频繁",
                "请输入验证码", "操作过于频繁", "该内容已被发布者删除",
                "此内容因违规无法查看",
                # 微信另一种报错页：可见区只有「未知错误，请稍后再试」，
                # 内嵌 JS 里把 title 写成「失效的验证页面」「你暂无权限查看此页面内容」。
                # 不认它 → 落 not_article → 引导用户重新复制链接（无效动作）。2026-09-22 实测。
                "未知错误，请稍后再试")

# **强特征**：这些串只可能出现在微信的验证/拦截页上，不会出现在正常页面的业务脚本里，
# 所以**可以全文匹配**（不剥 <script>）。与上面 VERIFY_HINTS 的区别见 looks_like_verify()。
#
# 为什么要单独有这一组（2026-09-23 实测 5/5 失败）：微信有一种**没有文案的验证页** ——
# body 只有空的 weui-msg、<title> 为空，上面那些词**一个都不出现**，
# 铁证只有两处，而且**都在 <script> 里面**：
#     PAGE_MID='mmbizwap:secitptpage/verify.html'
#     https://captcha.gtimg.com/TCaptcha.js
# 于是「先剥 script 再找词」对它天然失效 —— 上一次能认出来纯属巧合
# （同一串还出现在 <link href="…/secitptpage/verify80c8.css">，link 不在剥离范围内）。
# 认不出的后果不只是漏报：它会落 not_article，把用户引向「重新复制链接」这种无效动作，
# 而正确的处置是「等一会儿或换网络」。
VERIFY_STRONG_HINTS = ("secitptpage/verify", "TCaptcha.js")


# ---------------------------------------------------------------- 工具

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def http_get(url, referer=None, binary=False):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read()
        return resp.status, (data if binary else data.decode("utf-8", "ignore"))


def canonical_url(url):
    """去掉 #fragment 与追踪参数，只保留识别参数。

    **分两种站点**：
      · 微信公众号 —— 按 __biz/mid/idx/sn 归一（同一篇文章的分享链接千奇百怪，
        只有这四个参数能定位到唯一一篇）；
      · 其他站点   —— 保留原路径，只剔除常见追踪参数、去掉 fragment。

    早期版本对任何 URL 都兜底成 `mp.weixin.qq.com/s`，那会把非微信链接
    **改写成微信域名**，既查不了重也抓不到东西。
    """
    p = urllib.parse.urlsplit((url or "").strip())
    host = (p.netloc or "").lower()

    if host.endswith("mp.weixin.qq.com"):
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
        kept = {k: v for k, v in q if k in KEEP_PARAMS}
        if kept:
            query = urllib.parse.urlencode([(k, kept[k]) for k in KEEP_PARAMS if k in kept])
            return urllib.parse.urlunsplit((p.scheme or "https", p.netloc, "/s", query, ""))
        return urllib.parse.urlunsplit((p.scheme or "https", p.netloc, p.path, "", ""))

    if not host:
        return (url or "").strip()

    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False)
         if k.lower() not in TRACK_PARAMS]
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme or "https", p.netloc, path,
                                    urllib.parse.urlencode(q), ""))


def pick(patterns, text, group=1):
    for pat in patterns:
        m = re.search(pat, text, re.S)
        if m:
            val = html_mod.unescape(m.group(group)).strip()
            if val:
                return val
    return None


# ---------------------------------------------------------------- 正文定位

def extract_container(page):
    """定位 #js_content 容器内部 HTML，用 div 配对计数取完整内容。"""
    m = re.search(r'id="js_content"', page)
    if not m:
        return None
    open_end = page.find(">", m.end())
    if open_end == -1:
        return None
    i = open_end + 1
    depth = 1
    tag = re.compile(r"<(/?)div\b[^>]*?(/?)>", re.I)
    while depth > 0:
        nxt = tag.search(page, i)
        if not nxt:
            return page[open_end + 1:]
        if nxt.group(2) == "/":
            pass
        elif nxt.group(1) == "/":
            depth -= 1
        else:
            depth += 1
        if depth == 0:
            return page[open_end + 1:nxt.start()]
        i = nxt.end()
    return page[open_end + 1:]


# ---------------------------------------------------------------- 类型判定与小工具

def is_file_url(url) -> bool:
    """文件直链（.pdf/.zip/.mp4…）—— 不适合按网页存，要明确拒收而不是硬抓。"""
    path = urllib.parse.urlsplit(url or "").path.lower()
    return path.endswith(FILE_EXTS)


def looks_like_verify(page) -> bool:
    """是否真是「环境异常 / 需要验证」页。

    两个坑都踩过，所以判定要小心：
      · **不能只看 HTML 体积** —— 图片合辑之类的非文章页同样很小；
      · **不能裸匹配全文** —— 站点自己的错误处理脚本里常含「环境异常」这类词。
        实测 skillhub.cn 的页面里有 `window.alert('检测到浏览器环境异常…')`，
        裸匹配会把正常页面误判成验证页，让用户往反爬方向白折腾。

    但「一律剥掉 script 再找词」也不是万能的 —— 微信那种**无文案验证页**的铁证
    （`secitptpage/verify`）恰恰写在 script 里（详见 VERIFY_STRONG_HINTS 的说明）。

    所以分两级：
      ① **强特征：全文匹配** —— 这些串只可能出现在验证页上，剥掉反而会漏；
      ② **弱特征：只在可见内容里找** —— 剥掉 script/style，避免站点业务脚本里的
         「环境异常」把正常页面误判成验证页。
    """
    s = page or ""
    if any(h in s for h in VERIFY_STRONG_HINTS):          # ① 全文
        return True
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    return any(h in visible for h in VERIFY_HINTS)        # ② 可见区


def looks_like_spa(page) -> bool:
    """是否是需要 JS 才能出内容的单页应用（SPA）。

    特征：HTML 有壳、带 `<script>`，但**可见文字极少**。

    识别它的意义在于给准确的下一步：这类页面**既不是反爬、也不是页面坏了**，
    而是内容由 JS 异步渲染，静态抓取必然拿不到。报「解析失败」会让人以为是
    结构特殊或需要登录；报「验证页」更会把人带偏。所以单列一个 `need_render`。
    """
    if not page or "<script" not in page.lower():
        return False
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    txt = html_mod.unescape(s)
    return len(re.sub(r"\s+", "", txt)) < 200


def _img_size(data):
    """从图片字节流读出 (宽, 高)。**零依赖** —— 只解析头部，不解码像素。

    本脚本的硬约束是「零第三方依赖」，所以不能引入 PIL 之类。
    但只判断尺寸的话，读头部几字节就够：
      · PNG  —— IHDR 块固定在第 16–24 字节
      · GIF  —— 逻辑屏幕描述符在第 6–10 字节
      · JPEG —— 需要扫段找 SOF（尺寸在 SOF 段里）
      · WEBP —— VP8X（无损/动画）与 VP8（有损）字段位置不同

    读不出来返回 None —— **调用方遇 None 应当保留该图**，宁可多留也不要误删。
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if data[:4] == b"GIF8":
            w, h = struct.unpack("<HH", data[6:10])
            return w, h
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                mk = data[i + 1]
                if mk in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):     # SOF 段
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7:        # 无长度字段
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            tag = data[12:16]
            if tag == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if tag == b"VP8 ":
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h
    except Exception:
        return None
    return None


def _text_len(html_frag) -> int:
    """粗估一段 HTML 的可见文本长度（去掉脚本、样式、标签与空白）。"""
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", html_frag or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return len(re.sub(r"\s+", "", html_mod.unescape(s)))


# ---------------------------------------------------------------- 通用网页正文

def extract_generic(page):
    """非微信站点的正文定位（尽力而为，非精确）。

    顺序：<article> → <main> → role="main" → 纯文本最长的 <div>。
    零依赖做不到 readability 那么准，但覆盖面够用；**取不到就返回 None 交给
    调用方判失败，绝不把导航/页脚硬凑成正文**。
    """
    for pat in (r"<article\b[^>]*>(.*?)</article>",
                r"<main\b[^>]*>(.*?)</main>",
                r'<div\b[^>]*role="main"[^>]*>(.*?)</div>'):
        m = re.search(pat, page, re.S | re.I)
        if m and _text_len(m.group(1)) >= MIN_BODY_CHARS:
            return m.group(1)

    best, best_len = None, 0
    for m in re.finditer(r"<div\b[^>]*>(.*?)</div>", page, re.S | re.I):
        n = _text_len(m.group(1))
        if n > best_len:
            best, best_len = m.group(1), n
    return best if best_len >= MIN_BODY_CHARS else None


# ---------------------------------------------------------------- 微信图片合辑

def _match_block(text, start):
    """从 start 处的 `[` 或 `{` 开始，按配对找出该结构的完整文本。"""
    open_ch = text[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth, i = 0, start
    while i < len(text):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return text[start:]


def _extract_picture_list(page):
    """取微信图片合辑的图片（按顺序）。

    图片**不在 HTML 标签里**，而是页面内的一段 JS 对象：
        picture_page_info_list: [
            { cdn_url: '原图', watermark_info: { cdn_url: '水印版' }, ... },
            ...
        ]
    每个元素有**两个** cdn_url，第二个是加水印的版本，所以按元素配对切块、
    每块只取第一个，否则图片会翻倍。
    """
    # `[:=]` 两种都收：真实页面是对象属性 `picture_page_info_list: [...]`，
    # 但**上面第一关是宽松的子串匹配**（`in page`），万一微信改成 `var x = [...]`
    # 就会「第一关过、第二关不过」—— 拿到标题却图集为空 → album_ok=False →
    # 又落 not_article，而且表现和合辑识别完全失效一模一样，很难查。
    m = re.search(r"picture_page_info_list\s*[:=]\s*\[", page)
    if not m:
        return []
    block = _match_block(page, m.end() - 1)

    urls, cursor = [], 0
    for em in re.finditer(r"\{", block):
        if em.start() < cursor:          # 落在上一个元素内部（如 watermark_info）
            continue
        elem = _match_block(block, em.start())
        cursor = em.start() + len(elem)
        # 键名两侧的引号可有可无：真实页面是 JS 对象字面量（裸键 `cdn_url: '…'`），
        # 但若微信哪天改成 JSON 风格（`"cdn_url": "…"`），只认裸键就会**静默失效** ——
        # 表现和「合辑识别没做」一模一样（拿到标题、图集为空 → album_ok=False → not_article）。
        u = re.search(r"[\"']?cdn_url[\"']?\s*:\s*['\"]([^'\"]+)['\"]", elem)
        if u:
            u = html_mod.unescape(u.group(1)).strip()
            if u.startswith("http"):
                urls.append(u)
    return urls


def _clean_wx_desc(s):
    """清洗图片合辑的描述文本。

    实测页面里的 og:description 是**被转义过的 JS 片段**，例如：
        \\x0a\\x26lt;a class=\\x26quot;wx_topic_link\\x26quot; ...\\x26gt;#融资成功\\x26lt;/a\\x26gt;
    即：`\\x0a` 是换行、`\\x26lt;` 还原一层是 `&lt;`、再还原才是 `<`。
    这里逐层还原后去掉标签，只留纯文本（话题标签的文字本身保留）。
    """
    if not s:
        return s
    # 1) JS 风格的 \xHH 转义
    s = re.sub(r"\\x([0-9a-fA-F]{2})",
               lambda m: chr(int(m.group(1), 16)), s)
    # 2) HTML 实体（页面里可能有 1–2 层）
    for _ in range(2):
        s = html_mod.unescape(s)
    # 3) 去标签，保留话题文字
    s = re.sub(r"<[^>]+>", " ", s)
    # 4) 收敛空白
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def extract_image_album(page, url):
    """微信图片合辑（如分享链接带 `t=pages/image_detail`，或页面含 picture_page_info_list）。

    这类页面**没有 `js_content` 容器**，内容主体是一组图（典型是长图/多页图），
    但有完整文案放在 og:description 里。早期实现按「正文 0 字」直接拒收、
    还误报成验证页 —— 其实页面是好的，只是模板不同。

    返回 None 表示不像图片合辑，交给调用方按别的类型处理。
    """
    if "image_detail" not in (url or "") and "picture_page_info_list" not in page:
        return None

    title = pick([r'<meta\s+property="og:title"\s+content="([^"]*)"',
                  r'var\s+msg_title\s*=\s*["\']([^"\']*)'], page)
    desc = _clean_wx_desc(pick([r'<meta\s+property="og:description"\s+content="([^"]*)"',
                                r'<meta\s+name="description"\s+content="([^"]*)"'], page))

    imgs = _extract_picture_list(page)
    if not imgs:                     # 兜底：别的合辑模板可能把图放在标签属性里
        for pat in (r'data-src="([^"]+)"', r'<img[^>]+src="([^"]+)"'):
            for u in re.findall(pat, page):
                u = html_mod.unescape(u).strip()
                if not u.startswith("http") or u in imgs:
                    continue
                if re.search(r"(mmbiz\.qpic\.cn|mmbiz\.qlogo\.cn)", u):
                    imgs.append(u)
    if not title and not imgs:
        return None
    return {"title": title, "desc": desc, "images": imgs}


# ---------------------------------------------------------------- HTML → Markdown

def html_to_markdown(body):
    """把微信正文 HTML 转成 Markdown。返回 (markdown, image_urls, notes)。"""
    imgs = []
    notes = []

    def _img(m):
        attrs = m.group(0)
        src = None
        for p in ('data-src="([^"]+)"', 'src="([^"]+)"'):
            mm = re.search(p, attrs)
            if mm and not mm.group(1).startswith("data:"):
                src = html_mod.unescape(mm.group(1))
                break
        if not src:
            return ""
        imgs.append(src)
        return "\n\n\x00IMG%d\x00\n\n" % (len(imgs) - 1)

    def _iframe(m):
        """内嵌视频等：不下载，但必须留痕，不许静默丢弃。"""
        attrs = m.group(0)
        src = None
        for p in ('data-src="([^"]+)"', 'src="([^"]+)"'):
            mm = re.search(p, attrs)
            if mm:
                src = html_mod.unescape(mm.group(1))
                break
        kind = "视频" if "video" in attrs.lower() else "内嵌内容"
        notes.append(kind)
        if src:
            return "\n\n> [%s] %s\n\n" % (kind, src)
        return "\n\n> [%s（无外链）]\n\n" % kind

    h = re.sub(r"<img\b[^>]*>", _img, body, flags=re.I)
    h = re.sub(r"<iframe\b[^>]*>.*?</iframe>", _iframe, h, flags=re.S | re.I)
    h = re.sub(r"<iframe\b[^>]*/?>", _iframe, h, flags=re.I)
    h = re.sub(r"<script\b.*?</script>", "", h, flags=re.S | re.I)
    h = re.sub(r"<style\b.*?</style>", "", h, flags=re.S | re.I)
    h = re.sub(r"<svg\b.*?</svg>", "", h, flags=re.S | re.I)
    # 微信生态卡片：只记真媒体卡片（视频号 / 小程序等），排版辅助标签忽略
    NOISE_CARDS = {"style-type", "common-profile", "common_style_type",
                   "common_profile", "voice", "mpvoice"}
    for cm in re.finditer(r"<mp-([\w-]+)([^>]*)>", h, re.I):
        name, attrs = cm.group(1).lower(), cm.group(2)
        if name in NOISE_CARDS:
            continue
        if re.search(r"data-(vid|appid|src|miniprogram|path|type)\b", attrs, re.I):
            notes.append("卡片:" + name)
    h = re.sub(r"<mp-[\w-]+\b[^>]*>", "", h, flags=re.I)
    h = re.sub(r"</mp-[\w-]+>", "", h, flags=re.I)

    # 代码块
    def _pre(m):
        code = re.sub(r"<[^>]+>", "", m.group(1))
        code = html_mod.unescape(code).strip("\n")
        fence = "```"
        while fence in code:
            fence += "`"
        return "\n\n%s\n%s\n%s\n\n" % (fence, code, fence)

    h = re.sub(r"<pre\b[^>]*>(.*?)</pre>", _pre, h, flags=re.S | re.I)

    # 表格
    def _table(m):
        rows = []
        for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", m.group(1), re.S | re.I):
            cells = [re.sub(r"\s+", " ",
                            html_mod.unescape(re.sub(r"<[^>]+>", "", c))).strip()
                     for c in re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]
            if cells:
                rows.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        if not rows:
            return ""
        sep = "| " + " | ".join("---" for _ in rows[0].strip("| ").split("|")) + " |"
        return "\n\n" + rows[0] + "\n" + sep + "\n" + "\n".join(rows[1:]) + "\n\n"

    h = re.sub(r"<table\b[^>]*>(.*?)</table>", _table, h, flags=re.S | re.I)

    # 标题
    for n in range(1, 7):
        h = re.sub(r"<h%d\b[^>]*>(.*?)</h%d>" % (n, n),
                   lambda m, n=n: "\n\n" + "#" * n + " " + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "\n\n",
                   h, flags=re.S | re.I)

    # 列表 / 引用 / 换行 / 段落
    h = re.sub(r"<li\b[^>]*>", "\n- ", h, flags=re.I)
    h = re.sub(r"<blockquote\b[^>]*>", "\n\n> ", h, flags=re.I)
    h = re.sub(r"</blockquote>", "\n\n", h, flags=re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"<p\b[^>]*>", "\n\n", h, flags=re.I)
    h = re.sub(r"</p>", "\n\n", h, flags=re.I)
    h = re.sub(r"</?(section|div|tr|ul|ol|figure|figcaption|tbody|thead)\b[^>]*>", "\n", h, flags=re.I)

    # 行内
    h = re.sub(r"<(strong|b)\b[^>]*>(.*?)</\1>", lambda m: "**" + re.sub(r"<[^>]+>", "", m.group(2)).strip() + "**", h, flags=re.S | re.I)
    h = re.sub(r"<(em|i)\b[^>]*>(.*?)</\1>", lambda m: "*" + re.sub(r"<[^>]+>", "", m.group(2)).strip() + "*", h, flags=re.S | re.I)
    h = re.sub(r"<code\b[^>]*>(.*?)</code>", lambda m: "`" + html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() + "`", h, flags=re.S | re.I)
    h = re.sub(r"<a\b[^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>",
               lambda m: "[%s](%s)" % (re.sub(r"<[^>]+>", "", m.group(2)).strip(), html_mod.unescape(m.group(1))),
               h, flags=re.S | re.I)

    h = re.sub(r"<[^>]+>", "", h)
    h = html_mod.unescape(h)
    h = re.sub(r"[ \t]+\n", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    h = re.sub(r"[ \t]{2,}", " ", h)
    return h.strip(), imgs, notes


# ---------------------------------------------------------------- 单条抓取

def fetch_one(url, stage_root, sleep_before):
    if sleep_before:
        time.sleep(sleep_before)
    t0 = time.time()
    out = {"url": url, "url_canon": canonical_url(url), "ok": False,
           "title": None, "source": None, "author": None, "published_at": None,
           "chars": 0, "images": 0, "embedded": [], "token": None, "fail_reason": None,
           "kind": None}

    # ---- 先判定这条链接该不该抓、按什么抓 ----
    if not re.match(r"^https?://", url or ""):
        out["fail_reason"] = "bad_url"
        out["detail"] = "只接受 http/https 链接"
        return out
    if is_file_url(url):
        out["fail_reason"] = "unsupported_file"
        out["detail"] = ("文件直链（%s）不适合按网页存：请存它的网页版，或直接下载文件"
                         % (os.path.splitext(urllib.parse.urlsplit(url).path)[1] or "文件"))
        return out

    host = urllib.parse.urlsplit(url).netloc.lower()
    is_wx = host.endswith("mp.weixin.qq.com")
    out["kind"] = "wechat" if is_wx else "web"

    try:
        status, page = http_get(url, referer=(REFERER if is_wx else None))
    except urllib.error.HTTPError as e:
        out["fail_reason"] = "network"
        out["detail"] = "HTTP %s" % e.code
        return out
    except Exception as e:
        out["fail_reason"] = "network"
        out["detail"] = "%s: %s" % (type(e).__name__, e)
        return out

    body = None
    album = None
    title = None
    if is_wx:
        # **先试合辑，再提正文** —— 顺序不能反。
        #
        # 原来的写法是「body 为 None 才试合辑」，但 `extract_container` 的返回约定是
        # 「没有 #js_content → None；有但内容为空 → ""」，而**合辑页里通常也有 #js_content**
        # （里面是空的），于是 body 是 "" 而非 None → `body is None` 永不成立 →
        # 合辑分支永远进不去 → 正文为空 → 落 not_article（2026-09-23 实测 5 条全中）。
        #
        # `extract_image_album` 自带判据（URL 含 image_detail 或页面含
        # picture_page_info_list），不是合辑就返回 None，所以**它可以直接先跑**，
        # 既不需要也不该拿正文提取的结果当门禁。
        album = extract_image_album(page, url)
        if album is None:
            body = extract_container(page)
    else:
        body = extract_generic(page)

    if album is not None:
        title = album["title"]
        # 标题不在这里加 —— 下面 `if title:` 会统一加一次，否则会写重
        head = ""
        if album["desc"]:
            head += album["desc"] + "\n\n"
        head += ("> 微信图片合辑 · 共 %d 张图（这类分享页没有正文，内容主体是图片）\n\n"
                 % len(album["images"]))
        md = head + "".join("\x00IMG%d\x00\n\n" % i for i in range(len(album["images"])))
        img_urls, notes = album["images"], ["图片合辑"]
        out["kind"] = "wx_album"
    else:
        md, img_urls, notes = html_to_markdown(body or "")
    body_len = len(md)

    # 图片合辑的 md 只有「文案 + 图片占位」，长度天然可能低于门槛，
    # 但只要图抓到了就是有效条目，不该被字数门槛卡成 not_article（2026-09-22 实测：
    # 一条 9 图的合辑，md≈120 字 → 落 not_article，引导用户重新复制链接，是无效动作）。
    album_ok = album is not None and bool(album["images"])
    if body_len < MIN_BODY_CHARS and not album_ok:
        if looks_like_verify(page):
            out["fail_reason"] = "verify_page"
            out["detail"] = ("%s返回了验证/异常页，稍后重试或换网络（不是链接错了）"
                             % ("微信" if is_wx else "站点"))
        elif not is_wx and looks_like_spa(page):
            # 非微信 + 有壳无内容 = JS 渲染页面。**不是反爬、不是页面坏了**，
            # 静态抓取天生拿不到，得换渲染方式。
            out["fail_reason"] = "need_render"
            out["detail"] = ("页面是 JS 渲染的（HTML 里只有外壳、正文为空），"
                             "静态抓取拿不到内容。需用浏览器渲染后重取，"
                             "见 SKILL.md「JS 渲染页面（need_render）」")
        elif is_wx:
            out["fail_reason"] = "not_article"
            out["detail"] = ("微信域名但不是文章页，也没认出图片合辑；"
                             "分享卡片请在微信里右上角「用浏览器打开」后重新复制链接")
        else:
            out["fail_reason"] = "parse_failed"
            out["detail"] = ("抓到了页面（%dB）但定位不到正文，"
                             "可能需登录或页面结构特殊"
                             % len(page.encode("utf-8", "ignore")))
        return out

    title = title or pick([r'<meta\s+property="og:title"\s+content="([^"]*)"',
                           r'var\s+msg_title\s*=\s*["\']([^"\']*)',
                           r"<title[^>]*>(.*?)</title>"], page)
    source = pick([r'id="js_name"[^>]*>(.*?)</a>', r'data-nickname="([^"]+)"',
                   r'<meta\s+property="og:site_name"\s+content="([^"]*)"'], page)
    if source:
        source = re.sub(r"<[^>]+>", "", source).strip()
    author = pick([r'<meta\s+property="og:article:author"\s+content="([^"]*)"',
                   r'var\s+author\s*=\s*["\']([^"\']*)',
                   r'<meta\s+name="author"\s+content="([^"]*)"'], page)

    published = None
    ct = pick([r'var\s+ct\s*=\s*"(\d{9,13})"', r'var\s+create_time\s*=\s*"(\d{9,13})"'], page)
    if ct:
        ts = int(ct)
        if ts > 10 ** 11:
            ts //= 1000
        published = datetime.fromtimestamp(ts, CST).isoformat()
    else:
        iso = pick([r'<meta\s+property="article:published_time"\s+content="([^"]*)"'], page)
        if iso:
            published = iso

    # 落 stage
    token = hashlib.md5((out["url_canon"] + str(time.time())).encode()).hexdigest()[:8]
    sdir = os.path.join(stage_root, token)
    idir = os.path.join(sdir, "img")
    os.makedirs(idir, exist_ok=True)

    # 原始序号 → 落盘文件名。**不能用列表 append**：跳过的图会让后面全部错位 ——
    # 第 2 张被跳过时，第 3 张会顶到 _repl(1) 的位置，正文引用到错的图。
    slots = {}
    dropped = []
    for i, iu in enumerate(img_urls, 1):
        try:
            st, data = http_get(iu, referer=REFERER, binary=True)
            if st != 200 or len(data) < 100:
                continue
            ct_hdr = "png"
            if data[:3] == b"\xff\xd8\xff":
                ct_hdr = "jpg"
            elif data[:4] == b"GIF8":
                ct_hdr = "gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                ct_hdr = "webp"

            # ---- L1 配图过滤（PRD §6.4）：无信息载体不落盘 ----
            if ct_hdr == "gif":
                dropped.append((i, "动图"))
                continue
            size = _img_size(data)
            if size:                                     # None = 读不出尺寸，保留（不误删）
                w, h = size
                if min(w, h) < MIN_IMG_SIDE:
                    dropped.append((i, "%dx%d 过小" % (w, h)))
                    continue
                if max(w, h) / max(1, min(w, h)) > MAX_IMG_RATIO:
                    dropped.append((i, "%dx%d 长条" % (w, h)))
                    continue

            name = "%02d.%s" % (i, ct_hdr)
            with open(os.path.join(idir, name), "wb") as f:
                f.write(data)
            slots[i] = name
        except Exception as e:
            log("   ! 配图 %d 下载失败: %s" % (i, e))

    # 保序还原成列表：payload 的 images 与统计仍用原变量名，避免别处漏改
    img_rel = ["img/" + slots[k] for k in sorted(slots)]
    if dropped:
        kinds = {}
        for _, r in dropped:
            t = "动图" if r == "动图" else ("尺寸过小" if "过小" in r else "长条")
            kinds[t] = kinds.get(t, 0) + 1
        log("    - 已过滤 %d 张无信息配图（%s）"
            % (len(dropped), "、".join("%s %d 张" % (k, v) for k, v in kinds.items())))

    # 正文里把占位符换成实际落盘路径；**被过滤 / 下载失败的图连占位一起删掉**
    def _repl(m):
        nm = slots.get(int(m.group(1)) + 1)      # 占位符是 0-based，slots 是 1-based
        return "![](img/%s)" % nm if nm else ""

    md = re.sub(r"\x00IMG(\d+)\x00", _repl, md)
    if title:
        md = "# " + title + "\n\n" + md

    payload = {
        "url": url, "url_canon": out["url_canon"],
        "kind": out["kind"],
        "title": title, "source": source, "author": author,
        "published_at": published,
        "content_md": md,
        "images": img_rel,
        "embedded": notes,
    }
    with open(os.path.join(sdir, "payload.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(sdir, "content.md"), "w", encoding="utf-8") as f:
        f.write(md)

    out.update({"ok": True, "title": title, "source": source, "author": author,
                "published_at": published, "chars": len(md),
                "images": len(img_rel), "token": token,
                "embedded": notes,
                "elapsed": round(time.time() - t0, 1)})
    return out


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="MyRag 内容抓取（零依赖）：微信文章 / 微信图片合辑 / 其他网页")
    ap.add_argument("--urls-file", required=True, help="一行一条链接，# 开头与空行忽略")
    ap.add_argument("--stage-root", default=DEFAULT_STAGE_ROOT)
    ap.add_argument("--sleep", type=float, default=2.0, help="每条之间的间隔秒数")
    ap.add_argument("--canon-only", action="store_true",
                    help="只做 URL 规范化（纯字符串、不联网），输出 [{url, url_canon}]，供抓取前查重")
    args = ap.parse_args()

    with open(args.urls_file, encoding="utf-8") as f:
        raw = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]

    if args.canon_only:
        print(json.dumps([{"url": u, "url_canon": canonical_url(u)} for u in raw],
                         ensure_ascii=False))
        return

    seen, urls = set(), []
    for u in raw:
        c = canonical_url(u)
        if c not in seen:
            seen.add(c)
            urls.append(u)

    log("待抓 %d 条（原始 %d 条，去重后）" % (len(urls), len(raw)))
    os.makedirs(args.stage_root, exist_ok=True)

    results = []
    for i, u in enumerate(urls, 1):
        log("[%d/%d] %s" % (i, len(urls), u[:70]))
        r = fetch_one(u, args.stage_root, args.sleep if i > 1 else 0)
        r["idx"] = i
        if r["ok"]:
            log("    OK  [%s] %s字 %s图 token=%s%s" % (
                r.get("kind") or "wechat", r["chars"], r["images"], r["token"],
                "  内嵌:" + ",".join(r["embedded"]) if r.get("embedded") else ""))
        else:
            log("    FAIL %s %s" % (r["fail_reason"], r.get("detail", "")))
        results.append(r)

    ok = sum(1 for r in results if r["ok"])
    log("完成：成功 %d / 失败 %d" % (ok, len(results) - ok))
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
