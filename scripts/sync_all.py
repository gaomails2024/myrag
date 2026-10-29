#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键全量同步：主题树 + Wiki 概念层 + 织网（把散在各脚本里的维护动作串成一条流水线）。

用法：
    .venv/bin/python scripts/sync_all.py

流水线（顺序有讲究：先落树再编译，概念页的「所属话题」才不会返工）：

    [0-token]
    1. wiki_build.py sync                  画像向量 → 成员归属 → 标记待更新
    2. wiki_build.py rebind --before       apply 前记下「wiki 页 → 话题名」
    3. structure.py mount                  重算文章 × 话题挂载（读 catalog.json）
    4. structure.py apply                  落库 themes / article_themes（自动备份）
    5. wiki_build.py rebind --after        apply 重建了 themes，按名字把 wiki 页挂回
    6. wiki_build.py concepts attach       概念挂回话题（纯向量）
    [闸门]
    7. 查库统计待编译页数 → 打印 token 估算 → 等确认（输入 yes 才继续）
    [花 token]
    8. concepts compile --pending-only     只重编译真有新成员的页
    9. concepts link                       重织概念网互链（分批小调用）
    [收尾]
    10. 落盘自检：pending 是否清零、互链数、话题关联，当场查库验证，不报假成功

为什么是独立编排脚本而不是塞进某个现有脚本：它编排的是 structure.py 与
wiki_build.py 两个入口的 9 个子命令，塞进任何一边都会让那边的职责变糊；
子命令全部幂等，本脚本任何一步失败直接停，重跑即从断点处自然续上。

词云不进流水线：/api/tags 是实时统计，天然最新，没有「同步」这个概念。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WIKI = ROOT / "scripts" / "wiki_build.py"
STRUCT = ROOT / "scripts" / "structure.py"


def run(step: str, args: list[str]) -> None:
    """跑一个子命令，输出实时透传；失败即停（不继续下一步，也不回滚已完成步骤）。"""
    print("\n" + "=" * 68)
    print("▶ %s" % step)
    print("=" * 68)
    sys.stdout.flush()  # 不 flush 的话管道下标题会滞后于子进程输出，看起来像乱序
    try:
        rc = subprocess.run([sys.executable, "-u", *args]).returncode
    except KeyboardInterrupt:
        print("\n\n⚠️ 已中断。已完成的前几步不受影响（每步自己管事务/备份），"
              "\n  重跑本命令会跳过已完成的部分继续。")
        raise SystemExit(130)
    if rc != 0:
        print("\n❌ 「%s」失败（退出码 %d），流水线已停。" % (step, rc))
        print("   已完成的步骤不受影响；排查后重跑本命令即可。")
        raise SystemExit(rc)


