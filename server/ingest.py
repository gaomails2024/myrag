# -*- coding: utf-8 -*-
"""入库：切段 + 向量化 + 落盘 + 写库（PRD §6.1、§7.2、§7.3）。

切段规则（PRD §7.2）：
  · 按 Markdown 标题层级切
  · 单段上限 1024 token；超出按段落切，仍超长按句子切，最后按 token 硬切
  · 相邻段重叠 15%（约 154 token），取自上一段结尾
  · 代码块与表格整块保留，绝不切开
  · heading_path 记录完整标题链

标题链识别（微信正文的实际情况，实测三篇得出）：
  · 文档第一个标题是文章名，不进链；其余标题**含 H1** 都进链。
    微信正文的章节标题被导出成 `# 01` / `# 02` 形态的 H1，若只认 `##` 及
    以下，整篇会退化成「单一标题段 + 按长度硬切」，切段边界落在论证中间
    ——正是 §7.2 明令要避免的失败。
  · 整行加粗视为伪标题：微信子标题常是 `<strong>` 整段加粗，导出成
    `**项目简介**` / `  ** 2.1 五层记忆**`。
  · 纯编号标题与紧随其后的标题行合并：`# 01` + `缘起` → `01 缘起`；
    `**01**` + `**大模型正在毁掉你的深度学习**` → `01 大模型正在毁掉你的深度学习`。
"""

import importlib.util
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone

import numpy as np

from . import config, db, embed

CST = timezone(timedelta(hours=8))

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
IMG_REF_RE = re.compile(r"!\[([^\]]*)\]\(\s*img/([^)\s]+)\s*\)")

# 整行加粗 = 伪标题。`****` 这类空行不匹配；行内加粗（`**a**b`）也不匹配，
# 因为它们不满足「行尾就是 **」。
BOLD_LINE_RE = re.compile(r"^[ \t]*\*\*(?!\*)[ \t]*(.+?)[ \t]*\*\*[ \t]*$")
# 纯编号标题：`01` / `2.1` / `三、`
NUM_LABEL_RE = re.compile(r"^(\d+(?:\.\d+)*|[一二三四五六七八九十]+)[.、]?$")
END_PUNCT = "。！？；：!?;:"


def _num_level(num: str) -> int:
    """由编号推层级：`01` → 1，`2.1` → 2，`2.1.3` → 3。"""
    m = NUM_LABEL_RE.match(num)
    if not m or not m.group(1)[0].isdigit():
        return 1
    return min(1 + m.group(1).count("."), 6)


def _is_pseudo_heading(t: str) -> bool:
    """整行加粗到底是子标题还是强调句。

    判据：≤40 字且不以句末标点收尾。实测的区分效果：
      `**项目简介**`                                → 标题
      `**所谓"AI失业潮"，是一场精心设计的公关战**`      → 标题
      `**当业务人员懂技术，……会产生质的飞跃。**`       → 强调句（句号收尾）
      `**也许你还想看：**`                           → 强调句（冒号收尾）
    """
    s = (t or "").strip()
    if not s or len(s) > 40:
        return False
    return s[-1] not in END_PUNCT


def _is_short_label(line: str) -> bool:
    """`# 01` 下面那行「缘起」——短、无标记、不成句，可并入编号当标题名。"""
    s = (line or "").strip()
    if not s or len(s) > 20:
        return False
    if s[0] in "#->|!*`" or "**" in s or "![" in s:
        return False
    return s[-1] not in END_PUNCT


# ================================================================ 切段

def _count(text: str) -> int:
    return embed.count_tokens(text)


def _tail(text: str, k: int) -> str:
    """取结尾 k 个 token 的近似文本（重叠用）。"""
    if k <= 0:
        return ""
    tk = embed.get_tokenizer()
    ids = tk.encode(text, add_special_tokens=False)
    if len(ids) <= k:
        return text
    return tk.decode(ids[-k:], skip_special_tokens=True).strip()


def _hard_split(text: str, limit: int):
    """最后兜底：按 token 硬切。"""
    tk = embed.get_tokenizer()
    ids = tk.encode(text, add_special_tokens=False)
    out = []
    for i in range(0, len(ids), limit):
        piece = tk.decode(ids[i:i + limit], skip_special_tokens=True).strip()
        if piece:
            out.append(piece)
    return out or [text]


