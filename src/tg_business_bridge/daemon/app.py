import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher

from tg_business_bridge import db, media
from tg_business_bridge.config import Settings, assert_data_dir_safe
from tg_business_bridge.daemon import business_handlers, draft_handlers, fetch_server, group_handlers

log = logging.getLogger(__name__)


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
    bot = Bot(token=settings.bot_token)
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
