"""Групповые чаты: бот — обычный участник группы (не Business API).

Сохраняем сообщения только из групп, где состоит владелец (тот, кто подключил бота
в Telegram Business). Из чужих групп бот выходит сам: иначе любой мог бы добавить
бота к себе и подсовывать в архив владельца произвольный текст.
Чтобы бот видел все сообщения группы, а не только команды, в @BotFather нужно
выключить Group Privacy (Bot Settings → Group Privacy → Turn off).
"""
import logging
import sqlite3
import time

from aiogram import Bot, F, Router
from aiogram.types import Message

from tg_business_bridge import db, media
from tg_business_bridge.config import Settings
from tg_business_bridge.extract import extract_message_row

log = logging.getLogger(__name__)
router = Router()

GROUP_TYPES = {"group", "supergroup"}
_RECHECK_SEC = 600
# chat_id -> (разрешена ли группа, когда проверяли)
_allowed_cache: dict[int, tuple[bool, float]] = {}


def _owner_id(conn: sqlite3.Connection) -> int | None:
    connection = db.get_enabled_connection(conn)
    return connection["owner_id"] if connection is not None else None


async def _group_allowed(bot: Bot, conn: sqlite3.Connection, chat_id: int, owner_id: int) -> bool:
    cached = _allowed_cache.get(chat_id)
    if cached is not None and time.monotonic() - cached[1] < _RECHECK_SEC:
        return cached[0]
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=owner_id)
        allowed = member.status in {"creator", "administrator", "member", "restricted"}
    except Exception as exc:  # noqa: BLE001 - нет доступа к списку = считаем чужой
        log.warning("не удалось проверить владельца в группе %s: %s", chat_id, exc)
        allowed = False
    _allowed_cache[chat_id] = (allowed, time.monotonic())
    return allowed


@router.message(F.chat.type.in_(GROUP_TYPES))
async def on_group_message(
    msg: Message, conn: sqlite3.Connection, bot: Bot, settings: Settings,
) -> None:
    owner_id = _owner_id(conn)
    if owner_id is None:
        return  # бот ещё не подключён в Telegram Business — не знаем, кто владелец
    if not await _group_allowed(bot, conn, msg.chat.id, owner_id):
        log.warning("группа %s без владельца — выхожу", msg.chat.id)
        try:
            await bot.leave_chat(chat_id=msg.chat.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось выйти из группы %s: %s", msg.chat.id, exc)
        return
    if db.find_message_rowid(conn, msg.chat.id, msg.message_id) is not None:
        return  # повторная доставка апдейта
    row = extract_message_row(msg, bot_id=bot.id)
    # в группе «исходящие» — сообщения владельца и самого бота, остальное — входящие
    sender_id = row["sender_id"]
    row["direction"] = "out" if sender_id in (owner_id, bot.id) else "in"
    rowid = db.insert_message(conn, row)
    if media.should_download(row):
        rel = await media.download_media(bot, row, settings.media_dir)
        if rel is not None:
            db.set_media_path(conn, rowid, rel)


@router.edited_message(F.chat.type.in_(GROUP_TYPES))
async def on_group_edited(msg: Message, conn: sqlite3.Connection) -> None:
    new_text = msg.text or msg.caption
    rowid = db.find_message_rowid(conn, msg.chat.id, msg.message_id)
    if rowid is None:
        return
    db.add_message_event(
        conn, msg.chat.id, msg.message_id, "edited",
        msg.edit_date or int(time.time()), new_text=new_text,
    )
    if new_text is not None:
        db.set_message_text(conn, rowid, new_text)
