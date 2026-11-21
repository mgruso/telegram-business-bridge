from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message

from tg_business_bridge import db
from tg_business_bridge.config import Settings
from tg_business_bridge.daemon import group_handlers
from tg_business_bridge.daemon.group_handlers import on_group_edited, on_group_message
from tg_business_bridge.mcp_server import list_chats_impl
from tg_business_bridge import mcp_server
from tg_business_bridge.sender import send_business_reply

GROUP = -1001234567890
OWNER = 42


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    return Settings()


@pytest.fixture(autouse=True)
def _clear_cache():
    group_handlers._allowed_cache.clear()
    yield
    group_handlers._allowed_cache.clear()


@pytest.fixture()
def owned(conn):
    db.upsert_connection(conn, "c1", OWNER, '{"can_reply": false}', True)
    return conn


def _gmsg(sender_id=7, text="привет всем", message_id=5, **over) -> Message:
    d = {
        "message_id": message_id,
        "date": 1751800000,
        "chat": {"id": GROUP, "type": "supergroup", "title": "Друзья"},
        "from": {"id": sender_id, "is_bot": False, "first_name": "Паша"},
        "text": text,
    }
    d.update(over)
    return Message.model_validate(d)


def _bot(owner_status="member"):
    bot = AsyncMock()
    bot.id = 99
    bot.get_chat_member.return_value = SimpleNamespace(status=owner_status)
    return bot


@pytest.mark.asyncio
async def test_group_message_saved_when_owner_is_member(owned, settings):
    bot = _bot()
    await on_group_message(_gmsg(), conn=owned, bot=bot, settings=settings)
    row = owned.execute("SELECT chat_id, text, direction FROM messages").fetchone()
    assert (row["chat_id"], row["text"], row["direction"]) == (GROUP, "привет всем", "in")
    bot.leave_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_message_in_group_is_outgoing(owned, settings):
    await on_group_message(_gmsg(sender_id=OWNER), conn=owned, bot=_bot(), settings=settings)
    assert owned.execute("SELECT direction FROM messages").fetchone()["direction"] == "out"


@pytest.mark.asyncio
async def test_foreign_group_not_saved_and_bot_leaves(owned, settings):
    bot = _bot(owner_status="left")
    await on_group_message(_gmsg(), conn=owned, bot=bot, settings=settings)
    assert owned.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    bot.leave_chat.assert_awaited_once_with(chat_id=GROUP)


@pytest.mark.asyncio
async def test_no_business_connection_ignores_group(conn, settings):
    bot = _bot()
    await on_group_message(_gmsg(), conn=conn, bot=bot, settings=settings)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    bot.leave_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_group_message_skipped(owned, settings):
    bot = _bot()
    await on_group_message(_gmsg(), conn=owned, bot=bot, settings=settings)
    await on_group_message(_gmsg(), conn=owned, bot=bot, settings=settings)
    assert owned.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_group_edit_updates_text(owned, settings):
    await on_group_message(_gmsg(), conn=owned, bot=_bot(), settings=settings)
    await on_group_edited(_gmsg(text="исправлено", edit_date=1751800100), conn=owned)
    assert owned.execute("SELECT text FROM messages").fetchone()["text"] == "исправлено"


@pytest.mark.asyncio
async def test_send_to_group_goes_as_bot_without_business_connection(owned):
    bot = AsyncMock()
    bot.id = 99
    bot.send_message.return_value = Message.model_validate({
        "message_id": 77, "date": 1751800000,
        "chat": {"id": GROUP, "type": "supergroup", "title": "Друзья"},
        "from": {"id": 99, "is_bot": True, "first_name": "bot"},
        "text": "ответ",
    })
    # у Business-подключения нет can_reply — для группы это не важно
    res = await send_business_reply(bot, owned, GROUP, "ответ")
    assert res == {"ok": True, "error": None}
    bot.send_message.assert_awaited_once_with(chat_id=GROUP, text="ответ", parse_mode=None)
    bot.read_business_message.assert_not_awaited()
    row = owned.execute("SELECT direction FROM messages WHERE message_id=77").fetchone()
    assert row["direction"] == "out"


@pytest.mark.asyncio
async def test_list_chats_shows_group_title(owned, settings, monkeypatch):
    await on_group_message(_gmsg(), conn=owned, bot=_bot(), settings=settings)
    monkeypatch.setattr(mcp_server, "get_conn", lambda: owned)
    out = list_chats_impl()
    assert f"chat {GROUP}: группа" in out
    assert "Друзья" in out
    assert "от имени бота" in out