def pending_pages() -> list[dict]:
    """待编译页：与 wiki_build.py sync 的口径一致（pending>0 且已编译过）。"""
    from server import db
    db.init_db()
    with db.conn_ctx() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT name, pending, article_count FROM wiki_concepts
               WHERE pending > 0 AND compiled_at IS NOT NULL
               ORDER BY pending DESC, article_count DESC""")]


def final_check() -> int:
    """落盘自检：当场查库验证，不报假成功（PROJECT.md ㉛ 的教训）。"""
    from server import db
    with db.conn_ctx() as conn:
        pend = conn.execute(
            "SELECT COUNT(*) n FROM wiki_concepts WHERE pending > 0 "
            "AND compiled_at IS NOT NULL").fetchone()["n"]
        pages = conn.execute(
            "SELECT COUNT(*) n FROM wiki_pages WHERE slug LIKE 'c-%'").fetchone()["n"]
        # 概念互链存在 wiki_links（概念页也是 wiki_pages 行，src=页 id，dst=概念 slug），
        # 无向边每条存两次，除 2 还原边数；只数 概念→概念（→话题的幻觉边不该存在，
        # link() 织网前会清，这里顺手盯住）
        links = conn.execute(
            """SELECT COUNT(*) n FROM wiki_links wl
               JOIN wiki_pages p ON wl.src_page_id = p.id
               WHERE p.slug LIKE 'c-%' AND wl.dst_slug LIKE 'c-%'""").fetchone()["n"] // 2
        halluc = conn.execute(
            """SELECT COUNT(*) n FROM wiki_links wl
               JOIN wiki_pages p ON wl.src_page_id = p.id
               WHERE p.slug LIKE 'c-%' AND wl.dst_slug NOT LIKE 'c-%'""").fetchone()["n"]
        # 概念的「所属话题」存在 wiki_concepts.topic_id（app.py ㉚ 设计），
        # wiki_pages.topic_id 在概念页上是无人消费的列，不作为判据
        bound = conn.execute(
            """SELECT COUNT(*) n FROM wiki_concepts c
               JOIN themes t ON c.topic_id = t.id WHERE c.compiled_at IS NOT NULL""").fetchone()["n"]
        unbound = conn.execute(
            "SELECT slug FROM wiki_concepts WHERE topic_id IS NULL").fetchall()
    print("\n" + "=" * 68)
    print("✅ 全量同步完成，自检结果：")
    print("=" * 68)
    print("  已编译概念页 %d 个 · 待更新剩余 %d 个" % (pages, pend))
    print("  概念互链 %d 条（概念↔概念）" % links)
    if halluc:
        print("  ⚠️ %d 条概念页→话题的边（模型幻觉，link() 应已清理——出现即为回归）" % halluc)
    print("  话题关联：%d/%d 个概念页挂在主题树上" % (bound, pages))
    if pend:
        print("  ⚠️ 还有 %d 页待更新（可能是编译时个别页失败），"
              "重跑本命令或 concepts compile --pending-only --force 补" % pend)
    if unbound:
        print("  ⚠️ %d 个概念没挂上任何话题（ attach 时相似度都不达标，可接受）"
              % len(unbound))
    return 1 if pend else 0


def main() -> int:
    if not (WIKI.is_file() and STRUCT.is_file()):
        print("找不到 wiki_build.py / structure.py —— 请在项目根目录运行", file=sys.stderr)
        return 2

    # ---------- 0 token：能自动跑的全跑完 ----------
    run("1/6 Wiki 同步（画像 · 归属 · 标记待更新）", [str(WIKI), "sync"])
    run("2/6 记录 wiki 页话题名（apply 前）", [str(WIKI), "rebind", "--before"])
    run("3/6 主题树挂载", [str(STRUCT), "mount"])
    run("4/6 主题树落库（自动备份）", [str(STRUCT), "apply"])
    run("5/6 wiki 页挂回话题（apply 后）", [str(WIKI), "rebind", "--after"])
    run("6/6 概念挂回话题", [str(WIKI), "concepts", "attach"])

    # ---------- 闸门：花 token 之前必须确认 ----------
    pend = pending_pages()
    if not pend:
        print("\n✅ 没有待编译的页面，全量同步到此完成，本次没花任何 token。")
        return 0

    from server import llm
    reason = llm.missing_reason()
    if reason:
        print("\n" + "-" * 68)
        print("0 token 步骤全部完成，但 LLM 未配置，编译没法跑：\n%s" % reason)
        print("配置好 .env 后重跑本命令即可（0 token 步骤会很快跳过）。")
        return 0

    n = len(pend)
    print("\n" + "-" * 68)
    print("待编译 %d 个概念页，前 12 个：" % n)
    for d in pend[:12]:
        print("  %-22s +%d 篇新成员（共 %d 篇在谈）"
              % (d["name"][:20], d["pending"], d["article_count"]))
    if n > 12:
        print("  … 另有 %d 个" % (n - 12))
    print("\n这一步要花 token：约 %d 次调用 × 约 2 万 token ≈ %d 万 input token，"
          "耗时约 %d 分钟" % (n, n * 2, max(1, n * 12 // 60)))
    print("-" * 68)
    try:
        ans = input("继续花 token 重编译？输入 yes 继续，其他任意键停下：").strip().lower()
    except (KeyboardInterrupt, EOFError):
        # 管道运行（echo yes | …）时 stdin 关闭会抛 EOFError：没确认就停，绝不默认继续
        ans = ""
    if ans not in ("yes", "y"):
        print("\n已停下。0 token 步骤都已完成（主题树与成员关系已更新），"
              "\n页面还是旧内容；随时重跑本命令，会直接到这里继续。")
        return 0

    # ---------- 花 token ----------
    run("编译待更新页", [str(WIKI), "concepts", "compile", "--pending-only", "--force"])
    run("织网（概念互链）", [str(WIKI), "concepts", "link"])
    return final_check()


if __name__ == "__main__":
    raise SystemExit(main())
