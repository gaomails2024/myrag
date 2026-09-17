#!/usr/bin/env bash
# MyRag 后端停止（PRD §11.3）
#
# 服务是按需启动的（scripts/start.sh，nohup），本脚本用于停止它。
# 停掉不影响数据：全部状态在数据目录的 wxk.db 与 raw/、media/ 下。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# shellcheck source=./_env.sh disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"
load_env "$ROOT/.env"

DATA_DIR="${MYRAG_DATA_DIR:-$ROOT/data}"
PID_FILE="$DATA_DIR/server.pid"

if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "MyRag 已停止 (pid=$PID)"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# 兜底：按端口定位进程。
# 为什么不用 pkill -f "uvicorn server.app:app"：那会误杀**另一个实例**
# （同时跑正式版和开发版时必踩），也会误杀别的用户的同名进程。
PORT="${MYRAG_PORT:-8765}"
PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
if [ -n "$PID" ]; then
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.5
  done
  kill -9 "$PID" 2>/dev/null || true
  # 变量一律用 ${} 收边：紧跟中文标点时，set -u 会把多字节字符的一部分
  # 当成变量名（报 "PID，: unbound variable"）
  echo "MyRag 已停止 (pid=${PID}，端口 ${PORT})"
else
  echo "MyRag 未在运行（端口 $PORT 上没有人监听）"
fi
