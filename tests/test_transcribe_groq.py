from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from tg_business_bridge import db, transcribe
from tg_business_bridge.daemon import group_handlers
from tg_business_bridge.daemon.group_handlers import on_group_message
from test_transcribe import _mock_session


@pytest.mark.asyncio
async def test_groq_happy_path_sends_ogg_name(tmp_path):
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"opus")
    session = _mock_session(200, {"text": " привет из группы "})
    with patch("tg_business_bridge.transcribe.aiohttp.ClientSession", return_value=session):
        text = await transcribe.transcribe_groq(str(audio), "gsk_test")
    assert text == "привет из группы"
    sess = session.__aenter__.return_value
    url = sess.post.call_args.args[0]
    kwargs = sess.post.call_args.kwargs
    assert url == transcribe.GROQ_URL
    assert kwargs["headers"]["Authorization"] == "Bearer gsk_test"
    fields = {f[0]["name"]: f for f in kwargs["data"]._fields}
    assert fields["file"][0]["filename"] == "voice.ogg"  # .oga Groq не принимает
    assert fields["model"][2] == transcribe.GROQ_DEFAULT_MODEL


@pytest.mark.asyncio
async def test_groq_error_returns_none(tmp_path):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"x")
    with patch("tg_business_bridge.transcribe.aiohttp.ClientSession",
               return_value=_mock_session(429, text_data="rate limit")):
        assert await transcribe.transcribe_groq(str(audio), "k") is None


@pytest.mark.asyncio
async def test_groq_empty_text_returns_none(tmp_path):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"x")
    with patch("tg_business_bridge.transcribe.aiohttp.ClientSession",
               return_value=_mock_session(200, {"text": "  "})):
        assert await transcribe.transcribe_groq(str(audio), "k") is None


@pytest.mark.asyncio
async def test_provider_choice():
    both = SimpleNamespace(groq_api_key="g", groq_model="", deepgram_api_key="d")
    only_dg = SimpleNamespace(groq_api_key="", groq_model="", deepgram_api_key="d")
    none = SimpleNamespace(groq_api_key="", groq_model="", deepgram_api_key="")
    assert transcribe.enabled(both) and transcribe.enabled(only_dg) and not transcribe.enabled(none)
    with patch.object(transcribe, "transcribe_groq", AsyncMock(return_value="G")) as g, \
         patch.object(transcribe, "transcribe_file", AsyncMock(return_value="D")) as d:
        assert await transcribe.transcribe(both, "f.ogg") == "G"
        assert await transcribe.transcribe(only_dg, "f.ogg") == "D"
        assert await transcribe.transcribe(none, "f.ogg") is None
        g.assert_awaited_once()
        d.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_voice_is_transcribed(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_GROQ_API_KEY", "gsk")
    from tg_business_bridge.config import Settings
    settings = Settings()
    group_handlers._allowed_cache.clear()
    db.upsert_connection(conn, "c1", 42, "{}", True)
    msg = Message.model_validate({
        "message_id": 5, "date": 1751800000,
        "chat": {"id": -100777, "type": "supergroup", "title": "Фабрика"},
        "from": {"id": 7, "is_bot": False, "first_name": "Ли"},
        "voice": {"file_id": "F", "file_unique_id": "U", "duration": 12, "file_size": 1000},
    })
    bot = AsyncMock()
    bot.id = 99
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    with patch.object(group_handlers.media, "download_media", AsyncMock(return_value="media/2026-11/U.oga")), \
         patch.object(group_handlers.transcribe, "transcribe", AsyncMock(return_value="образцы будут в пятницу")) as tr:
        await on_group_message(msg, conn=conn, bot=bot, settings=settings)
    tr.assert_awaited_once()
    row = conn.execute("SELECT text, media_type FROM messages").fetchone()
    assert row["media_type"] == "voice" and row["text"] == "образцы будут в пятницу"
    group_handlers._allowed_cache.clear()
