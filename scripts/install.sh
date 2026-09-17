#!/usr/bin/env bash
# ================================================================
# MyRag 安装脚本
#
#   bash scripts/install.sh                      # 装依赖 + 部署 Skill
#   bash scripts/install.sh --skill-only         # 只部署 Skill（依赖已装好）
#   bash scripts/install.sh --skill-dir <路径>    # 指定客户端 skills 目录
#
# 做四件事：
#   1. 检查 Python 版本（需要 3.11+）
#   2. 建 .venv 并安装依赖
#   3. 把仓库 skill/ 部署到 Agent 客户端的 skills 目录，
#      并把项目路径写进 SKILL.md（模板里的 {{MYRAG_HOME}} 占位符）
#   4. 生成 .env（已存在则不动）
#
# 幂等：可重复执行；覆盖 SKILL.md 前会先备份。
# ================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_SRC="$ROOT/skill"
SKILL_DST="${HOME}/.workbuddy/skills/MyRag"
DO_DEPS=1

usage() {
  cat <<'USAGE'
用法：bash scripts/install.sh [选项]

  --skill-only        只部署 Skill，不装依赖
  --no-deps           同上
  --skill-dir PATH    指定客户端 skills 目录（默认 ~/.workbuddy/skills/MyRag）
  -h, --help          显示本帮助
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --skill-only|--no-deps) DO_DEPS=0 ;;
    --skill-dir) SKILL_DST="${2:-}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

step() { printf '\n== %s ==\n' "$1"; }
ok()   { printf '   [ok] %s\n' "$1"; }
warn() { printf '   [ !] %s\n' "$1"; }
die()  { printf '   [ERR] %s\n' "$1" >&2; exit 1; }

echo "MyRag 安装"
echo "  项目根：$ROOT"
echo "  Skill 目标：$SKILL_DST"

# ---------------------------------------------------------------- 1. Python
step "1/4 检查 Python"
PY_BIN=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
    PY_BIN="$(command -v "$cand")"
    break
  fi
done
if [ -z "$PY_BIN" ]; then
  die "没找到 Python 3.11+。macOS 可执行：brew install python@3.12"
fi
ok "使用 $PY_BIN （$("$PY_BIN" -V 2>&1)）"

# ---------------------------------------------------------------- 2. 依赖
if [ "$DO_DEPS" = "1" ]; then
  step "2/4 创建虚拟环境并安装依赖"
  if [ -x "$ROOT/.venv/bin/python" ]; then
    ok ".venv 已存在，跳过创建"
  else
    "$PY_BIN" -m venv "$ROOT/.venv" || die "创建 .venv 失败"
    ok "已创建 .venv"
  fi
  "$ROOT/.venv/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
  warn "即将安装 torch 等依赖，首次会下载数百 MB，请耐心等待"
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt" || die "依赖安装失败"
  ok "依赖安装完成"
else
  step "2/4 跳过依赖安装（--skill-only）"
fi

# ---------------------------------------------------------------- 3. Skill
step "3/4 部署 Skill 到客户端目录"
[ -d "$SKILL_SRC" ] || die "仓库里没有 skill/ 目录，无法部署"
mkdir -p "$SKILL_DST" || die "无法创建 $SKILL_DST"
if [ -f "$SKILL_DST/SKILL.md" ]; then
  cp "$SKILL_DST/SKILL.md" "$SKILL_DST/SKILL.md.bak-$(date +%Y%m%d%H%M%S)"
  warn "目标已有 SKILL.md，已备份为 .bak-<时间戳>"
fi
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude '__pycache__' --exclude '.DS_Store' "$SKILL_SRC/" "$SKILL_DST/" || die "拷贝失败"
else
  cp -R "$SKILL_SRC/." "$SKILL_DST/" || die "拷贝失败"
fi
ok "Skill 文件已就位"

"$PY_BIN" - "$SKILL_DST/SKILL.md" "$ROOT" <<'PY'
import pathlib, sys
path, home = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8")
if "{{MYRAG_HOME}}" not in text:
    print("   [ !] 没找到占位符（可能已替换过），跳过")
    raise SystemExit(0)
path.write_text(text.replace("{{MYRAG_HOME}}", home), encoding="utf-8")
print("   [ok] 已把项目路径写入 SKILL.md：%s" % home)
PY
[ $? -eq 0 ] || die "占位符替换失败"

# ---------------------------------------------------------------- 4. .env
step "4/4 生成配置文件"
if [ -f "$ROOT/.env" ]; then
  ok ".env 已存在，未改动"
else
  cp "$ROOT/.env.example" "$ROOT/.env"
  ok "已由 .env.example 生成 .env（默认 127.0.0.1:8765）"
fi

cat <<EOF

安装完成。下一步：

  1) 启动服务（按需启动，不会开机自启）
       bash "$ROOT/scripts/start.sh"
     首次启动会自动下载 BGE-M3 权重（约 2-4 GB，需要能连 ModelScope）

  2) 打开工作台
       http://127.0.0.1:8765/

  3) 入库内容
       · 主路径：在微信里转发文章/视频号给 WorkBuddy，说 "MyRag"
       · 备用：直接把一批链接粘给 WorkBuddy，说 "MyRag"

注意：视频号转写依赖第三方 Skill video-transcript，
      需单独安装并扫码登录元宝，详见 README 的「视频号」一节。
EOF