def _split_oversized(text: str, limit: int):
    """超长段落逐级拆：① 空行 ② 句末标点 ③ token 硬切。"""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) == 1:
        parts = [s.strip() for s in re.split(r"(?<=[。！？；!?;])", text) if s.strip()]
    if len(parts) == 1:
        return _hard_split(text, limit)
    out = []
    for p in parts:
        out.extend(_hard_split(p, limit) if _count(p) > limit else [p])
    return out


def _blocks(md: str):
    """切成块。kind: para | code | table。

    标题链规则见模块 docstring。第一个标题视为文章名、不进链，其余标题
    （含后续 H1）连同整行加粗伪标题一起进链。
    """
    lines = md.split("\n")
    blocks, stack, buf = [], [], []
    cur_path = ""
    i, n = 0, len(lines)
    first_heading = True
    pending = None      # 纯编号标题，等待与下一行标题名合并：(编号, 层级)

    def flush():
        nonlocal buf
        if buf:
            t = "\n".join(buf).strip()
            if t:
                blocks.append({"heading_path": cur_path, "kind": "para", "text": t})
            buf = []

    def commit(title, level):
        nonlocal stack, cur_path
        stack = [s for s in stack if s[0] < level]
        stack.append((level, title))
        cur_path = " > ".join(t for _, t in stack)

    def take(title, level):
        """有 pending 编号就合并成 `01 缘起`，层级以编号为准。"""
        nonlocal pending
        if pending:
            pnum, plvl = pending
            pending = None
            return "%s %s" % (pnum, title), (_num_level(pnum) or plvl)
        return title, level

    def settle_pending():
        """编号后面没等到标题名 —— 编号自己当标题，语义仍强于留空。"""
        nonlocal pending
        if pending:
            pnum, plvl = pending
            pending = None
            commit(pnum, _num_level(pnum) or plvl)

    while i < n:
        line = lines[i]
        fm = FENCE_RE.match(line)
        hm = HEADING_RE.match(line)

        # 空行不打断「编号 → 标题名」的等待
        if pending and not line.strip():
            i += 1
            continue

        if fm:
            flush()
            ch = fm.group(1)[0]
            chunk = [line]
            i += 1
            while i < n:
                cur = lines[i]
                chunk.append(cur)
                i += 1
                s = cur.strip()
                if s.startswith(ch * 3) and set(s) == {ch}:
                    break
            blocks.append({"heading_path": cur_path, "kind": "code",
                           "text": "\n".join(chunk).strip()})
            continue

        if hm:
            flush()
            level = len(hm.group(1))
            title = hm.group(2).strip()
            i += 1
            if first_heading:               # 第一个标题 = 文章名，不进链
                first_heading = False
                stack, cur_path = [], ""
                continue
            if NUM_LABEL_RE.match(title):   # 纯编号，等下一行看有没有标题名
                pending = (title, level)
                continue
            commit(*take(title, level))
            continue

        bm = BOLD_LINE_RE.match(line)
        if bm and _is_pseudo_heading(bm.group(1)):
            flush()
            title = bm.group(1).strip()
            i += 1
            if first_heading:
                first_heading = False
                continue
            if NUM_LABEL_RE.match(title):
                pending = (title, 1)
                continue
            head = title.split(" ", 1)[0]
            level = (_num_level(head) if NUM_LABEL_RE.match(head)
                     else (stack[-1][0] if stack else 1))
            commit(*take(title, level))
            continue

        if line.lstrip().startswith("|") and line.count("|") >= 2:
            flush()
            tbl = []
            while i < n and lines[i].lstrip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            blocks.append({"heading_path": cur_path, "kind": "table",
                           "text": "\n".join(tbl).strip()})
            continue

        # 块引用（`> `）单独成块 —— **尤其是 OCR 注入的「图片文字」**。
        # 以前它只是一行普通文本，和前后正文并进同一个 para、再按标题合并成
        # 上千字的长段；而精排在检索时只读段落前 320 字符（见 config.RERANK_MAX_CHARS），
        # 于是图内文字**根本没参与排序**。实测：206 个含「图片文字」的段，
        # 平均 1265 字、99.5% 超过 320 字符，标记平均出现在第 261 字符处 ——
        # 精排的前 320 字符刚好只够看到标记行，一个字的内容都读不到。
        if line.lstrip().startswith(">"):
            flush()
            quote = []
            while i < n and lines[i].lstrip().startswith(">"):
                quote.append(lines[i])
                i += 1
            t = "\n".join(quote).strip()
            if t:
                blocks.append({"heading_path": cur_path, "kind": "quote", "text": t})
            continue

        # 编号后紧跟一行短标题名 → 合并（`# 01` + `缘起` = `01 缘起`）
        if pending and _is_short_label(line):
            flush()
            pnum, plvl = pending
            pending = None
            commit("%s %s" % (pnum, line.strip()), _num_level(pnum) or plvl)
            i += 1
            continue

        if pending:                          # 没等到标题名，编号自己当标题
            flush()
            settle_pending()

        buf.append(line)
        i += 1

    flush()
    return blocks


