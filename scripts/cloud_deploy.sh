#!/usr/bin/env bash
# ============================================================
# MyRag 云端部署（cloud_deploy.sh）
#
# 在服务器上完成「代码与数据之外」的剩余工作：
#   预检环境 → 写 .env（口令 + 硅基流动 Key）→ 装 systemd → 启动 → 健康检查
#
# 使用顺序：先 `cloud_push.sh --init`（推代码数据+装依赖），再跑本脚本。
#
# 用法：
#   bash scripts/cloud_deploy.sh             # 部署
#   bash scripts/cloud_deploy.sh --dry-run   # 只打印命令不执行
#   bash scripts/cloud_deploy.sh --force     # 服务器已有 .env 时覆盖重写
#
# 配置来源（与 cloud_push.sh 同一套）：
#   cloud/push.conf    服务器地址（MYRAG_CLOUD_PUSH_HOST / DIR / SSH 参数）
#   cloud/remote.env   访问口令（MYRAG_CLOUD_PASSWORD；没有则现场随机生成并打印）
#   本地 .env          硅基流动 Key（SILICONFLOW_API_KEY，从本机读出写到服务器；
#                      **Key 只在 ssh stdin 里走一道，不进本仓库、不进命令行参数**）
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$ROOT/cloud/push.conf"
[ -f "$CONF" ] && . "$CONF"
ENVF="$ROOT/cloud/remote.env"
[ -f "$ENVF" ] && . "$ENVF"

HOST="${MYRAG_CLOUD_PUSH_HOST:-}"
DIR="${MYRAG_CLOUD_PUSH_DIR:-/opt/myrag}"
SSH_ARGS="${MYRAG_CLOUD_PUSH_SSH:-}"

FORCE=0; DRY=0
for a in "$@"; do
  case "$a" in
    --force)   FORCE=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done
[ -n "$HOST" ] || { echo "缺 MYRAG_CLOUD_PUSH_HOST（见 cloud/push.conf）"; exit 2; }

rsh()  { if [ "$DRY" = 1 ]; then echo "[dry] ssh $SSH_ARGS $HOST \"$*\""; else ssh $SSH_ARGS "$HOST" "$*"; fi; }
rpush(){ if [ "$DRY" = 1 ]; then echo "[dry] rsync $*"; else rsync "$@"; fi; }

# ---- 口令与 Key ------------------------------------------------------------
PASSWORD="${MYRAG_CLOUD_PASSWORD:-}"
GENPW=0
if [ -z "$PASSWORD" ]; then
  PASSWORD="MyRag-$(openssl rand -hex 5)"
  GENPW=1
fi
APIKEY="$(grep -E '^SILICONFLOW_API_KEY=' "$ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
[ -n "$APIKEY" ] || echo "⚠️ 本地 .env 里没有 SILICONFLOW_API_KEY：云端搜索将走关键词降级模式"

# ---- 1/4 预检 ---------------------------------------------------------------
echo "== 0/4 初始化目录归属（幂等：/opt/myrag 归部署用户所有，后续推送免 sudo）"
rsh "sudo mkdir -p $DIR && sudo chown -R \$(whoami):\$(whoami) $DIR && echo '目录就绪'"

echo "== 1/4 服务器预检（Python / venv / 依赖 / 端口 / 磁盘）"
rsh "python3 --version"
rsh "python3 -m venv --help >/dev/null 2>&1 && echo 'venv 模块 OK' \
     || echo '⚠️ 缺 python3-venv：apt install -y python3-venv 后重跑'"
rsh "test -x $DIR/.venv/bin/uvicorn && echo '依赖已装（.venv 就绪）' \
     || echo '⚠️ $DIR/.venv 不就绪：先跑 cloud_push.sh --init'"
rsh "(ss -tlnp 2>/dev/null | grep -q :8766 && echo '⚠️ 端口 8766 已被占用！') || echo '端口 8766 空闲'"
rsh "df -h $DIR 2>/dev/null | tail -1 || echo '⚠️ 目录 $DIR 不存在：先跑 cloud_push.sh'"
rsh "grep -qE 'MemAvailable:[[:space:]]*[0-9]{4,}' /proc/meminfo && echo '内存余量尚可' \
     || echo '⚠️ 可用内存 <1G，注意同机其他系统的水位'"

# ---- 2/4 .env ---------------------------------------------------------------
echo "== 2/4 写服务器 .env（口令 + 向量服务 Key）"
if [ "$DRY" = 0 ]; then
  TMPENV="$(mktemp)"
  trap 'rm -f "$TMPENV"' EXIT
  {
    echo "# 由 scripts/cloud_deploy.sh 生成于 $(date '+%F %T')"
    echo "MYRAG_CLOUD_PASSWORD=$PASSWORD"
    echo "SILICONFLOW_API_KEY=$APIKEY"
  } > "$TMPENV"
  if [ "$FORCE" = 0 ] && ssh $SSH_ARGS "$HOST" "test -f $DIR/.env" 2>/dev/null; then
    echo "⚠️ 服务器已有 $DIR/.env（不动它）。要覆盖请加 --force"
  else
    ssh $SSH_ARGS "$HOST" "cat > $DIR/.env && chmod 600 $DIR/.env" < "$TMPENV"
    echo "已写入（权限 600）"
  fi
else
  echo "[dry] ssh … \"cat > $DIR/.env\"  < 临时文件(口令+Key，内容不回显)"
fi

# ---- 3/4 systemd ------------------------------------------------------------
echo "== 3/4 安装并启动 systemd 服务（myrag-cloud，需 sudo 装单元文件）"
rpush -az -e "ssh $SSH_ARGS" "$ROOT/cloud/myrag-cloud.service" \
      "$HOST:$DIR/myrag-cloud.service"
rsh "sudo install -m 644 $DIR/myrag-cloud.service /etc/systemd/system/myrag-cloud.service \
     && sudo systemctl daemon-reload && sudo systemctl enable --now myrag-cloud"

# ---- 4/4 健康检查 -----------------------------------------------------------
echo "== 4/4 健康检查（等 3 秒让服务起稳）"
rsh "sleep 3 && curl -s http://127.0.0.1:8766/api/health | head -c 200 \
     && echo || (systemctl status myrag-cloud --no-pager | head -15 && exit 1)"

echo ""
echo "=========================================="
echo "部署完成。访问地址：http://$(echo "$HOST" | cut -d@ -f2):8766"
if [ "$GENPW" = 1 ]; then
  echo "访问口令（随机生成，请记下/发给朋友）：$PASSWORD"
else
  echo "访问口令：你在 cloud/remote.env 里配的那个"
fi
echo "改口令：改 cloud/remote.env 重跑本脚本 --force（旧 Cookie 立即全体失效）"
echo "=========================================="
