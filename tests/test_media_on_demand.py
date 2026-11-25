"""Файлы без хранения на сервере: read_media берёт их из Telegram по запросу."""
import asyncio
import base64
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from mcp.server.fastmcp import Image
from PIL import Image as PILImage

from tg_business_bridge import db, mcp_server, media
from tg_business_bridge.daemon import fetch_server

CHAT = 777


@pytest.fixture()
def c(conn, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_conn", lambda: conn)
    return conn


def add_file(conn, media_type="photo", file_name=None, file_size=1000, mid=1):
    raw = {}
    if file_name:
        raw[media_type] = {"file_name": file_name}
    return db.insert_message(conn, dict(
        connection_id="c1", chat_id=CHAT, message_id=mid, sender_id=5, sender_name="Ли",
        ts=1_800_000_000, direction="in", is_auto=0, media_type=media_type, text=None,
        file_id="FILE", file_unique_id="U", file_size=file_size, raw_json=json.dumps(raw),
    ))


def jpeg(w=640, h=480, color=(200, 30, 30)) -> bytes:
    buf = BytesIO()
    im = PILImage.new("RGB", (w, h), color)
    for x in range(0, w, 7):  # немного деталей, чтобы JPEG не был тривиальным
        im.putpixel((x, x % h), (0, 0, 0))
    im.save(buf, "JPEG", quality=93)
    return buf.getvalue()


def pdf(pages=7) -> bytes:
    ims = [PILImage.new("RGB", (595, 842), (30 * i % 255, 120, 200)) for i in range(pages)]
    buf = BytesIO()
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:], resolution=72)
    return buf.getvalue()


def fetch_returns(data: bytes, ext: str):
    return patch.object(mcp_server, "_fetch_file", return_value=(data, ext, None))


def images(out):
    return [x for x in out if isinstance(x, Image)]


def text(out):
    return "\n".join(x for x in out if isinstance(x, str)) if isinstance(out, list) else out


def test_photo_is_passed_byte_for_byte(c):
    add_file(c)
    original = jpeg()
    with fetch_returns(original, ".jpg") as f:
        out = mcp_server.read_media_impl(CHAT, 1)
    f.assert_called_once_with("FILE")
    assert images(out)[0].data == original  # ровно те же байты, без пересжатия
    assert "640×480" in text(out) and "без изменений" in text(out)


def test_image_region_is_lossless_crop(c):
    add_file(c)
    original = jpeg(1000, 800)
    with fetch_returns(original, ".jpg"):
        out = mcp_server.read_media_impl(CHAT, 1, region="0.5,0.5,1,1")
    img = PILImage.open(BytesIO(images(out)[0].data))
    assert img.format == "PNG" and img.size == (500, 400)
    src = PILImage.open(BytesIO(original)).convert("RGB").crop((500, 400, 1000, 800))
    assert list(img.convert("RGB").getdata()) == list(src.getdata())  # пиксель в пиксель


def test_big_image_suggests_region(c, monkeypatch):
    monkeypatch.setattr(mcp_server, "MAX_IMAGE_BYTES", 100)
    add_file(c, media_type="document", file_name="скан.jpg")
    with fetch_returns(jpeg(), ".jpg"):
        out = mcp_server.read_media_impl(CHAT, 1)
    assert isinstance(out, str) and "region" in out


def test_pdf_pages_as_images_with_next_hint(c):
    add_file(c, media_type="document", file_name="прайс.pdf")
    with fetch_returns(pdf(7), ".pdf"):
        out = mcp_server.read_media_impl(CHAT, 1)
    assert len(images(out)) == 5
    assert "7 стр." in text(out) and 'pages="6-7"' in text(out)
    page = PILImage.open(BytesIO(images(out)[0].data))
    assert page.format == "PNG"
    assert abs(page.width - round(595 * 200 / 72)) <= 2  # 200 dpi по умолчанию


def test_pdf_page_selection_and_dpi(c):
    add_file(c, media_type="document", file_name="прайс.pdf")
    with fetch_returns(pdf(7), ".pdf"):
        out = mcp_server.read_media_impl(CHAT, 1, pages="6-7", dpi=300)
    assert len(images(out)) == 2 and "Дальше" not in text(out)
    page = PILImage.open(BytesIO(images(out)[0].data))
    assert abs(page.width - round(595 * 300 / 72)) <= 2


def test_pdf_region_zooms(c):
    add_file(c, media_type="document", file_name="чертёж.pdf")
    with fetch_returns(pdf(1), ".pdf"):
        out = mcp_server.read_media_impl(CHAT, 1, region="0,0,0.25,0.25")
    page = PILImage.open(BytesIO(images(out)[0].data))
    # четверть страницы A4 рендерится на потолке 600 dpi: в 3 раза детальнее обычных 200 dpi
    zoom = mcp_server.ZOOM_DPI_MAX / 72
    assert abs(page.width - round(0.25 * 595 * zoom)) <= 2
    assert abs(page.height - round(0.25 * 842 * zoom)) <= 2
    assert "фрагмент" in text(out)


def test_pdf_limits_and_bad_args(c):
    add_file(c, media_type="document", file_name="a.pdf")
    with fetch_returns(pdf(20), ".pdf"):
        assert "не больше" in mcp_server.read_media_impl(CHAT, 1, pages="1-11")
        assert "таких страниц нет" in mcp_server.read_media_impl(CHAT, 1, pages="50")
    assert "region" in mcp_server.read_media_impl(CHAT, 1, region="1,1,0,0")


def test_fetch_errors_are_explained(c):
    add_file(c, media_type="document", file_name="a.pdf")
    with patch.object(mcp_server, "_fetch_file", return_value=(None, "", "больше 20 MB — Bot API")):
        assert "больше 20 MB" in mcp_server.read_media_impl(CHAT, 1)


