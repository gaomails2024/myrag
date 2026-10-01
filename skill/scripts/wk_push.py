#!/usr/bin/env python3
"""wk_push.py — 把 MyRAG stage 目录（微信文章抓取产物）推送到 WeKnora 知识库。

P1 采集链路出口：端侧抓取/判类/OCR 不变，本脚本替代原 POST /api/ingest（本地库），
将 Markdown 底本 + 元数据写入 WeKnora 指定 KB（REST file 上传，幂等）。

零依赖（纯 stdlib，同 wx_fetch.py 约定）。

用法：
  python3 wk_push.py --stage <stage目录> [--base-url U] [--api-key K] [--kb ID]
                     [--collector 名字] [--dry-run] [--no-dedup]

配置优先级：命令行 > 环境变量（WEKNORA_BASE_URL / WEKNORA_API_KEY / WEKNORA_KB_ID /
MYRAG_COLLECTOR）> ~/.myrag-cloud.json（chmod 600，不进仓库）。

stdout 只回一行 JSON 简报：
  成功: {"ok": true,  "dup": false, "knowledge_id": "...", "title": "...", "url_canon": "..."}
  重复: {"ok": true,  "dup": true,  "knowledge_id": "...", ...}
  失败: {"ok": false, "dup": false, "fail_reason": "<枚举>", ...}
fail_reason 枚举：bad_stage / bad_config / network / http_<code> / upload_rejected
正文与 metadata 明细不打印（避免撑爆上下文）。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid

IMG_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")

FALLBACK_CFG = os.path.expanduser("~/.myrag-cloud.json")


def fail(reason, **extra):
    out = {"ok": False, "dup": False, "fail_reason": reason}
    out.update(extra)
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


def load_config(args):
    cfg = {}
    if os.path.exists(FALLBACK_CFG):
        try:
            with open(FALLBACK_CFG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            fail("bad_config", detail="config file unreadable")
    base_url = args.base_url or os.environ.get("WEKNORA_BASE_URL") or cfg.get("base_url")
    api_key = args.api_key or os.environ.get("WEKNORA_API_KEY") or cfg.get("api_key")
    kb_id = args.kb or os.environ.get("WEKNORA_KB_ID") or cfg.get("kb_id")
    tenant_id = os.environ.get("WEKNORA_TENANT_ID") or cfg.get("tenant_id")
    collector = args.collector or os.environ.get("MYRAG_COLLECTOR") or cfg.get("collector")
    if not base_url or not api_key or not kb_id:
        fail("bad_config", detail="need base-url/api-key/kb (arg > env > ~/.myrag-cloud.json)")
    return base_url.rstrip("/"), api_key, kb_id, str(tenant_id or ""), collector or ""


def http_json(method, url, api_key, tenant_id, body=None, timeout=30):
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", api_key)
    if tenant_id:
        req.add_header("X-Tenant-ID", tenant_id)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}
    except Exception as e:
        fail("network", detail=str(e)[:200])


def http_multipart(url, api_key, tenant_id, fields, file_field, filename, content, timeout=120):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
             % (boundary, k, v)).encode("utf-8")
        )
    head = ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: text/markdown; charset=utf-8\r\n\r\n" % (boundary, file_field, filename))
    parts.append(head.encode("utf-8"))
    parts.append(content.encode("utf-8"))
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(url, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("X-API-Key", api_key)
    if tenant_id:
        req.add_header("X-Tenant-ID", tenant_id)
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}
    except Exception as e:
        fail("network", detail=str(e)[:200])


def strip_images(md):
    """剥离 ![]() 图片行（图内文字已由 OCR 以 blockquote 注入正文，不随本行丢失）。"""
    kept, stripped = [], 0
    for line in md.splitlines():
        if IMG_LINE.match(line):
            stripped += 1
            continue
        kept.append(line)
    return "\n".join(kept).strip() + "\n", stripped


LEDGER = os.path.expanduser("~/.myrag-cloud-ledger.jsonl")
_BAD_FN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def make_file_name(title, canon_hash):
    """显示名即文件名：`<标题(清洗截断)>.md`（UI 列表自带时间列，日期前缀冗余）。"""
    t = _BAD_FN.sub("", title).strip().strip(".")
    t = re.sub(r"\s+", " ", t)[:100].strip()
    if not t:
        t = "wx_%s" % canon_hash
    return "%s.md" % t


def ledger_lookup(url_canon):
    """本地台账：url_canon → knowledge_id。返回 None 表示未见。"""
    if not os.path.exists(LEDGER):
        return None
    hit = None
    try:
        with open(LEDGER, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("url_canon") == url_canon:
                    hit = rec
    except Exception:
        return None
    return hit


def ledger_append(rec):
    try:
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def ensure_tag_ids(base_url, api_key, tenant_id, kb_id, names):
    """把标签名解析成 WeKnora 原生 tag_id；库里没有的自动创建（MyRAG 式自动打标）。

    返回 tag_id 列表；names 去空白、去空、去重，保持原顺序。"""
    want = []
    for n in names:
        n = str(n).strip()
        if n and n not in want:
            want.append(n)
    if not want:
        return []
    st, resp = http_json("GET", "%s/knowledge-bases/%s/tags?page_size=200" % (base_url, kb_id),
                         api_key, tenant_id)
    if st != 200:
        return []
    existing = {}
    for t in ((resp.get("data") or {}).get("data") or []):
        nm = (t.get("name") or "").strip()
        if nm and nm not in existing:
            existing[nm] = t.get("id")
    ids = []
    for n in want:
        tid = existing.get(n)
        if not tid:
            st2, r2 = http_json("POST", "%s/knowledge-bases/%s/tags" % (base_url, kb_id),
                                api_key, tenant_id, {"name": n})
            if st2 in (200, 201):
                tid = ((r2.get("data") or {}).get("id"))
                if tid:
                    existing[n] = tid
        if tid:
            ids.append(tid)
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, help="stage 目录（含 payload.json + content.md）")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--kb", default=None)
    ap.add_argument("--collector", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-dedup", action="store_true")
    args = ap.parse_args()

    stage = args.stage
    payload_path = os.path.join(stage, "payload.json")
    content_path = os.path.join(stage, "content.md")
    if not (os.path.isdir(stage) and os.path.isfile(payload_path) and os.path.isfile(content_path)):
        fail("bad_stage", detail="need payload.json + content.md in %s" % stage)
    try:
        payload = json.load(open(payload_path, "r", encoding="utf-8"))
        md_raw = open(content_path, "r", encoding="utf-8").read()
    except Exception as e:
        fail("bad_stage", detail=str(e)[:200])

    base_url, api_key, kb_id, tenant_id, collector = load_config(args)

    title = (payload.get("title") or "").strip() or "untitled"
    url = payload.get("url") or ""
    url_canon = payload.get("url_canon") or url
    source = payload.get("source") or ""
    author = payload.get("author") or ""
    published_at = payload.get("published_at") or ""
    date = (published_at[:10] or "") if len(published_at) >= 10 else ""
    if not date:
        import datetime
        date = datetime.date.today().strftime("%Y%m%d")
    date = date.replace("-", "")

    # 端侧批注（判类/摘要/标签，Agent 写入；缺省容忍）
    ann = {}
    ann_path = os.path.join(stage, "annotations.json")
    if os.path.isfile(ann_path):
        try:
            ann = json.load(open(ann_path, "r", encoding="utf-8"))
        except Exception:
            ann = {}

    canon_hash = hashlib.md5(url_canon.encode("utf-8")).hexdigest()[:10]
    file_name = make_file_name(title, canon_hash)

    md_body, n_img = strip_images(md_raw)
    header = "# %s\n\n" % title
    meta_line = "> 来源：%s" % (source or "未知公众号")
    if author:
        meta_line += "｜作者：%s" % author
    if published_at:
        meta_line += "｜发布：%s" % published_at[:10]
    if url:
        meta_line += "｜[原文链接](%s)" % url
    if collector:
        meta_line += "｜收集人：%s" % collector
    meta_line += "\n\n"
    content = header + meta_line + md_body

    # 端侧批注的标签：统一为去空白、去空、去重的名字列表
    raw_tags = ann.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = raw_tags.split(",")
    tag_names = []
    for t in raw_tags:
        t = str(t).strip()
        if t and t not in tag_names:
            tag_names.append(t)

    # 服务端要求 map[string]string：值一律字符串（tags 逗号拼接），否则 400
    tags = ", ".join(tag_names)
    metadata = {
        "url": url,
        "url_canon": url_canon,
        "source": source,
        "author": author,
        "published_at": published_at,
        "collector": collector,
        "category": str(ann.get("category", "")),
        "summary": str(ann.get("summary", "")),
        "tags": str(tags),
        "pipeline": "myrag-p1",
    }

    if args.dry_run:
        print(json.dumps({
            "ok": True, "dry_run": True, "file_name": file_name, "title": title,
            "url_canon": url_canon, "bytes": len(content.encode("utf-8")),
            "images_stripped": n_img, "tags": tag_names,
        }, ensure_ascii=False))
        sys.exit(0)

    # 预检去重：本地台账（url_canon 精确）→ 服务端 409（同名文件）兜底
    if not args.no_dedup:
        hit = ledger_lookup(url_canon)
        if hit:
            print(json.dumps({
                "ok": True, "dup": True, "knowledge_id": hit.get("knowledge_id"),
                "title": title, "url_canon": url_canon,
            }, ensure_ascii=False))
            return

    fields = {
        "fileName": file_name,
        "metadata": json.dumps(metadata, ensure_ascii=False),
        "channel": "myrag",
    }
    # 批注标签 → WeKnora 原生标签（库里没有自动建），随上传挂载，UI 可见可筛选
    tag_ids = ensure_tag_ids(base_url, api_key, tenant_id, kb_id, tag_names)
    if tag_ids:
        fields["tag_ids"] = ",".join(tag_ids)
    st, resp = http_multipart(
        "%s/knowledge-bases/%s/knowledge/file" % (base_url, kb_id),
        api_key, tenant_id, fields, "file", file_name, content)

    if st in (200, 201):
        kd = (resp.get("data") or {})
        ledger_append({"url_canon": url_canon, "knowledge_id": kd.get("id"),
                       "file_name": file_name, "title": title, "kb_id": kb_id})
        print(json.dumps({
            "ok": True, "dup": False, "knowledge_id": kd.get("id"),
            "title": title, "url_canon": url_canon,
        }, ensure_ascii=False))
    elif st == 409:
        kd = (resp.get("data") or {})
        ledger_append({"url_canon": url_canon, "knowledge_id": kd.get("id"),
                       "file_name": file_name, "title": title, "kb_id": kb_id})
        print(json.dumps({
            "ok": True, "dup": True, "knowledge_id": kd.get("id"),
            "title": title, "url_canon": url_canon,
        }, ensure_ascii=False))
    else:
        msg = (resp.get("error") or {}).get("message") or resp.get("raw") or str(resp)[:200]
        print(json.dumps({
            "ok": False, "dup": False, "fail_reason": "http_%d" % st,
            "title": title, "url_canon": url_canon, "detail": str(msg)[:200],
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
