#!/usr/bin/env python3
"""stage_ocr.py — 对 MyRAG stage 目录（cloud 模式）的配图做 L1 过滤 + macOS Vision OCR，
把图内文字以 blockquote（`> 图片文字：`）注入 content.md，独立成段。

与系统 scripts/ocr_images.py 同一套判据与格式（PRD §6.4），区别只在操作对象：
本脚本作用于 stage 目录（推送 WeKnora 之前），不依赖本地库。

用法（必须用 {{MYRAG_HOME}}/.venv/bin/python 跑，依赖 pyobjc）：
  .venv/bin/python scripts/stage_ocr.py --stage <stage目录> [--force]

行为：
  · L1 硬规则（宽或高 <100px；宽高比 > 1:8；GIF）→ 跳过，不 OCR
  · OCR 文字 < 10 字 → 不注入
  · 幂等：图片行紧后已有 `图片文字` 标记则跳过（--force 重跑）
  · 只改 content.md，img/ 原件不动
stdout：一行 JSON {stage, images, injected, skipped, chars_added}
"""
import argparse
import json
import os
import re
import sys

try:
    import Quartz
    from Foundation import NSAutoreleasePool, NSURL
    import Vision
except ImportError:
    sys.exit("缺少 pyobjc：.venv/bin/pip install pyobjc-framework-Vision pyobjc-framework-Quartz\n")

MARK = "图片文字"
IMG_REF = re.compile(r"^\s*!\[[^\]]*\]\(([^)]+)\)\s*$")


def ocr(path):
    """识别一张图，返回按视觉顺序排列的文本行。"""
    pool = NSAutoreleasePool.alloc().init()
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


def pixel_size(path):
    url = NSURL.fileURLWithPath_(str(path))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return None
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, 0, None)
    if not props:
        return None
    w = props.get(Quartz.kCGImagePropertyPixelWidth)
    h = props.get(Quartz.kCGImagePropertyPixelHeight)
    if w and h:
        return int(w), int(h)
    return None


def l1_skip(path):
    """L1 硬规则：小图 / 超宽比 / GIF → True（不值得 OCR）。"""
    if path.lower().endswith(".gif"):
        return "gif"
    size = pixel_size(path)
    if size is None:
        return "unreadable"
    w, h = size
    if w < 100 or h < 100:
        return "tiny"
    if max(w, h) / min(w, h) > 8:
        return "ratio"
    return None


def dedup_adjacent(lines):
    out = []
    for s in lines:
        s = s.strip()
        if s and (not out or out[-1] != s):
            out.append(s)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    content_path = os.path.join(args.stage, "content.md")
    img_dir = os.path.join(args.stage, "img")
    if not os.path.isfile(content_path):
        sys.exit("content.md not found: " + args.stage)

    lines = open(content_path, "r", encoding="utf-8").read().splitlines()
    out = []
    stats = {"images": 0, "injected": 0, "skipped": 0, "chars_added": 0}
    changed = False

    for line in lines:
        out.append(line)
        m = IMG_REF.match(line)
        if not m:
            continue
        stats["images"] += 1
        rel = m.group(1).split("?")[0]
        name = os.path.basename(rel)
        img_path = os.path.join(img_dir, name)
        if not os.path.isfile(img_path):
            stats["skipped"] += 1
            continue

        # 幂等：紧后已有标记则跳过
        tail = "\n".join(lines[len(out) - 1:len(out) + 4])
        if not args.force and MARK in tail:
            stats["skipped"] += 1
            continue

        reason = l1_skip(img_path)
        if reason:
            stats["skipped"] += 1
            continue

        texts = dedup_adjacent(ocr(img_path))
        joined = " ".join(texts)
        if len(joined) < 10:
            stats["skipped"] += 1
            continue

        block_lines = ["> %s：" % MARK] + ["> " + s for s in texts]
        out.append("")
        out.extend(block_lines)
        stats["injected"] += 1
        stats["chars_added"] += len(joined)
        changed = True

    if changed:
        with open(content_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")

    stats["stage"] = os.path.basename(args.stage.rstrip("/"))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
