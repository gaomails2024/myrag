#!/usr/bin/env bash
# MyRag 后端状态（PRD §11.3）：回 /api/health，并提示怎么启动
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# shellcheck source=./_env.sh disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"
load_env "$ROOT/.env"

PORT="${MYRAG_PORT:-8765}"
PROBE_HOST="127.0.0.1"
DATA_DIR="${MYRAG_DATA_DIR:-$ROOT/data}"
PID_FILE="$DATA_DIR/server.pid"
URL="http://$PROBE_HOST:$PORT/api/health"

BODY="$(curl -s --max-time 3 "$URL" || true)"
if [ -n "$BODY" ]; then
  echo "$BODY"
  if [ -f "$PID_FILE" ]; then
    # ${} 收边：紧跟中文全角括号时，set -u 会误吞多字节字符（见 stop.sh 同处注释）
    echo "启动方式=nohup（pid=$(cat "$PID_FILE")，端口=${PORT}）"
  fi
  exit 0
fi

echo "MyRag 后端未响应（${PROBE_HOST}:${PORT}）" >&2
if [ -f "$PID_FILE" ]; then
  echo "残留 PID 文件：$(cat "$PID_FILE")（该进程已不存在，重启电脑后即为此种情况）" >&2
fi
echo "启动：bash scripts/start.sh" >&2
exit 1
