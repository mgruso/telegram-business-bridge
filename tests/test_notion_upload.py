"""save_file_to_notion: загрузка файлов из Telegram в Notion (с «поддельным Notion»)."""
import json
import re
import threading
from types import SimpleNamespace

import httpx
import pytest

from tg_business_bridge import db, mcp_server, notion_upload

CHAT = 777
PAGE = "0123456789abcdef0123456789abcdef"
PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"
PAGE_URL = f"https://www.notion.so/workspace/Li-Mei-factory-{PAGE}?pvs=4"


class FakeNotion:
    def __init__(self):
        self.calls: list[tuple[str, str, httpx.Request]] = []
        self.uploads = 0
        self.properties: dict = {}
        self.page_status = 200
        self.gate: threading.Event | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.gate is not None:
            self.gate.wait(5)
        path = request.url.path
        self.calls.append((request.method, path, request))
        if self.page_status != 200 and ("/pages/" in path or "/blocks/" in path):
            return httpx.Response(self.page_status, json={"code": "object_not_found", "message": "nope"})
        if request.method == "POST" and path == "/v1/file_uploads":
            self.uploads += 1
            return httpx.Response(200, json={"id": f"up{self.uploads}", "status": "pending"})
        if request.method == "POST" and re.fullmatch(r"/v1/file_uploads/[^/]+/(send|complete)", path):
            return httpx.Response(200, json={"status": "uploaded"})
        if request.method == "GET" and path.startswith("/v1/pages/"):
            return httpx.Response(200, json={"properties": self.properties})
        if request.method == "PATCH" and path.endswith("/children"):
            return httpx.Response(200, json={"results": [{"id": "blk1"}]})
        if request.method == "PATCH" and path.startswith("/v1/pages/"):
            body = json.loads(request.content)
            for k, v in body["properties"].items():
                self.properties[k] = {"type": "files", "files": [
                    {"name": f.get("name"), "type": "file", "file": {"url": "https://s3/x"}}
                    if f["type"] == "file_upload" else f for f in v["files"]]}
            return httpx.Response(200, json={})
        return httpx.Response(400, json={"code": "unexpected", "message": path})

    def json_bodies(self, method, suffix):
        return [json.loads(r.content) for m, p, r in self.calls if m == method and p.endswith(suffix)]


@pytest.fixture()
def notion(conn, tmp_path, monkeypatch):
    fake = FakeNotion()
    monkeypatch.setenv("NOTION_TOKEN", "ntn_test")
    monkeypatch.setenv("OWNER_TZ", "Europe/Moscow")
    monkeypatch.setattr(mcp_server, "get_conn", lambda: conn)
    monkeypatch.setattr(mcp_server, "get_settings",
                        lambda: SimpleNamespace(db_path=tmp_path / "bridge.db"))
    monkeypatch.setattr(mcp_server, "_notion_client",
                        lambda: notion_upload.NotionClient("ntn_test", transport=httpx.MockTransport(fake.handler)))
    files: dict[int, bytes] = {}
    monkeypatch.setattr(mcp_server, "_load_file",
                        lambda row: (files.get(row["message_id"], b"\xff\xd8photo"), ".jpg", None))
    fake.files = files
    return fake


def add(conn, mid=1, media_type="photo", file_name=None, uid=None):
    raw = {"chat": {"id": CHAT, "type": "private", "first_name": "Ли"}}
    if file_name:
        raw[media_type] = {"file_name": file_name}
    db.insert_message(conn, dict(
        connection_id="c1", chat_id=CHAT, message_id=mid, sender_id=5, sender_name="Ли Мэй",
        ts=1_795_445_460, direction="in", is_auto=0, media_type=media_type, text=None,
        file_id=f"F{mid}", file_unique_id=uid or f"U{mid}", file_size=100, raw_json=json.dumps(raw)))


def test_parse_page_id():
    assert notion_upload.parse_page_id(PAGE_URL) == PAGE_ID
    assert notion_upload.parse_page_id(PAGE_ID) == PAGE_ID
    assert notion_upload.parse_page_id(PAGE) == PAGE_ID
    assert notion_upload.parse_page_id("https://notion.so/nothing-here") is None


def test_photo_goes_to_page_body_as_image(notion, conn):
    add(conn)
    out = mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_URL, caption="образец ткани")
    assert "готово" in out and "notion.so/" + PAGE in out
    block = notion.json_bodies("PATCH", f"/blocks/{PAGE_ID}/children")[0]["children"][0]
    assert block["type"] == "image" and block["image"]["file_upload"]["id"] == "up1"
    caption = block["image"]["caption"][0]["text"]["content"]
    assert "Из Telegram" in caption and "Ли Мэй" in caption and "образец ткани" in caption
    assert "+03:00" in caption
    req = notion.calls[0][2]
    assert req.headers["Notion-Version"] == notion_upload.NOTION_VERSION
    assert req.headers["Authorization"] == "Bearer ntn_test"


