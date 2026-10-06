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

# stage 残留一行摘要。
# 为什么加这个：ingest 的 stage 清理是**静默失败**的（rmtree 撞 shim 守卫时只 log.warning），
# 抓完没提交 ingest 的路径又压根不走清理 —— 残留会悄悄攒（10-01 清过 328 个，三天后又是 50 个）。
# 挂到 status.sh 上是为了让它**当天就被看见**，而不是等攒几百个再一次性清。
# stage_check.py 只依赖标准库，系统 python3 就能跑；有 .venv 时优先用它。
# 注意：它返回非 0 表示"有待人工判断的残留"，这里必须吞掉，不能改变 status.sh 的退出码语义。
stage_brief() {
  local py="$ROOT/.venv/bin/python"
  [ -x "$py" ] || py="$(command -v python3 || true)"
  [ -n "$py" ] || return 0
  "$py" "$ROOT/scripts/stage_check.py" --brief >&2 2>&1 || true
}

BODY="$(curl -s --max-time 3 "$URL" || true)"
if [ -n "$BODY" ]; then
  echo "$BODY"
  if [ -f "$PID_FILE" ]; then
    # ${} 收边：紧跟中文全角括号时，set -u 会误吞多字节字符（见 stop.sh 同处注释）
    echo "启动方式=nohup（pid=$(cat "$PID_FILE")，端口=${PORT}）"
  fi
  stage_brief
  exit 0
fi

echo "MyRag 后端未响应（${PROBE_HOST}:${PORT}）" >&2
if [ -f "$PID_FILE" ]; then
  echo "残留 PID 文件：$(cat "$PID_FILE")（该进程已不存在，重启电脑后即为此种情况）" >&2
fi
echo "启动：bash scripts/start.sh" >&2
stage_brief
exit 1
