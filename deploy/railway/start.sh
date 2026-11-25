#!/usr/bin/env bash
# Запускает в одном контейнере процессы:
#   0) (если заданы TELEGRAM_API_ID и TELEGRAM_API_HASH) свой Telegram Bot API сервер
#      в локальном режиме на 127.0.0.1:8081 — бот может скачивать файлы больше 20 MB
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
PIDS=()

# 0) свой Bot API сервер — только если заданы оба ключа с my.telegram.org
if [[ -n "${TELEGRAM_API_ID:-}" && -n "${TELEGRAM_API_HASH:-}" ]]; then
  TGAPI_DIR=/tmp/tgapi  # временные файлы, не том: мы ничего не храним
  mkdir -p "$TGAPI_DIR/tmp"
  env -u BRIDGE_BOT_TOKEN -u BRIDGE_PROXY_SECRET -u BRIDGE_INTERNAL_TOKEN \
    telegram-bot-api --local --http-ip-address=127.0.0.1 --http-port=8081 \
      --dir="$TGAPI_DIR" --temp-dir="$TGAPI_DIR/tmp" &
  PIDS+=($!)
  for _ in $(seq 1 60); do
    (exec 3<>/dev/tcp/127.0.0.1/8081) 2>/dev/null && break
    sleep 0.5
  done
  export BRIDGE_BOT_API_URL="http://127.0.0.1:8081"
  echo "свой Telegram Bot API сервер запущен (локальный режим, 127.0.0.1:8081)" >&2
else
  unset BRIDGE_BOT_API_URL || true
fi
NO_API_KEYS=(-u TELEGRAM_API_ID -u TELEGRAM_API_HASH)

# 1) collector: секрет прокси ему не нужен
env "${NO_API_KEYS[@]}" -u BRIDGE_PROXY_SECRET "$BIN/tg-business-bridge" &
PIDS+=($!)

# 2) MCP: ни токена бота, ни секрета; слушает только localhost
env "${NO_API_KEYS[@]}" -u BRIDGE_BOT_TOKEN -u BRIDGE_PROXY_SECRET \
  BRIDGE_MCP_TRANSPORT=streamable-http \
  BRIDGE_MCP_HOST=127.0.0.1 \
  BRIDGE_MCP_PORT=8765 \
  "$BIN/tg-business-bridge-mcp" &
PIDS+=($!)

# 3) Caddy: токен бота ему не нужен
env "${NO_API_KEYS[@]}" -u BRIDGE_BOT_TOKEN -u BRIDGE_INTERNAL_TOKEN \
  caddy run --config /app/deploy/railway/Caddyfile --adapter caddyfile &
PIDS+=($!)

trap 'kill -TERM "${PIDS[@]}" 2>/dev/null || true' TERM INT

# ждём, пока завершится любой из процессов
set +e
wait -n "${PIDS[@]}"
CODE=$?
echo "Один из процессов завершился (код $CODE) — останавливаю остальные" >&2
kill -TERM "${PIDS[@]}" 2>/dev/null
wait
exit "${CODE:-1}"
