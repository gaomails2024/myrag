#!/usr/bin/env bash
# MyRag 后端启动（PRD §11.3）
#
# 项目根由本脚本位置推导 —— **项目可以放在任何目录**，不需要改脚本。
# 端口 / 主机 / 数据目录读 .env 与环境变量，与 server/config.py 同一套约定。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# 载入 .env（已存在的环境变量优先，见 _env.sh 里的说明）
# shellcheck source=./_env.sh disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"
load_env "$ROOT/.env"

PY="$ROOT/.venv/bin/python"
HOST="${MYRAG_HOST:-127.0.0.1}"
PORT="${MYRAG_PORT:-8765}"
PROBE_HOST="127.0.0.1"        # 健康检查固定走回环：HOST=0.0.0.0 时也能探通

DATA_DIR="${MYRAG_DATA_DIR:-$ROOT/data}"
PID_FILE="$DATA_DIR/server.pid"
LOG="$DATA_DIR/server.log"

mkdir -p "$DATA_DIR"

if [ -f "$PID_FILE" ]; then
  OLD="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    echo "MyRag 已在运行 (pid=$OLD) http://$PROBE_HOST:$PORT"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if [ ! -x "$PY" ]; then
  echo "虚拟环境不存在：$PY" >&2
  echo "先执行：" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# 权重已缓存 → 强制离线，避免 transformers 去探 HuggingFace（不通就卡住）
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false

nohup "$PY" -m uvicorn server.app:app --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
echo $! > "$PID_FILE"

for _ in $(seq 1 30); do
  if curl -s --max-time 2 "http://$PROBE_HOST:$PORT/api/health" >/dev/null 2>&1; then
    echo "MyRag 已启动 (pid=$(cat "$PID_FILE")) http://$PROBE_HOST:$PORT"
    exit 0
  fi
  sleep 0.5
done

echo "启动失败：15 秒内未通过健康检查，详见 $LOG" >&2
tail -30 "$LOG" >&2
exit 1