def split_markdown(md: str, max_tokens=None, overlap_ratio=None):
    """返回 (chunks, warnings)。chunk = {heading_path, text, token_count}"""
    max_tokens = max_tokens or config.CHUNK_MAX_TOKENS
    if overlap_ratio is None:
        overlap_ratio = config.CHUNK_OVERLAP_RATIO
    overlap = int(max_tokens * overlap_ratio)

    warnings = []
    blocks = _blocks(md)

    # 按「连续相同 heading_path」分组 —— 一个标题段是一个完整论点
    groups = []
    for b in blocks:
        if groups and groups[-1]["path"] == b["heading_path"]:
            groups[-1]["items"].append(b)
        else:
            groups.append({"path": b["heading_path"], "items": [b]})

    chunks = []
    for g in groups:
        segs = []
        for b in g["items"]:
            if b["kind"] in ("code", "table") or _count(b["text"]) <= max_tokens:
                segs.append(b)
            else:
                for piece in _split_oversized(b["text"], max_tokens):
                    segs.append({"heading_path": b["heading_path"],
                                 "kind": "para", "text": piece})

        texts, budget, cur, cur_tok = [], max_tokens, [], 0
        for b in segs:
            tk = _count(b["text"])
            if b["kind"] == "quote":
                # 块引用**强制独占一个 chunk**（不与相邻正文合并）。
                # 只「独立成块」还不够：下面的累积逻辑只要没超 max_tokens 就会
                # 把它和前后正文拼回同一个 chunk，长段问题原样存在。独立成段后
                # 图内文字才谈得上被精排看全、被单独命中。
                if cur:
                    texts.append("\n\n".join(cur))
                    cur, cur_tok = [], 0
                texts.append(b["text"])
                budget = max_tokens - overlap
                continue
            if b["kind"] in ("code", "table") and tk > max_tokens:
                warnings.append(
                    "%s %d token 超上限，按 PRD §7.2 整块保留未切" % (b["kind"], tk))
                if cur:
                    texts.append("\n\n".join(cur))
                    cur, cur_tok = [], 0
                texts.append(b["text"])
                budget = max_tokens - overlap
                continue
            if cur and cur_tok + tk > budget:
                texts.append("\n\n".join(cur))
                budget = max_tokens - overlap
                cur, cur_tok = [], 0
            cur.append(b["text"])
            cur_tok += tk + 2

        if cur:
            texts.append("\n\n".join(cur))

        for k, t in enumerate(texts):
            # 块引用段（`> ` 开头，即 OCR 注入的图内文字）**不带 overlap 前缀**：
            # 精排窗口只有 320 字符，153 字符的前缀会把真正的内容挤出去 ——
            # 而图内文字是图片里的字，它的上文由相邻 chunk 完整保留，不差这点重叠。
            if k == 0 or t.lstrip().startswith(">"):
                final = t
            else:
                final = _tail(texts[k - 1], overlap) + "\n\n" + t
            final = final.strip()
            if final:
                chunks.append({"heading_path": g["path"], "text": final,
                               "token_count": _count(final)})
    return chunks, warnings


