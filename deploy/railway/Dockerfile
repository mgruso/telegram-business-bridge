# Образ для Railway: collector + MCP-сервер + прокси Caddy в одном контейнере,
# плюс (по желанию) собственный Telegram Bot API сервер в локальном режиме —
# он позволяет боту скачивать файлы больше 20 MB. Обычный ./Dockerfile не тронут.

# --- сборка официального telegram-bot-api (https://github.com/tdlib/telegram-bot-api) ---
# Та же база, что и у основного образа: бинарник совместим с его системными библиотеками.
# Слой кэшируется Railway — долгая компиляция нужна только при первой сборке.
FROM python:3.12-slim AS tgbotapi
RUN apt-get update \
 && apt-get install -y --no-install-recommends make git zlib1g-dev libssl-dev gperf cmake g++ ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ARG TGBOTAPI_REF=master
RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git /src \
 && cd /src && git checkout "$TGBOTAPI_REF" && git submodule update --init --recursive \
 && mkdir build && cd build \
 && cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/opt/tgapi .. \
 && cmake --build . --target install -j 2 \
 && strip /opt/tgapi/bin/telegram-bot-api \
 && /opt/tgapi/bin/telegram-bot-api --version

# --- основной образ ---
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy
COPY --from=tgbotapi /opt/tgapi/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
RUN apt-get update \
 && apt-get install -y --no-install-recommends libssl3 zlib1g libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && telegram-bot-api --version

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
