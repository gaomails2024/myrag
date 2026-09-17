#!/usr/bin/env python3
"""下载 reranker 权重（PRD §8.4）。

**为什么单独一个脚本、而不是让后端自动下载**：

reranker 是**增强项**——没有它，检索照常工作（只是少了精排）。而权重可能有 2GB
级、下载要几分钟。这两点加起来意味着：它的下载**绝不能发生在检索请求路径上**，
否则使用者搜一次就要挂住等下载（实测踩过：一次 curl 直接超时）。

对比：embedding 模型（BGE-M3）可以联网兜底，因为没有它整个系统不可用——性质不同。

    .venv/bin/python scripts/fetch_rerank.py            # 下载（已存在则跳过）
    .venv/bin/python scripts/fetch_rerank.py --check    # 只检查状态，不下载
    .venv/bin/python scripts/fetch_rerank.py --off      # 关闭精排（写进 .env）
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server import config  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="reranker 权重管理")
    ap.add_argument("--check", action="store_true", help="只检查，不下载")
    ap.add_argument("--off", action="store_true", help="关闭精排（写 .env）")
    args = ap.parse_args()

    if args.off:
        env = config.ROOT / ".env"
        text = env.read_text(encoding="utf-8") if env.exists() else ""
        if "MYRAG_RERANK=" in text:
            print("MYRAG_RERANK 已在 .env 里，请手动改那一行")
            return
        env.write_text(text.rstrip("\n") + "\nMYRAG_RERANK=0\n", encoding="utf-8")
        print("已写入 .env：MYRAG_RERANK=0（重启后端生效；检索将跳过精排）")
        return

    try:
        path = config.resolve_rerank_path(allow_download=False)
        size = sum(f.stat().st_size for f in pathlib.Path(path).rglob("*") if f.is_file())
        print("✓ 权重已在本地：%s（%.0f MB）" % (path, size / 1048576))
        print("  后端重启后自动启用精排（config.RERANK_ENABLED=%s）" % config.RERANK_ENABLED)
        return
    except RuntimeError:
        pass

    if args.check:
        print("✗ 权重不在本地")
        print("  下载：.venv/bin/python scripts/fetch_rerank.py")
        print("  不下载也没关系：检索会跳过精排，其余功能不受影响")
        return

    print("开始下载 %s（数百 MB 到 2GB 级，视网络可能几分钟）…" % config.RERANK_MODEL_ID)
    path = config.resolve_rerank_path(allow_download=True)
    size = sum(f.stat().st_size for f in pathlib.Path(path).rglob("*") if f.is_file())
    print("✓ 下载完成：%s（%.0f MB）" % (path, size / 1048576))
    print("  重启后端即启用精排：bash scripts/stop.sh && bash scripts/start.sh")


if __name__ == "__main__":
    main()