# ================================================================ url_canon

_fallback_canon = None
_script_canon = None


def _inline_canon(url: str) -> str:
    """与 wx_fetch.canonical_url 等价的兜底实现（脚本不可用时的回退）。"""
    import urllib.parse

    KEEP = ("__biz", "mid", "idx", "sn")
    p = urllib.parse.urlsplit((url or "").strip())
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
    kept = {k: v for k, v in q if k in KEEP}
    if kept:
        query = urllib.parse.urlencode([(k, kept[k]) for k in KEEP if k in kept])
        return urllib.parse.urlunsplit((p.scheme or "https",
                                        p.netloc or "mp.weixin.qq.com", "/s", query, ""))
    return urllib.parse.urlunsplit((p.scheme or "https",
                                   p.netloc or "mp.weixin.qq.com", p.path, "", ""))


_CANON_SAMPLES = [
    "https://mp.weixin.qq.com/s/abc?scene=1&srcid=xyz#rd",
    "https://mp.weixin.qq.com/s?__biz=MzA3ODk5OTEzOA==&mid=1&idx=1&sn=deadbeef&chksm=xx&scene=2",
    "https://mp.weixin.qq.com/s/plain",
]


def _canon_selfcheck(fn) -> list:
    """对拍：抓取脚本里的 canonical_url 与内置实现结果是否一致。

    两边不一致 = 查重会漏判（PRD §5.2 要求"查重与入库同一实现"）。解耦之后
    两边有各自演化的可能，所以首次加载时验一次，不一致就告警，别等出问题才发现。
    """
    bad = []
    for u in _CANON_SAMPLES:
        try:
            if fn(u) != _inline_canon(u):
                bad.append(u)
        except Exception:
            bad.append(u)
    return bad


def get_canon():
    """取 URL 规范化函数 —— 查重与入库必须同一实现（PRD §5.2）。

    优先加载抓取脚本里的 canonical_url（与 Skill 侧同一份代码，天然一致）；
    找不到脚本（只装了后端、没装 Skill）时退回内置的等价实现。
    候选路径见 config.fetch_script_path()。
    """
    global _script_canon, _fallback_canon
    if _script_canon is None and _fallback_canon is None:
        path = config.fetch_script_path()
        if path is not None:
            try:
                spec = importlib.util.spec_from_file_location("myrag_wx_fetch", str(path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _script_canon = mod.canonical_url
                drift = _canon_selfcheck(_script_canon)
                if drift:
                    print("[canon] 警告：抓取脚本与内置 URL 规范化实现结果不一致，"
                          "查重可能漏判；样例：%s" % "、".join(drift[:2]))
            except Exception as e:
                print("[canon] 抓取脚本加载失败（%s），改用内置实现：%s" % (path, e))
                _script_canon = None
        if _script_canon is None:
            _fallback_canon = _inline_canon
    return _script_canon or _fallback_canon or _inline_canon


# ================================================================ 落盘与索引

def _date_of(published_at):
    """从 ISO8601 取 YYYYMMDD。"""
    if not published_at:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(published_at))
    return "".join(m.groups()) if m else None


def _year_of(date_str):
    return date_str[:4]


def read_stage(stage_token: str):
    if not stage_token:
        return None, None
    sdir = config.STAGE_DIR / stage_token
    pj = sdir / "payload.json"
    if not pj.exists():
        return None, None
    try:
        payload = json.loads(pj.read_text(encoding="utf-8"))
    except Exception:
        return sdir, None
    return sdir, payload


def _rewrite_img_links(md: str, article_id: str, existing: set):
    """把 `![](img/01.png)` 改写成相对 data/ 可解析的 `../../media/<id>/01.png`。

    原文文件位于 data/raw/<年>/<id>.md，`../../media/<id>/NN.ext` 正好指回
    data/media/<id>/NN.ext；同时挂载 /raw 与 /media 后，浏览器也能直接解析。
    """
    def _r(m):
        alt, name = m.group(1), m.group(2)
        base = os.path.basename(name)
        if base not in existing:
            return ""  # 图没下成功，删掉占位
        return "![%s](../../media/%s/%s)" % (alt, article_id, base)

    return IMG_REF_RE.sub(_r, md)


def copy_media(sdir, article_id: str):
    """把 stage/img 复制到 data/media/<id>/，返回 (相对路径列表, 文件名集合)。"""
    src = sdir / "img"
    dst = config.MEDIA_DIR / article_id
    files = []
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.iterdir()):
            if f.is_file():
                shutil.copy2(f, dst / f.name)
                files.append(f.name)
    return ["media/%s/%s" % (article_id, n) for n in files], set(files)


