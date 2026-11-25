#!/usr/bin/env bash
# Запускает в одном контейнере три процесса:
#   1) collector — демон, получает сообщения из Telegram и пишет в bridge.db
#   2) MCP-сервер — читает bridge.db, слушает ТОЛЬКО 127.0.0.1:8765
#   3) Caddy — принимает запросы из интернета на $PORT, пускает к MCP только по секретному пути
# Если любой процесс завершится, контейнер выходит с ошибкой и Railway перезапускает его целиком.
set -euo pipefail

: "${BRIDGE_BOT_TOKEN:?BRIDGE_BOT_TOKEN не задан (Railway → Variables)}"
: "${BRIDGE_PROXY_SECRET:?BRIDGE_PROXY_SECRET не задан (Railway → Variables)}"

if [[ ! "$BRIDGE_PROXY_SECRET" =~ ^[A-Za-z0-9_-]{32,}$ ]]; then
  echo "ОТКАЗ ЗАПУСКА: BRIDGE_PROXY_SECRET должен быть не короче 32 символов (буквы, цифры, - и _)" >&2
  exit 1
fi

export PORT="${PORT:-8080}"
export BRIDGE_DATA_DIR="${BRIDGE_DATA_DIR:-/data}"
mkdir -p "$BRIDGE_DATA_DIR"

if [[ "${BRIDGE_SEND_POLICY:-approve}" != "approve" || -n "${BRIDGE_AUTO_SEND_CHAT_IDS:-}" ]]; then
  echo "ВНИМАНИЕ: включена автоотправка (BRIDGE_SEND_POLICY / BRIDGE_AUTO_SEND_CHAT_IDS)" >&2
fi

# Файлы не храним на сервере: read_media берёт их из Telegram по запросу (через демон)
export BRIDGE_STORE_MEDIA="${BRIDGE_STORE_MEDIA:-false}"
# Внутренний пароль между MCP и демоном: новый на каждый запуск, наружу не выходит
export BRIDGE_INTERNAL_TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n')"
export BRIDGE_FETCH_PORT=8766

BIN=/app/.venv/bin

# 1) collector: секрет прокси ему не нужен
env -u BRIDGE_PROXY_SECRET "$BIN/tg-business-bridge" &
COLLECTOR=$!

# 2) MCP: ни токена бота, ни секрета; слушает только localhost
env -u BRIDGE_BOT_TOKEN -u BRIDGE_PROXY_SECRET \
  BRIDGE_MCP_TRANSPORT=streamable-http \
  BRIDGE_MCP_HOST=127.0.0.1 \
  BRIDGE_MCP_PORT=8765 \
  "$BIN/tg-business-bridge-mcp" &
MCP=$!

# 3) Caddy: токен бота ему не нужен
env -u BRIDGE_BOT_TOKEN -u BRIDGE_INTERNAL_TOKEN caddy run --config /app/deploy/railway/Caddyfile --adapter caddyfile &
PROXY=$!

trap 'kill -TERM $COLLECTOR $MCP $PROXY 2>/dev/null || true' TERM INT

# ждём, пока завершится любой из процессов
set +e
wait -n $COLLECTOR $MCP $PROXY
CODE=$?
echo "Один из процессов завершился (код $CODE) — останавливаю остальные" >&2
kill -TERM $COLLECTOR $MCP $PROXY 2>/dev/null
wait
exit "${CODE:-1}"
