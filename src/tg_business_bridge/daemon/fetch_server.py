"""Внутренняя точка выдачи файлов из Telegram для MCP-сервера.

MCP-сервер намеренно не знает токен бота. Когда агенту нужен файл, которого нет на
диске (BRIDGE_STORE_MEDIA=false), MCP просит демон: GET /file?file_id=… с заголовком
X-Bridge-Token. Демон скачивает файл из Telegram в память и отдаёт байты, ничего не
сохраняя. Слушает только 127.0.0.1; без BRIDGE_INTERNAL_TOKEN не запускается.
"""
import hmac
import logging
import os

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiohttp import web

from tg_business_bridge import media

log = logging.getLogger(__name__)

DEFAULT_PORT = 8766


def internal_token() -> str:
    return (os.environ.get("BRIDGE_INTERNAL_TOKEN") or "").strip()


def fetch_port() -> int:
    try:
        return int(os.environ.get("BRIDGE_FETCH_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def make_app(bot: Bot, token: str) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("X-Bridge-Token", ""), token):
            return web.Response(status=403, text="forbidden")
        file_id = request.query.get("file_id", "")
        if not file_id or len(file_id) > 512:
            return web.Response(status=400, text="bad file_id")
        try:
            file = await bot.get_file(file_id)
        except TelegramBadRequest as e:
            if "too big" in str(e).lower():
                return web.Response(status=413, text="file is too big for Bot API")
            log.warning("getFile failed: %s", e)
            return web.Response(status=404, text="file not available")
        except TelegramAPIError as e:
            log.warning("getFile failed: %s", e)
            return web.Response(status=502, text="telegram error")
        if file.file_size and file.file_size > media.max_download_bytes():
            media.release_local_file(bot, file.file_path)
            return web.Response(status=413, text="file is too big for Bot API")
        if not file.file_path:
            return web.Response(status=404, text="file not available")
        try:
            buf = await bot.download_file(file.file_path, timeout=300)
        except (TelegramAPIError, OSError) as e:
            log.warning("download failed: %s", e)
            return web.Response(status=502, text="download failed")
        finally:
            media.release_local_file(bot, file.file_path)
        data = buf.getvalue() if buf is not None else b""
        return web.Response(body=data, content_type="application/octet-stream",
                            headers={"X-File-Path": file.file_path})

    app = web.Application()
    app.router.add_get("/file", handle)
    return app


async def start(bot: Bot) -> web.AppRunner | None:
    token = internal_token()
    if not token:
        log.info("BRIDGE_INTERNAL_TOKEN не задан — выдача файлов по запросу выключена")
        return None
    runner = web.AppRunner(make_app(bot, token), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", fetch_port())
    await site.start()
    log.info("выдача файлов по запросу слушает 127.0.0.1:%s", fetch_port())
    return runner