def write_raw(article_id: str, date_str: str, md: str) -> str:
    """写原文（只写不改），返回相对 data/ 的路径。"""
    ydir = config.RAW_DIR / _year_of(date_str)
    ydir.mkdir(parents=True, exist_ok=True)
    p = ydir / ("%s.md" % article_id)
    p.write_text(md, encoding="utf-8")
    return "raw/%s/%s.md" % (_year_of(date_str), article_id)


META_ORDINAL = 0   # 元数据段固定用 ordinal=0，正文分段从 1 起


def _meta_text(row, has_repo_card: bool = False) -> str:
    """把文章元数据拼成可检索文本。

    为什么需要：License / 仓库 / Star 这些字段只存在于 app_cards，正文里没有；
    一级标题也不进正文分段。若只索引正文，PRD 验收 3（搜 Apache-2.0 命中）
    和"按标题搜"都会落空。

    has_repo_card：该分类是否带 GitHub 核查能力（由分类配置决定，不写死分类名）。
    """
    parts = ["标题：%s" % row["title"]]
    if row["source"]:
        parts.append("来源：%s" % row["source"])
    if row["author"]:
        parts.append("作者：%s" % row["author"])
    if row["published_at"]:
        parts.append("发布：%s" % row["published_at"])
    tags = db.jloads(row["tags"], [])
    if tags:
        parts.append("标签：%s" % "、".join(tags))
    if row["summary"]:
        parts.append("摘要：%s" % row["summary"])
    if has_repo_card:
        for label, key in (("仓库", "repo_url"), ("License", "license"),
                           ("语言", "language"), ("Star", "stars"),
                           ("结论", "verdict")):
            v = row[key]
            if v not in (None, ""):
                parts.append("%s：%s" % (label, v))
    return "\n".join(parts)


def upsert_meta_chunk(conn, article_id: str):
    """写入/刷新元数据段（核查后 License 等字段会变，需重建）。"""
    row = conn.execute(
        """SELECT a.title, a.source, a.author, a.published_at, a.tags, a.summary,
                  a.category, c.repo_url, c.license, c.language, c.stars, c.verdict
           FROM articles a LEFT JOIN app_cards c ON c.article_id = a.id
           WHERE a.id = ?""", (article_id,)).fetchone()
    if not row:
        return
    cat = db.category_map(conn).get(row["category"]) or {}
    text = _meta_text(row, "repo_card" in (cat.get("features") or []))

    old = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE article_id = ? AND ordinal = ?",
        (article_id, META_ORDINAL))]
    if old:
        conn.executemany("DELETE FROM chunk_vecs WHERE chunk_id = ?", [(i,) for i in old])
        conn.executemany("DELETE FROM chunk_sparse WHERE chunk_id = ?", [(i,) for i in old])
        conn.execute("DELETE FROM chunks WHERE article_id = ? AND ordinal = ?",
                     (article_id, META_ORDINAL))

    dense, sparse = embed.encode([text])
    cur = conn.execute(
        """INSERT INTO chunks (article_id, ordinal, heading_path, text, token_count)
           VALUES (?,?,?,?,?)""",
        (article_id, META_ORDINAL, "元数据", text, _count(text)))
    cid = cur.lastrowid
    conn.execute("INSERT INTO chunk_vecs (chunk_id, embedding) VALUES (?,?)",
                 (cid, dense[0].astype(np.float32).tobytes()))
    conn.execute(
        "INSERT INTO chunk_sparse (chunk_id, article_id, weights) VALUES (?,?,?)",
        (cid, article_id, db.jdumps(sparse[0])))

    from . import search as search_mod
    search_mod.bump_index()


