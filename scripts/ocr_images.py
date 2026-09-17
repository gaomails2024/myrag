#!/usr/bin/env python3
"""配图 OCR（PRD §6.4）—— 走 macOS Vision，**本地推理、内容不上传**。

**为了什么**：图片里的文字此前完全搜不到（图片合辑类文章甚至「正文 0 字」）。
把 OCR 文字写进正文后，图片内容就进入了切段与索引。

**在「遗忘」链条里的位置**：这是「弃载体、保语义」的前半段 —— 文字先落下来，
之后删图才不会连内容一起丢掉。所以顺序必须是**先 OCR、后删图**，不能反过来。

用法：

    .venv/bin/python scripts/ocr_images.py T-20260907-01     # 一篇
    .venv/bin/python scripts/ocr_images.py A-20260816-01 --min-chars 10
    .venv/bin/python scripts/ocr_images.py --all             # 全部文章
    .venv/bin/python scripts/ocr_images.py --all --dry-run   # 只统计，不改文件
    .venv/bin/python scripts/ocr_images.py X --force         # 已处理过的也重跑

**幂等**：正文里已有 `图片文字` 标记的图会被跳过，重复跑不会叠加。
**写回策略**：在图片引用行的**紧后面**插入一段 blockquote，保留原引用不动 ——
删不删图是另一个决定，这一步只负责"把文字留下"。
"""

import argparse
import json
import pathlib
import re
import sys
import time

try:
    import Vision
    import Quartz
    from Foundation import NSURL, NSAutoreleasePool
except ImportError:
    sys.exit("缺少 pyobjc：.venv/bin/pip install pyobjc-framework-Vision pyobjc-framework-Quartz\n"
             "（仅 macOS 可用；其他平台需换 OCR 引擎，见 PRD §6.4）")

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
MEDIA_DIR = ROOT / "data" / "media"
STAGE_DIR = ROOT / "data" / "_stage"      # 抓取落地区：入库前在这里过滤 + OCR

# OCR 文字少于此长度视为「无信息载体」（小图标、分割线、扫码引导图），不写进正文。
# 与 PRD §6.4 的 L2 判据一致。
MIN_CHARS = 10

MARK = "图片文字"          # 正文里的标记，幂等判断靠它
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def ocr(path):
    """识别一张图，返回按视觉顺序排列的文本行。"""
    pool = NSAutoreleasePool.alloc().init()      # 不加这个，批量跑会持续涨内存
    try:
        url = NSURL.fileURLWithPath_(str(path))
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None:
            return []
        img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if img is None:
            return []
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLanguages_(["zh-Hans", "en-US"])
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        handler.performRequests_error_([req], None)
        return [o.topCandidates_(1)[0].string() for o in (req.results() or [])]
    finally:
        del pool


def find_raw(article_id):
    """正文文件在 raw/<年>/<id>.md，年份不写死 —— 按 id 反查。"""
    hits = list(RAW_DIR.rglob("%s.md" % article_id))
    return hits[0] if hits else None


def clean_lines(lines):
    """去掉纯噪声行（单字符、纯符号），保留有信息量的。"""
    out = []
    for ln in lines:
        s = (ln or "").strip()
        if len(s) < 2:
            continue
        if not re.search(r"[\w\u4e00-\u9fff]", s):   # 没有任何文字/数字
            continue
        out.append(s)
    return out


def merge_duplicates(lines):
    """相邻重复行只留一条（Vision 对同一段偶有重复输出）。"""
    out = []
    for s in lines:
        if not out or out[-1] != s:
            out.append(s)
    return out


def inject(raw_text, article_id, texts):
    """把 {文件名: 文字} 插到正文中对应的图片引用行后面。"""
    added = 0
    for name, lines in texts.items():
        if not lines:
            continue
        body = "\n".join("> " + s for s in lines)
        block = "\n\n> %s：\n%s" % (MARK, body)
        # 匹配 `![](../../media/<id>/<name>)`（允许前后空白）
        pat = re.compile(r"(^[ \t]*!\[[^\]]*\]\([^)]*/%s\)[ \t]*$)" % re.escape(name),
                         re.M)
        m = pat.search(raw_text)
        if not m:
            continue
        if MARK in raw_text[m.end():m.end() + 40]:      # 紧后面已有标记 → 幂等跳过
            continue
        raw_text = raw_text[:m.end()] + block + raw_text[m.end():]
        added += 1
    return raw_text, added


def process(article_id, min_chars, force, dry_run):
    folder = MEDIA_DIR / article_id
    if not folder.is_dir():
        return {"id": article_id, "err": "没有这个配图目录"}
    imgs = sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMG_EXT)
    if not imgs:
        return {"id": article_id, "err": "目录里没有图片"}

    raw = find_raw(article_id)
    if raw is None:
        return {"id": article_id, "err": "找不到正文文件"}

    original = raw.read_text(encoding="utf-8")
    if not force and MARK in original:
        pass          # 仍要跑：可能有新增的图。逐图幂等判断在 inject() 里。

    texts, skipped, t0 = {}, 0, time.perf_counter()
    for p in imgs:
        lines = merge_duplicates(clean_lines(ocr(p)))
        n = sum(len(x) for x in lines)
        if n < min_chars:
            skipped += 1
            continue
        texts[p.name] = lines

    total_chars = sum(sum(len(x) for x in v) for v in texts.values())
    dt = time.perf_counter() - t0

    new_text, added = inject(original, article_id, texts)
    if added and not dry_run:
        raw.write_text(new_text, encoding="utf-8")

    return {"id": article_id, "imgs": len(imgs), "kept": len(texts),
            "skipped": skipped, "chars": total_chars, "injected": added,
            "sec": dt, "raw": raw.name, "dry": dry_run}


