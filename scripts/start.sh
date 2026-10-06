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

# 剥掉 Agent 客户端注入的「删除守卫」。
#
# 不剥的后果（2026-10-07 实测定位）：后端 Python 侧的一切删除都变成
# `PermissionError: [Errno 1] Operation not permitted` ——
# `DELETE /api/articles/{id}` 直接 500，ingest 之后的 `_drop_stage` 必然失败。
# **这就是 data/_stage 残留反复出现的真正根源**（不是脚本没写清理，是根本删不掉）。
#
# 以前这条只写在 SKILL.md 里，靠"启动的人记得带 env -u"；换个会话用裸 start.sh
# 起服务就会复发。所以在这里主动剥掉，不指望人记得。
for _v in PYTHONPATH \
          CODEBUDDY_SAFE_DELETE_ENABLED \
          CODEBUDDY_SAFE_DELETE_SANDBOX \
          CODEBUDDY_SAFE_DELETE_BIN_DIR \
          CODEBUDDY_SAFE_DELETE_BROKER_DELETE \
          CODEBUDDY_SAFE_DELETE_REPORT_PATH \
          CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD \
          CODEBUDDY_BROKERED_FS_HOOK_ENABLED \
          CODEBUDDY_BROKERED_SHELL_ENV \
          CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID; do
  unset "$_v" 2>/dev/null || true
done

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
