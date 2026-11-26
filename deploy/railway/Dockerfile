# Образ для Railway: collector + MCP-сервер + прокси Caddy в одном контейнере,
# плюс (по желанию) собственный Telegram Bot API сервер в локальном режиме —
# он позволяет боту скачивать файлы больше 20 MB. Обычный ./Dockerfile не тронут.
#
# Bot API сервер не компилируем (на Railway компиляция tdlib не помещается в лимиты
# сборки): берём готовый бинарник официального tdlib/telegram-bot-api из образа
# aiogram/telegram-bot-api. Он собран под Alpine (musl), поэтому и основной образ —
# Alpine; все Python-зависимости моста имеют готовые musllinux-колёса.
FROM aiogram/telegram-bot-api:10.5 AS tgbotapi

FROM python:3.12-alpine
RUN apk add --no-cache bash openssl libstdc++ ca-certificates
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy
COPY --from=tgbotapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
RUN telegram-bot-api --version

ENV PYTHONUNBUFFERED=1 \
    BRIDGE_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev \
 && sed -i 's/\r$//' deploy/railway/start.sh \
 && chmod +x deploy/railway/start.sh \
 && PORT=8080 BRIDGE_PROXY_SECRET=build-check-only-not-a-real-secret-000 \
    caddy validate --config deploy/railway/Caddyfile --adapter caddyfile

CMD ["/app/deploy/railway/start.sh"]
