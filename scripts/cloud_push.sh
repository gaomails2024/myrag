#!/usr/bin/env bash
# ============================================================
# MyRag 云端推送（cloud_push.sh）
#
# 把本机知识库的「一致性快照」+ 云端版代码推到云服务器。
# **手动跑，没有任何自动定时**——想让朋友看到新文章时跑一次。
#
# 用法：
#   bash scripts/cloud_push.sh             # 全量：代码 + 数据库快照 + raw/media（增量）
#   bash scripts/cloud_push.sh --db-only   # 只更新数据库快照（日常用这个，最快）
#   bash scripts/cloud_push.sh --init      # 附带云端首次安装（venv + pip 装依赖）
#   bash scripts/cloud_push.sh --prune     # media/raw 加 --delete（本地删过的云端也删；
#                                          #   默认不加，宁多勿丢）
#   bash scripts/cloud_push.sh --dry-run   # 只打印命令不执行（首次配置后先跑这个）
#
# 配置：写进 cloud/push.conf（含主机名，不进 git；.gitignore 已挡）：
#   MYRAG_CLOUD_PUSH_HOST=root@1.2.3.4     # 必填
#   MYRAG_CLOUD_PUSH_DIR=/opt/myrag        # 云端项目目录，默认 /opt/myrag
#   MYRAG_CLOUD_PUSH_SSH="-p 2222"         # ssh/rsync 附加参数，默认空
#
# 推什么、不推什么（红线）：
#   推：cloud/、server/、web/index.html、wxk.db 快照、data/raw/、data/media/
#   不推：本地 .env（里面有你的 LLM Key，**永不出本机**）；云端 .env 在服务器上
#        手工维护（参考 cloud/env.example：口令 + 硅基流动 Key）
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$ROOT/cloud/push.conf"
[ -f "$CONF" ] && . "$CONF"

HOST="${MYRAG_CLOUD_PUSH_HOST:-}"
DIR="${MYRAG_CLOUD_PUSH_DIR:-/opt/myrag}"
SSH_ARGS="${MYRAG_CLOUD_PUSH_SSH:-}"

DB_ONLY=0; INIT=0; PRUNE=0; DRY=0
for a in "$@"; do
  case "$a" in
    --db-only) DB_ONLY=1 ;;
    --init)    INIT=1 ;;
    --prune)   PRUNE=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "未知参数：$a（见文件头用法）"; exit 2 ;;
  esac
done

if [ -z "$HOST" ]; then
  echo "缺 MYRAG_CLOUD_PUSH_HOST。建一个 cloud/push.conf 写上："
  echo "  MYRAG_CLOUD_PUSH_HOST=root@你的服务器IP"
  exit 2
fi

# --dry-run 时 ssh/rsync 只打印不执行；数据库快照这步**照真做**——它同时是
# 本地备份（进 data/_backup/），无破坏性，且让 dry-run 的 rsync 命令有真实文件可指。
run()  { if [ "$DRY" = 1 ]; then echo "[dry] $*"; else "$@"; fi; }
rsh()  { if [ "$DRY" = 1 ]; then echo "[dry] ssh $SSH_ARGS $HOST \"$*\""; else ssh $SSH_ARGS "$HOST" "$*"; fi; }
rpush(){ if [ "$DRY" = 1 ]; then echo "[dry] rsync $*"; else rsync "$@"; fi; }

echo "== 1/4 数据库一致性快照（SQLite backup API，自动合并 WAL，不可直接 cp 活库）"
SNAP="$(cd "$ROOT" && env -u PYTHONPATH .venv/bin/python -c 'from server import db; print(db.backup_db("cloud"))')"
echo "   快照：$SNAP ($(du -h "$SNAP" | cut -f1))"

echo "== 2/4 云端目录准备 + 代码同步（cloud/ server/ web/index.html）"
rsh "mkdir -p $DIR/web $DIR/data"
rpush -az --delete -e "ssh $SSH_ARGS" \
      --exclude '__pycache__' --exclude '*.pyc' --exclude 'push.conf' \
      "$ROOT/cloud/"  "$HOST:$DIR/cloud/"
rpush -az --delete -e "ssh $SSH_ARGS" \
      --exclude '__pycache__' --exclude '*.pyc' \
      "$ROOT/server/" "$HOST:$DIR/server/"
rpush -az -e "ssh $SSH_ARGS" "$ROOT/web/index.html" "$HOST:$DIR/web/"

echo "== 3/4 数据库快照上云（先传 .new 再原子改名，朋友端不断档）"
rpush -az --progress -e "ssh $SSH_ARGS" "$SNAP" "$HOST:$DIR/data/wxk.db.new"
rsh "mv $DIR/data/wxk.db.new $DIR/data/wxk.db && ls -la $DIR/data/wxk.db"

if [ "$DB_ONLY" = 0 ]; then
  echo "== 4/4 原文与配图（增量；首次约 2.9G，之后只有新文章的量）"
  DEL=""
  [ "$PRUNE" = 1 ] && DEL="--delete"
  rpush -az $DEL -e "ssh $SSH_ARGS" "$ROOT/data/raw/"   "$HOST:$DIR/data/raw/"
  rpush -az $DEL -e "ssh $SSH_ARGS" "$ROOT/data/media/" "$HOST:$DIR/data/media/"
else
  echo "== 4/4 跳过（--db-only）"
fi

if [ "$INIT" = 1 ]; then
  echo "== 附：云端首次安装（venv + 依赖；不含 torch/FlagEmbedding，4G 机器装得动）"
  rsh "cd $DIR && python3 -m venv .venv && .venv/bin/pip install -r cloud/requirements.txt"
fi

echo "== 收尾检查"
rsh "test -f $DIR/.env && echo '云端 .env 存在' \
     || echo '⚠️ 云端 .env 不存在：ssh 上去按 cloud/env.example 建 $DIR/.env（MYRAG_CLOUD_PASSWORD 必填）'"

echo ""
echo "完成。后续动作："
echo "  1. ssh $HOST 'cd $DIR && systemctl restart myrag-cloud'   （systemd 部署好后）"
echo "  2. 浏览器开 http://服务器IP:8766 输口令验收"
echo "  3. 提醒：每次推送都会在 data/_backup/ 留一份 wxk-*-cloud.db（约300M），偶尔清理旧的"