def test_pdf_block_and_filename(notion, conn):
    add(conn, mid=2, media_type="document", file_name="прайс 2026.pdf")
    notion.files[2] = b"%PDF-1.7 data"
    mcp_server.save_file_to_notion_impl(CHAT, 2, PAGE_ID)
    block = notion.json_bodies("PATCH", "/children")[0]["children"][0]
    assert block["type"] == "pdf"
    created = notion.json_bodies("POST", "/v1/file_uploads")[0]
    assert created == {"filename": "прайс 2026.pdf", "content_type": "application/pdf"}


def test_big_file_uploaded_in_parts(notion, conn, monkeypatch):
    monkeypatch.setattr(notion_upload, "SINGLE_PART_MAX", 10)
    monkeypatch.setattr(notion_upload, "PART_SIZE", 10)
    add(conn, mid=3, media_type="document", file_name="big.zip")
    notion.files[3] = b"x" * 25
    out = mcp_server.save_file_to_notion_impl(CHAT, 3, PAGE_ID)
    assert "готово" in out
    created = notion.json_bodies("POST", "/v1/file_uploads")[0]
    assert created["mode"] == "multi_part" and created["number_of_parts"] == 3
    sends = [r for m, p, r in notion.calls if p.endswith("/send")]
    assert [re.search(rb'name="part_number"\r\n\r\n(\d+)', r.content)[1] for r in sends] == [b"1", b"2", b"3"]
    assert any(p.endswith("/complete") for _, p, _ in notion.calls)
    assert notion.json_bodies("PATCH", "/children")[0]["children"][0]["type"] == "file"


def test_duplicate_is_not_uploaded_twice(notion, conn):
    add(conn)
    mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_ID)
    out = mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_URL)
    assert out.startswith("Уже сделано")
    assert notion.uploads == 1


def test_files_property_append_keeps_our_previous_files(notion, conn):
    add(conn, mid=1)
    add(conn, mid=2)
    notion.properties = {"Файлы": {"type": "files", "files": []},
                         "Name": {"type": "title", "title": []}}
    assert "готово" in mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_ID, property="Файлы")
    out = mcp_server.save_file_to_notion_impl(CHAT, 2, PAGE_ID, property="Файлы")
    assert "готово" in out and "тело" not in out
    last = notion.json_bodies("PATCH", f"/pages/{PAGE_ID}")[-1]["properties"]["Файлы"]["files"]
    assert [f["file_upload"]["id"] for f in last] == ["up1", "up2"]  # первый файл сохранён


def test_manual_files_in_property_are_never_erased(notion, conn):
    add(conn)
    notion.properties = {"Файлы": {"type": "files", "files": [
        {"name": "договор.pdf", "type": "file", "file": {"url": "https://s3/manual"}}]}}
    out = mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_ID, property="Файлы")
    assert "добавленные вручную" in out and "тело" in out
    assert notion.json_bodies("PATCH", f"/pages/{PAGE_ID}") == []  # поле не трогали
    assert notion.json_bodies("PATCH", "/children")


def test_missing_property_falls_back_to_body(notion, conn):
    add(conn)
    notion.properties = {"Вложения": {"type": "files", "files": []}}
    out = mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_ID, property="Файлы")
    assert "Вложения" in out and "тело" in out


def test_page_without_access_explains_connections(notion, conn):
    add(conn)
    notion.page_status = 404
    out = mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_ID)
    assert "ошибка" in out and "Connections" in out
    # после ошибки можно попробовать снова — это не «дубль»
    notion.page_status = 200
    assert "готово" in mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_ID)


def test_no_token(notion, conn, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN")
    add(conn)
    assert "NOTION_TOKEN" in mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_ID)


def test_bad_page_and_no_file(notion, conn):
    add(conn)
    assert "ссылка на страницу" in mcp_server.save_file_to_notion_impl(CHAT, 1, "какая-то карточка")
    db.insert_message(conn, dict(connection_id="c1", chat_id=CHAT, message_id=9, sender_id=5,
                                 sender_name="Ли", ts=1, direction="in", is_auto=0, media_type="text",
                                 text="hi", raw_json="{}"))
    assert mcp_server.save_file_to_notion_impl(CHAT, 9, PAGE_ID) == "В этом сообщении нет файла."


def test_slow_upload_continues_in_background(notion, conn, monkeypatch):
    monkeypatch.setattr(mcp_server, "NOTION_WAIT_SECONDS", 0.2)
    add(conn)
    notion.gate = threading.Event()
    out = mcp_server.save_file_to_notion_impl(CHAT, 1, PAGE_ID)
    assert "в фоне" in out and "notion_upload_status(job_id=1)" in out
    assert "загрузка идёт" in mcp_server.notion_upload_status_impl(1)
    notion.gate.set()
    for t in threading.enumerate():
        if t.name.startswith("notion-upload-"):
            t.join(5)
    assert "готово" in mcp_server.notion_upload_status_impl(1)
    assert "готово" in mcp_server.notion_upload_status_impl()