def test_known_big_file_not_even_requested(c):
    add_file(c, media_type="document", file_name="big.pdf", file_size=50 * 1024 * 1024)
    with patch.object(mcp_server, "_fetch_file") as f:
        out = mcp_server.read_media_impl(CHAT, 1)
    f.assert_not_called()
    assert "больше 20 MB" in out


# --- внутренняя точка выдачи файлов в демоне и клиент MCP ---

def fake_bot(data=b"BYTES", size=5, path="photos/file_1.jpg"):
    bot = AsyncMock()
    bot.get_file.return_value = SimpleNamespace(file_size=size, file_path=path)
    bot.download_file.return_value = BytesIO(data)
    return bot


@pytest.mark.asyncio
async def test_fetch_server_requires_token_and_returns_bytes():
    bot = fake_bot(b"\xff\xd8photo")
    async with TestClient(TestServer(fetch_server.make_app(bot, "secret"))) as client:
        r = await client.get("/file", params={"file_id": "F"})
        assert r.status == 403
        r = await client.get("/file", params={"file_id": "F"}, headers={"X-Bridge-Token": "wrong"})
        assert r.status == 403
        r = await client.get("/file", params={"file_id": "F"}, headers={"X-Bridge-Token": "secret"})
        assert r.status == 200 and await r.read() == b"\xff\xd8photo"
        assert r.headers["X-File-Path"] == "photos/file_1.jpg"
    bot.get_file.assert_awaited_with("F")


@pytest.mark.asyncio
async def test_fetch_server_too_big():
    bot = fake_bot(size=media.MAX_DOWNLOAD_BYTES + 1)
    async with TestClient(TestServer(fetch_server.make_app(bot, "s"))) as client:
        r = await client.get("/file", params={"file_id": "F"}, headers={"X-Bridge-Token": "s"})
        assert r.status == 413
    bot.download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_fetch_file_end_to_end(monkeypatch):
    bot = fake_bot(b"%PDF-data", path="documents/file_9.pdf")
    server = TestServer(fetch_server.make_app(bot, "tok"), host="127.0.0.1")
    await server.start_server()
    try:
        monkeypatch.setenv("BRIDGE_INTERNAL_TOKEN", "tok")
        monkeypatch.setenv("BRIDGE_FETCH_PORT", str(server.port))
        data, ext, err = await asyncio.to_thread(mcp_server._fetch_file, "F")
        assert (data, ext, err) == (b"%PDF-data", ".pdf", None)
        monkeypatch.setenv("BRIDGE_INTERNAL_TOKEN", "wrong")
        data, _, err = await asyncio.to_thread(mcp_server._fetch_file, "F")
        assert data is None and err
    finally:
        await server.close()


def test_fetch_file_without_token_is_disabled(monkeypatch):
    monkeypatch.delenv("BRIDGE_INTERNAL_TOKEN", raising=False)
    data, _, err = mcp_server._fetch_file("F")
    assert data is None and "не настроена" in err


# --- приём новых сообщений без хранения файлов ---

def settings_for(tmp_path, store: bool, transcribe_key: str = ""):
    return SimpleNamespace(store_media=store, data_dir=tmp_path, media_dir=tmp_path / "media",
                           groq_api_key="", groq_model="", deepgram_api_key=transcribe_key)


def row_of(media_type):
    return {"media_type": media_type, "file_id": "F", "file_unique_id": "U", "file_size": 100,
            "ts": 1_800_000_000, "text": None}


@pytest.mark.asyncio
async def test_no_store_photo_not_downloaded(conn, tmp_path):
    rowid = add_file(conn)
    with patch.object(media, "download_media", AsyncMock()) as dl:
        await media.ingest_media(AsyncMock(), conn, settings_for(tmp_path, False), row_of("photo"), rowid)
    dl.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_store_voice_transcribed_then_deleted(conn, tmp_path):
    rowid = add_file(conn, media_type="voice")
    f = tmp_path / "media" / "2026-11" / "U.oga"
    f.parent.mkdir(parents=True)

    async def fake_download(bot, row, media_dir):
        f.write_bytes(b"opus")
        return "media/2026-11/U.oga"

    with patch.object(media, "download_media", fake_download), \
         patch("tg_business_bridge.transcribe.transcribe", AsyncMock(return_value="образцы в пятницу")):
        await media.ingest_media(AsyncMock(), conn, settings_for(tmp_path, False, "dg"), row_of("voice"), rowid)
    got = conn.execute("SELECT text, media_path FROM messages WHERE id=?", (rowid,)).fetchone()
    assert got["text"] == "образцы в пятницу" and got["media_path"] is None
    assert not f.exists()  # файл удалён сразу после расшифровки


@pytest.mark.asyncio
async def test_store_mode_keeps_old_behaviour(conn, tmp_path):
    rowid = add_file(conn)
    with patch.object(media, "download_media", AsyncMock(return_value="media/2026-11/U.jpg")):
        await media.ingest_media(AsyncMock(), conn, settings_for(tmp_path, True), row_of("photo"), rowid)
    assert conn.execute("SELECT media_path FROM messages").fetchone()[0] == "media/2026-11/U.jpg"


@pytest.mark.asyncio
async def test_read_media_tool_returns_image_through_mcp(c):
    add_file(c)
    with fetch_returns(jpeg(), ".jpg"):
        result = await mcp_server.mcp.call_tool("read_media", {"chat_id": CHAT, "message_id": 1})
    content = result[0] if isinstance(result, tuple) else result
    assert [x.type for x in content] == ["text", "image"]
    assert content[1].mimeType == "image/jpeg"
    assert base64.b64decode(content[1].data)[:2] == b"\xff\xd8"