def chunk_and_index(conn, article_id: str, md: str):
    """切段 → 向量化 → 写 chunks / chunk_vecs / chunk_sparse（调用方事务内）。

    正文分段用 ordinal 1..n，元数据段由 upsert_meta_chunk 单独写 ordinal=0。
    """
    chunks, warnings = split_markdown(md)
    if not chunks:
        t = (md or "").strip()
        chunks = [{"heading_path": "", "text": t, "token_count": _count(t)}]

    old = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE article_id = ? AND ordinal > ?",
        (article_id, META_ORDINAL))]
    if old:
        conn.executemany("DELETE FROM chunk_vecs WHERE chunk_id = ?", [(i,) for i in old])
        conn.executemany("DELETE FROM chunk_sparse WHERE chunk_id = ?", [(i,) for i in old])
        conn.execute("DELETE FROM chunks WHERE article_id = ? AND ordinal > ?",
                     (article_id, META_ORDINAL))

    dense, sparse = embed.encode([c["text"] for c in chunks])

    for i, c in enumerate(chunks):
        cur = conn.execute(
            """INSERT INTO chunks (article_id, ordinal, heading_path, text, token_count)
               VALUES (?,?,?,?,?)""",
            (article_id, i + 1, c["heading_path"], c["text"], c["token_count"]))
        cid = cur.lastrowid
        conn.execute("INSERT INTO chunk_vecs (chunk_id, embedding) VALUES (?,?)",
                     (cid, dense[i].astype(np.float32).tobytes()))
        conn.execute(
            "INSERT INTO chunk_sparse (chunk_id, article_id, weights) VALUES (?,?,?)",
            (cid, article_id, db.jdumps(sparse[i])))

    from . import search as search_mod
    search_mod.bump_index()

    return {"chunks": len(chunks), "warnings": warnings,
            "tokens": sum(c["token_count"] for c in chunks)}


# ================================================================ 主流程

def _default_review_due():
    return (datetime.now(CST) + timedelta(days=config.REVIEW_DUE_DAYS)).isoformat(
        timespec="seconds")