def process_stage(token, min_chars, dry_run):
    """处理抓取落地区（**入库之前**）：OCR 每张图，有文字的留下、无文字的连图带引用删掉。

    与 `--all`（存量）模式的三个差别：
      1. 改的是 `data/_stage/<token>/`，不是已入库的 `data/raw/`；
      2. 无文字的图要**真删文件**（不删的话后端照样会把它入库）；
      3. 必须**同步 payload.json 的 images** —— 后端是按那份清单决定入库哪些图的。
    """
    sdir = STAGE_DIR / token
    if not sdir.is_dir():
        return {"id": token, "err": "stage 目录不存在"}
    img_dir = sdir / "img"
    md_path = sdir / "content.md"
    pay_path = sdir / "payload.json"
    if not md_path.exists():
        return {"id": token, "err": "没有 content.md"}

    imgs = sorted(p for p in img_dir.iterdir()
                  if p.is_file() and p.suffix.lower() in IMG_EXT) if img_dir.is_dir() else []
    md = md_path.read_text(encoding="utf-8")
    kept, dropped, chars, t0 = [], [], 0, time.perf_counter()

    for p in imgs:
        lines = merge_duplicates(clean_lines(ocr(p)))
        n = sum(len(x) for x in lines)
        if n < min_chars:
            dropped.append(p.name)
            continue
        kept.append(p.name)
        chars += n
        block = "\n\n> %s：\n%s" % (MARK, "\n".join("> " + s for s in lines))
        pat = re.compile(r"(^[ \t]*!\[[^\]]*\]\([^)]*/%s\)[ \t]*$)" % re.escape(p.name), re.M)
        m = pat.search(md)
        if m and MARK not in md[m.end():m.end() + 40]:
            md = md[:m.end()] + block + md[m.end():]

    # 无信息载体：删文件 + 去掉正文里那一行引用（否则入库后是破图）
    for nm in dropped:
        if not dry_run:
            (img_dir / nm).unlink(missing_ok=True)
        md = re.sub(r"^[ \t]*!\[[^\]]*\]\([^)]*/%s\)[ \t]*\n?" % re.escape(nm),
                    "", md, flags=re.M)

    if not dry_run:
        md_path.write_text(md, encoding="utf-8")
        if pay_path.exists():
            try:
                pay = json.loads(pay_path.read_text(encoding="utf-8"))
                keep = set(kept)
                pay["images"] = [x for x in (pay.get("images") or [])
                                 if pathlib.Path(x).name in keep]
                pay_path.write_text(json.dumps(pay, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                return {"id": token, "err": "payload.json 同步失败：%s" % e}

    return {"id": token, "imgs": len(imgs), "kept": len(kept), "skipped": len(dropped),
            "chars": chars, "injected": len(kept), "sec": time.perf_counter() - t0,
            "raw": "content.md", "dry": dry_run}


def main():
    ap = argparse.ArgumentParser(description="配图 OCR：把图里的文字写进正文（本地 Vision）")
    ap.add_argument("article_id", nargs="?", help="文章 id，如 T-20260907-01")
    ap.add_argument("--all", action="store_true", help="处理所有有配图的文章")
    ap.add_argument("--stage", metavar="TOKEN",
                    help="处理抓取落地区（入库前）：OCR + 剔除无信息图 + 同步 payload")
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS,
                    help="OCR 文字少于该长度视为无信息载体，不写进正文（默认 %d）" % MIN_CHARS)
    ap.add_argument("--force", action="store_true", help="已注入过的也重跑")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不改正文")
    args = ap.parse_args()

    if args.stage:
        r = process_stage(args.stage, args.min_chars, args.dry_run)
        if r.get("err"):
            sys.exit("✗ %s" % r["err"])
        print("  %s  %d 张 → 有文字 %d 张 / %d 字；无信息已删 %d 张  %.1fs%s"
              % (r["id"], r["imgs"], r["kept"], r["chars"], r["skipped"], r["sec"],
                 "（dry-run）" if args.dry_run else ""))
        return

    if args.all:
        ids = sorted(p.name for p in MEDIA_DIR.iterdir() if p.is_dir())
    elif args.article_id:
        ids = [args.article_id]
    else:
        ap.error("给一个 article_id，或用 --all / --stage <token>")

    if args.dry_run:
        print("（dry-run：不改任何文件）\n")

    tot = {"imgs": 0, "kept": 0, "chars": 0, "injected": 0, "err": 0}
    for aid in ids:
        r = process(aid, args.min_chars, args.force, args.dry_run)
        if r.get("err"):
            tot["err"] += 1
            print("  %-16s ✗ %s" % (aid, r["err"]))
            continue
        tot["imgs"] += r["imgs"]; tot["kept"] += r["kept"]
        tot["chars"] += r["chars"]; tot["injected"] += r["injected"]
        if r["chars"] or r["injected"]:
            print("  %-16s %3d 张 → 有效 %3d 张 / %6d 字  注入 %3d 处  %.1fs"
                  % (aid, r["imgs"], r["kept"], r["chars"], r["injected"], r["sec"]))

    print("\n合计：%d 张图，有效 %d 张，%d 字，注入 %d 处%s"
          % (tot["imgs"], tot["kept"], tot["chars"], tot["injected"],
             "（dry-run，未写入）" if args.dry_run else ""))
    if not args.dry_run and tot["injected"]:
        print("提示：正文已更新，跑 scripts/reindex.py 让新文字进入检索索引。")


if __name__ == "__main__":
    main()
