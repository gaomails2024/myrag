#!/usr/bin/env python3
"""JS 渲染页面抓取 —— 处理 `wx_fetch.py` 报出的 `need_render`。

**为什么单独一个脚本，而不是塞进 wx_fetch.py**：
`wx_fetch.py` 的硬约束是**零依赖**（Skill 侧要能在不装任何包的机器上跑）。
渲染必须要 playwright，两者不能混。所以分工是：
**wx_fetch 只负责识别**（发现「有壳无内容」就报 `need_render`），**渲染在这里做**。

**解析与落盘不在这里重写**（2026-10-07 根治）：本脚本只负责「开浏览器、拿到渲染后的
DOM」，正文 / 图片 / 元数据的解析一律调 `wx_fetch.parse_page`，落盘调
`wx_fetch.save_stage` —— 与静态路径**共用同一份实现**。
以前这里自己写了一套：正文定位用的是面向普通网站的 `extract_generic`（选出的块里
一张图都没有），配图下载还漏了微信图床必需的 `Referer` —— 结果走渲染的 9 篇文章
全部 0 图、图内文字全丢。共用一个实现之后，这两类分叉都不会再发生。

用法：

    .venv/bin/python scripts/render_fetch.py <URL>
    .venv/bin/python scripts/render_fetch.py <URL> --stage-root /path/to/_stage
    .venv/bin/python scripts/render_fetch.py <URL> --wait 4000      # 多等一会儿再取

前置（一次性，约 180MB）：`.venv/bin/python -m playwright install chromium`
"""

import argparse
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skill" / "scripts"))

try:
    import wx_fetch as wf          # 复用它的解析 / 落盘 / 常量，不重复实现
except Exception as e:             # noqa: BLE001
    sys.exit("加载 wx_fetch 失败（它在 skill/scripts/ 下，应与本仓库一起存在）：%s" % e)


# 微信把配图放在**正文容器之外**的图片容器里。实测（2026-10-07，小菜园那篇）：
#   · `#js_content` 里只有 1 张赞赏二维码，正文配图一张都不在；
#   · 真正的配图在 `.share_media .swiper_item` 里，**每个 item 一张图**，
#     其中 `data-src` 是原图、里面的 `<img src>` 是压缩版 —— 同一 item 只取一张，
#     否则同一张图的原图与压缩版会被当成两张。
#   · 第一个 item 是轮播器的占位展示，与后面某个 item 是同一张图；
#     按「去掉 query 的路径」去重即可去掉这个重复。
# 这个列表只作为 `extra_img_urls` 传给 parse_page，**只在正文里一张图都没有时
# 才会被启用**（理由见 `wx_fetch.parse_page` 的 docstring）。
# 渲染后的微信页面会把**页面 UI 节点**塞进正文容器里，最重的是**整个赞赏弹窗**
# （实测 `#js_content` 里嵌着 `#contentAreaWrp.weui-half-screen-dialog__bd`，
# 内含「名称已清空 / 赞赏金额 / 最低赞赏 ¥0 / 赠予作者其它金额」等一大段弹窗文字）。
# 只靠字符串剥标签会把它们当正文吃进来 —— 实测多出上百字的弹窗文字。
# 这些容器都是组件级 class/id，本来就不属于正文；静态路径的正文里没有它们
# （静态 HTML 里这些节点不在正文容器内），所以只在渲染这一侧剔除。
_UI_NOISE_SELECTORS = [
    "#contentAreaWrp",                    # 赞赏弹窗内容区（被塞进了正文容器内）
    ".wx_bottom_modal_group",             # 底部弹窗组
    ".wx_bottom_modal_group_container",
    ".weui-half-screen-dialog",           # 半屏对话框
    ".reward_pop_panel",                  # 赞赏面板
    ".dialog-pay",                        # 赞赏对话框
    ".rich_media_meta",                   # 作者 / 时间 / 公众号 信息条
    ".rich_media_meta_list",
    ".rich_media_meta_link",
    ".author_profile-pay_area",           # 赞赏作者区
    ".album_con",                         # 「收录于 xx」话题标签
    ".js_alert_confirm",                  # 确认弹窗
    ".qr_code_pc_outer",                  # 电脑端二维码推广
    "script", "style", "noscript",
]

_DOM_BODY_JS = """
(selectors) => {
  const jc = document.querySelector('#js_content')
          || document.querySelector('#js_image_content');
  if (!jc) return null;
  const clone = jc.cloneNode(true);      // 在副本上删，不动真实页面
  clone.querySelectorAll(selectors.join(',')).forEach(e => e.remove());
  return clone.innerHTML;
}
"""

_DOM_IMAGES_JS = """
() => {
  const seen = new Set(), out = [];
  document.querySelectorAll('.share_media .swiper_item').forEach(it => {
    const img = it.querySelector('img');
    const u = it.getAttribute('data-src')
           || (img && (img.getAttribute('data-src') || img.getAttribute('src')));
    if (!u || !u.startsWith('http') || u.startsWith('data:')) return;
    const key = u.split('?')[0];
    if (seen.has(key)) return;
    seen.add(key);
    out.push(u);
  });
  return out;
}
"""


def render_page(url, wait_ms, timeout_ms):
    """用真实浏览器打开页面，返回 (渲染后的 HTML, 正文 HTML, 正文容器之外的图片 URL 列表)。

    三个产物都交给 `wx_fetch.parse_page` 解析（与静态路径同一份实现）：
      · HTML         —— 元数据（标题 / 公众号 / 发布时间）从里面取；
      · 正文 HTML    —— 用 DOM 精确取出，并顺手剔除页面 UI 节点（见 _UI_NOISE_SELECTORS）；
      · 图片 URL     —— `extra_img_urls`。静态路径没有 DOM 查询能力，取不到它，
                       这正是渲染路径必须存在的原因。
    """
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
            # 配图是懒加载：不滚到底，图不会加载、DOM 里的地址也还是占位符
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(300)
            except Exception:                       # noqa: BLE001
                pass
            return (page.content(),
                    page.evaluate(_DOM_BODY_JS, _UI_NOISE_SELECTORS),
                    page.evaluate(_DOM_IMAGES_JS))
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
    html, body_html, extra_imgs = render_page(url, args.wait, args.timeout)
    elapsed = time.perf_counter() - t0

    r = wf.parse_page(html, url, extra_img_urls=extra_imgs, body_html=body_html)
    if not r["ok"]:
        print("✗ 渲染后仍取不到内容：%s\n  %s" % (r["fail_reason"], r.get("detail") or ""))
        print("  渲染耗时 %.1fs，HTML %d 字节" % (elapsed, len(html)))
        sys.exit(1)

    token, md, img_rel = wf.save_stage(
        args.stage_root, url, wf.canonical_url(url), r["kind"],
        r["title"], r["source"], r["author"], r["published_at"],
        r["md"], r["img_urls"], r["notes"], fetched_by="render")

    print("OK  [%s] %d字 %d图 token=%s  渲染 %.1fs"
          % (r["kind"], len(md), len(img_rel), token, elapsed))
    if r["notes"]:
        print("    - 附注：%s" % "、".join(r["notes"]))
    print("    stage: %s" % os.path.join(args.stage_root, token))
    print("    下一步：判类 → OCR（scripts/ocr_images.py --stage %s）→ 入库" % token)


if __name__ == "__main__":
    main()
