# Образ для Railway: collector + MCP-сервер + прокси Caddy в одном контейнере.
# Обычный ./Dockerfile (только collector, для docker compose) не тронут.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy

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
