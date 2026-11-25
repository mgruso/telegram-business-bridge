import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError

from tg_business_bridge import db, media
from tg_business_bridge.config import Settings, assert_data_dir_safe
from tg_business_bridge.daemon import business_handlers, draft_handlers, fetch_server, group_handlers

log = logging.getLogger(__name__)

LOCAL_MARKER = ".bot-api-local"  # в data_dir: бот уже «выписан» из облака Telegram


def make_bot(settings: Settings) -> Bot:
    """Бот через облако Telegram или через свой Bot API сервер в локальном режиме."""
    if settings.bot_api_url:
        api = TelegramAPIServer.from_base(settings.bot_api_url, is_local=True)
        # свой сервер сначала сам качает файл из Telegram — большим файлам нужно время
        return Bot(token=settings.bot_token, session=AiohttpSession(api=api, timeout=300))
    return Bot(token=settings.bot_token)


async def move_between_servers(settings: Settings) -> None:
    """Переезд облако ↔ свой сервер. Перед работой через свой сервер Telegram требует
    один раз вызвать logOut в облаке (иначе апдейты могут не приходить); после этого
    10 минут бот не может вернуться в облако. Метка в data_dir запоминает, что выход
    уже сделан; без своего сервера метка снимается — при следующем переезде logOut
    будет вызван снова."""
    marker = settings.data_dir / LOCAL_MARKER
    if not settings.bot_api_url:
        if marker.exists():
            marker.unlink(missing_ok=True)
            log.warning("работа через облако Telegram (свой Bot API сервер выключен); "
                        "если бот недавно был на своём сервере, облако примет его через ~10 минут")
        return
    if marker.exists():
        return
    cloud = Bot(token=settings.bot_token)
    try:
        await cloud.log_out()
        log.warning("бот выписан из облака Telegram (logOut) — дальше работа через свой Bot API сервер")
    except TelegramAPIError as e:
        # уже выписан ранее — это нормально
        log.warning("logOut в облаке вернул ошибку (вероятно, уже выполнен): %s", e)
    finally:
        await cloud.session.close()
    marker.write_text(settings.bot_api_url, encoding="utf-8")


async def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    assert_data_dir_safe(settings)
    conn = db.connect(settings.db_path)
    recovered = db.recover_stale_sending(conn)
    if recovered > 0:
        log.warning("recovered %d stale sending draft(s)", recovered)
    swept = media.sweep_outbox(conn, settings.data_dir)
    if swept > 0:
        log.warning("removed %d orphaned outbox dir(s)", swept)
    await move_between_servers(settings)
    bot = make_bot(settings)
    if settings.bot_api_url:
        log.info("работа через свой Bot API сервер %s (файлы до %s)", settings.bot_api_url, media.max_download_label())
    me = await bot.get_me()
    log.info("bot @%s started", me.username)
    if me.can_read_all_group_messages:
        log.info("getMe: can_read_all_group_messages=True — в группах бот видит все сообщения")
    else:
        log.warning(
            "getMe: can_read_all_group_messages=False — в группах включён режим приватности, "
            "бот видит только команды, упоминания и ответы ему. Исправление: @BotFather → "
            "/setprivacy → Disable, затем заново добавить бота в группы"
        )

    dp = Dispatcher(conn=conn, settings=settings)
    dp.include_router(business_handlers.router)
    # до draft_handlers: сообщения групп не должны попадать в обработчики владельца
    dp.include_router(group_handlers.router)
    dp.include_router(draft_handlers.router)

    fetch_runner = await fetch_server.start(bot)
    watcher = asyncio.create_task(draft_handlers.watch_drafts(bot, conn, settings))
    media_cleanup = None
    if settings.media_retention_days > 0:
        media_cleanup = asyncio.create_task(media.watch_media_cleanup(conn, settings))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if fetch_runner is not None:
            await fetch_runner.cleanup()
        # cancel() лишь запрашивает отмену — дожидаемся фактического завершения задач
        for task in (watcher, media_cleanup):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def main() -> None:
    asyncio.run(run())
