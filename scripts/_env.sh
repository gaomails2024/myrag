#!/usr/bin/env bash
# ================================================================
# 载入项目根的 .env —— 但**不覆盖已经存在的环境变量**。
#
# 为什么不能直接 `. ./.env`：那会把命令行传进来的值顶掉，
# 于是 `MYRAG_PORT=9000 bash scripts/start.sh` 会失效，与
# server/config.py 承诺的「环境变量 > .env > 默认值」不一致。
#
# 用法（在 start.sh / stop.sh / status.sh 里）：
#     . "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
#     load_env "$ROOT/.env"
# ================================================================

load_env() {
  local file="$1" line key val
  [ -f "$file" ] || return 0

  while IFS= read -r line || [ -n "$line" ]; do
    # 跳过空行与注释
    case "$line" in
      '' | '#'*) continue ;;
    esac
    # 只认 KEY=value
    case "$line" in
      *=*) ;;
      *) continue ;;
    esac

    key="${line%%=*}"
    val="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    # 去掉值两侧空白
    val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    # 去掉成对的引号
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac

    [ -n "$key" ] || continue
    # 已有值就不动（命令行 / 父进程传入的优先）
    if [ -z "${!key:-}" ]; then
      export "$key=$val"
    fi
  done < "$file"
}
