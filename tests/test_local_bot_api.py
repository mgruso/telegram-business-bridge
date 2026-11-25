"""Свой Telegram Bot API сервер (локальный режим): лимиты, удаление копий, переезд из облака."""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiohttp.test_utils import TestClient, TestServer

from tg_business_bridge import media
from tg_business_bridge.daemon import app, fetch_server


def test_limits_cloud_vs_local(monkeypatch):
    monkeypatch.delenv("BRIDGE_BOT_API_URL", raising=False)
    assert media.max_download_bytes() == 20 * 1024 * 1024
    assert media.should_download({"file_id": "F", "file_size": 50 * 1024 * 1024}) is False
    monkeypatch.setenv("BRIDGE_BOT_API_URL", "http://127.0.0.1:8081")
    assert media.max_download_bytes() == media.LOCAL_DEFAULT_MAX_MB * 1024 * 1024
    assert media.should_download({"file_id": "F", "file_size": 50 * 1024 * 1024}) is True
    monkeypatch.setenv("BRIDGE_MAX_FILE_MB", "64")
    assert media.max_download_label() == "64 MB"


def settings(tmp_path, url=""):
    return SimpleNamespace(bot_token="1:x", bot_api_url=url, data_dir=tmp_path)


def test_make_bot_local_session(tmp_path):
    bot = app.make_bot(settings(tmp_path, "http://127.0.0.1:8081"))
    assert bot.session.api.is_local
    assert bot.session.api.api_url("1:x", "getMe") == "http://127.0.0.1:8081/bot1:x/getMe"
    assert not app.make_bot(settings(tmp_path)).session.api.is_local


@pytest.mark.asyncio
async def test_logout_from_cloud_once(tmp_path):
    s = settings(tmp_path, "http://127.0.0.1:8081")
    cloud = AsyncMock()
    with patch.object(app, "Bot", return_value=cloud):
        await app.move_between_servers(s)
        await app.move_between_servers(s)  # второй запуск — logOut не повторяется
    cloud.log_out.assert_awaited_once()
    assert (tmp_path / app.LOCAL_MARKER).exists()


@pytest.mark.asyncio
async def test_logout_error_is_tolerated(tmp_path):
    s = settings(tmp_path, "http://127.0.0.1:8081")
    cloud = AsyncMock()
    cloud.log_out.side_effect = TelegramBadRequest(method=None, message="Logged out")
    with patch.object(app, "Bot", return_value=cloud):
        await app.move_between_servers(s)
    assert (tmp_path / app.LOCAL_MARKER).exists()


@pytest.mark.asyncio
async def test_back_to_cloud_clears_marker(tmp_path):
    (tmp_path / app.LOCAL_MARKER).write_text("x")
    with patch.object(app, "Bot") as bot_cls:
        await app.move_between_servers(settings(tmp_path))
    bot_cls.assert_not_called()
    assert not (tmp_path / app.LOCAL_MARKER).exists()


def local_bot(tmp_path, data=b"%PDF-big", size=50 * 1024 * 1024):
    path = tmp_path / "documents" / "file_7.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    bot = AsyncMock()
    bot.session = SimpleNamespace(api=SimpleNamespace(
        is_local=True, wrap_local_file=SimpleNamespace(to_local=lambda p: p)))
    bot.get_file.return_value = SimpleNamespace(file_size=size, file_path=str(path))
    bot.download_file.return_value = BytesIO(data)
    return bot, path


@pytest.mark.asyncio
async def test_fetch_big_file_in_local_mode_and_delete_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_BOT_API_URL", "http://127.0.0.1:8081")
    bot, path = local_bot(tmp_path)
    async with TestClient(TestServer(fetch_server.make_app(bot, "t"))) as client:
        r = await client.get("/file", params={"file_id": "F"}, headers={"X-Bridge-Token": "t"})
        assert r.status == 200 and await r.read() == b"%PDF-big"
    assert not path.exists()  # копия своего сервера удалена сразу после передачи


@pytest.mark.asyncio
async def test_too_big_even_for_local_deletes_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_BOT_API_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("BRIDGE_MAX_FILE_MB", "10")
    bot, path = local_bot(tmp_path, size=50 * 1024 * 1024)
    async with TestClient(TestServer(fetch_server.make_app(bot, "t"))) as client:
        r = await client.get("/file", params={"file_id": "F"}, headers={"X-Bridge-Token": "t"})
        assert r.status == 413
    assert not path.exists()


def test_release_is_noop_in_cloud_mode(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"1")
    bot = SimpleNamespace(session=SimpleNamespace(api=SimpleNamespace(is_local=False)))
    media.release_local_file(bot, str(f))
    assert f.exists()