def do_ingest(conn, req: dict) -> dict:
    """POST /api/ingest 的全部逻辑（不含 HTTP）。"""
    stage_token = (req.get("stage_token") or "").strip()

    # 幂等 ①：同一个 stage_token 已 ingest 过（stage 目录已被清理，不能靠它判断）
    row = db.find_by_stage_token(conn, stage_token)
    if row:
        return {"ok": True, "dup": True, "id": row["article_id"], "fail_reason": None,
                "detail": "stage_token 已入库（幂等，未重复写）"}

    sdir, payload = read_stage(stage_token)
    if payload is None:
        return {"ok": False, "dup": False, "id": None, "fail_reason": "stage_not_found",
                "detail": "stage 目录或 payload.json 不存在: %s" % stage_token}

    url_canon = payload.get("url_canon") or ""
    url = req.get("url") or payload.get("url") or ""

    # 幂等 ②：url_canon 命中（PRD §9：HTTP 200，不返 409）
    dup = db.find_by_canon(conn, url_canon)
    if dup:
        db.log_ingest(conn, url_canon, dup["id"], "skip_dup", {"stage_token": stage_token})
        return {"ok": True, "dup": True, "id": dup["id"], "fail_reason": None,
                "detail": "url_canon 已存在"}

    cats = db.category_map(conn)
    if not cats:                       # 极端情况：categories 表为空（未播种）
        db.seed_categories(conn)
        cats = db.category_map(conn)

    category = (req.get("category") or "").strip()
    if category not in cats:           # 判不准 → 落默认分类（PRD §5.4）
        category = (db.default_category(conn) or {}).get("key") or category
    cat = cats.get(category) or {}
    features = cat.get("features") or []
    retrieval = cat.get("retrieval") or "rag"

    title = req.get("title") or payload.get("title") or "(无标题)"
    source = req.get("source") or payload.get("source")
    author = req.get("author") or payload.get("author")
    published_at = req.get("published_at") or payload.get("published_at")
    content_md = payload.get("content_md") or ""

    # source 为空 = 抓取降级，必须进待复核（PRD §6.2 / §15）
    want_flag = int(req.get("review_flag") or 0)
    review_flag = 1 if (want_flag == 1 or not source) else 0

    date_str = _date_of(published_at) or datetime.now(CST).strftime("%Y%m%d")
    article_id = db.allocate_id(conn, cat.get("prefix") or "X", date_str)
    now = db.now_iso()

    # 配图先归位，正文图链才知道哪些图真的存在
    attachments, files = copy_media(sdir, article_id)
    md = _rewrite_img_links(content_md, article_id, files)
    raw_path = write_raw(article_id, date_str, md)

    review_due = None
    if "repo_card" in features:
        review_due = req.get("review_due") or _default_review_due()

    conn.execute(
        """INSERT INTO articles
           (id, category, title, source, author, published_at, collected_at, url, url_canon,
            summary, tags, raw_path, attachments, review_flag, review_due, status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'ok', ?, ?)""",
        (article_id, category, title, source, author, published_at, now, url, url_canon,
         req.get("summary") or "", db.jdumps(req.get("tags") or []), raw_path,
         db.jdumps(attachments), review_flag, review_due, now, now))

    if "repo_card" in features:
        ac = req.get("app_card") or {}
        conn.execute(
            """INSERT OR REPLACE INTO app_cards
               (article_id, repo_url, license, language, stars, last_commit, last_release,
                created_date, boundary, deploy_note, verdict, evidence, verified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (article_id, ac.get("repo_url"), ac.get("license"), ac.get("language"),
             ac.get("stars"), ac.get("last_commit"), ac.get("last_release"),
             ac.get("created_date"), ac.get("boundary"), ac.get("deploy_note"),
             ac.get("verdict"), db.jdumps(ac.get("evidence") or {}),
             ac.get("verified_at")))

    if "concepts" in features:
        for c in (req.get("concepts") or []):
            name = (c.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                """INSERT INTO concepts (name, definition, article_id, related)
                   VALUES (?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     definition = COALESCE(excluded.definition, concepts.definition),
                     article_id = excluded.article_id""",
                (name, c.get("definition"), article_id,
                 db.jdumps(c.get("related") or [])))

    # 切段+索引只对 retrieval=rag 的分类做；fulltext 类只存全文（PRD §5.5）。
    # 元数据段必须在 app_cards 之后写，否则拿不到 License 等字段。
    if retrieval == "rag":
        idx = chunk_and_index(conn, article_id, md)
        upsert_meta_chunk(conn, article_id)
    else:
        idx = {"chunks": 0, "warnings": [], "tokens": 0}

    db.log_ingest(conn, url_canon, article_id, "ingest",
                  {"stage_token": stage_token, "chars": len(md),
                   "chunks": idx["chunks"], "warnings": idx["warnings"]})

    # 成功才清理 stage；失败保留便于重试（PRD §9）
    try:
        if sdir and sdir.exists():
            shutil.rmtree(sdir)
    except Exception:
        pass

    return {"ok": True, "dup": False, "id": article_id, "fail_reason": None,
            "chunks": idx["chunks"], "chars": len(md), "attachments": len(attachments),
            "warnings": idx["warnings"]}


def do_ingest_text(conn, req: dict) -> dict:
    """POST /api/ingest-text 辅路径：不抓取、不判类、summary 留空（PRD §9 / §10.3）。"""
    title = (req.get("title") or "").strip() or "(无标题)"
    md = (req.get("raw_content") or "").strip()
    if not md:
        return {"ok": False, "dup": False, "id": None, "fail_reason": "empty_content"}
    if len(md) > config.MAX_INGEST_TEXT_CHARS:
        return {"ok": False, "dup": False, "id": None, "fail_reason": "too_large"}

    cats = db.category_map(conn)
    if not cats:
        db.seed_categories(conn)
        cats = db.category_map(conn)

    category = req.get("category")
    review_flag = 0
    if category not in cats:               # 不确定 → 默认分类 + 待复核（PRD §5.4）
        category = (db.default_category(conn) or {}).get("key")
        review_flag = 1
    cat = cats.get(category) or {}
    features = cat.get("features") or []
    retrieval = cat.get("retrieval") or "rag"

    url = (req.get("url") or "").strip()
    now = db.now_iso()
    if url:
        url_canon = get_canon()(url)
        dup = db.find_by_canon(conn, url_canon)
        if dup:
            db.log_ingest(conn, url_canon, dup["id"], "skip_dup", {"via": "ingest-text"})
            return {"ok": True, "dup": True, "id": dup["id"], "fail_reason": None,
                    "detail": "url_canon 已存在"}
    else:
        # 无链接的手工补录：用 title + 时间生成占位 key
        import hashlib

        url_canon = "myrag://text/" + hashlib.sha1(
            (title + now).encode("utf-8")).hexdigest()[:16]

    published_at = req.get("published_at")
    date_str = _date_of(published_at) or datetime.now(CST).strftime("%Y%m%d")
    article_id = db.allocate_id(conn, cat.get("prefix") or "X", date_str)

    md = md if md.lstrip().startswith("#") else "# %s\n\n%s" % (title, md)
    raw_path = write_raw(article_id, date_str, md)

    review_due = _default_review_due() if "repo_card" in features else None

    conn.execute(
        """INSERT INTO articles
           (id, category, title, source, author, published_at, collected_at, url, url_canon,
            summary, tags, raw_path, attachments, review_flag, review_due, status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'ok', ?, ?)""",
        (article_id, category, title, req.get("source"), None, published_at, now,
         url, url_canon, "", "[]", raw_path, "[]", review_flag, review_due, now, now))

    if retrieval == "rag":
        idx = chunk_and_index(conn, article_id, md)
        upsert_meta_chunk(conn, article_id)
    else:
        idx = {"chunks": 0, "warnings": [], "tokens": 0}
    db.log_ingest(conn, url_canon, article_id, "ingest",
                  {"via": "ingest-text", "chunks": idx["chunks"]})

    return {"ok": True, "dup": False, "id": article_id, "fail_reason": None,
            "chunks": idx["chunks"], "chars": len(md),
            "review_flag": review_flag, "warnings": idx["warnings"]}


def delete_article(conn, article_id: str) -> dict:
    """删库 + 清原文与配图（PRD §9）。"""
    row = conn.execute("SELECT raw_path FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        return {"ok": False, "fail_reason": "not_found"}
    removed = []
    rp = row["raw_path"]
    if rp:
        p = config.DATA_DIR / rp
        if p.exists():
            p.unlink()
            removed.append(rp)
    mdir = config.MEDIA_DIR / article_id
    if mdir.is_dir():
        shutil.rmtree(mdir)
        removed.append("media/%s/" % article_id)

    # chunk_vecs / chunk_sparse 没有外键，ON DELETE CASCADE 清不到它们，
    # 必须显式删；否则留下孤儿向量，会被 KNN 召回又 JOIN 不到文章。
    cids = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE article_id = ?", (article_id,))]
    if cids:
        conn.executemany("DELETE FROM chunk_vecs WHERE chunk_id = ?", [(i,) for i in cids])
        conn.executemany("DELETE FROM chunk_sparse WHERE chunk_id = ?", [(i,) for i in cids])
    conn.execute("DELETE FROM chunks WHERE article_id = ?", (article_id,))
    conn.execute("DELETE FROM chunk_sparse WHERE article_id = ?", (article_id,))

    conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    db.log_ingest(conn, None, article_id, "delete", {"removed": removed})
    from . import search as search_mod
    search_mod.bump_index()
    return {"ok": True, "id": article_id, "removed": removed}


def purge_orphans(conn) -> dict:
    """清理孤儿向量行（历史遗留或异常中断产生）。启动时跑一次，自愈。"""
    n_vec = conn.execute(
        "DELETE FROM chunk_vecs WHERE chunk_id NOT IN (SELECT id FROM chunks)"
    ).rowcount
    n_sp = conn.execute(
        "DELETE FROM chunk_sparse WHERE chunk_id NOT IN (SELECT id FROM chunks)"
    ).rowcount
    if n_vec or n_sp:
        from . import search as search_mod
        search_mod.bump_index()
    return {"vec": n_vec, "sparse": n_sp}
