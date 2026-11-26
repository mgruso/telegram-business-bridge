"""Загрузка файлов из Telegram в Notion через официальный Notion API.

Файл загружается File Upload API (до 20 MB — одним запросом, больше — частями по
10 MB, до 5 GB на платных тарифах) и сразу прикрепляется к странице: блоком в тело
страницы или в свойство типа «Files & media». Ключ — NOTION_TOKEN (внутренняя
интеграция); доступ есть только к страницам, которые явно открыты интеграции.
"""
import math
import mimetypes
import re

import httpx

API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
SINGLE_PART_MAX = 20 * 1024 * 1024
PART_SIZE = 10 * 1024 * 1024

IMAGE_TYPES = {
    "image/gif", "image/heic", "image/jpeg", "image/png", "image/svg+xml", "image/tiff",
    "image/webp", "image/vnd.microsoft.icon", "image/bmp", "image/avif", "image/apng",
}
_EXTRA_TYPES = {".heic": "image/heic", ".webp": "image/webp", ".avif": "image/avif",
                ".md": "text/markdown", ".oga": "audio/ogg", ".ogg": "audio/ogg"}

_ID_RE = re.compile(r"([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})")


class NotionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    def human(self) -> str:
        if self.status == 401:
            return "Notion не принял ключ NOTION_TOKEN (неверный или отозван)."
        if self.status == 404 or self.code == "object_not_found":
            return ("Notion не нашёл страницу или у интеграции нет к ней доступа. В Notion на этой "
                    "странице (или родительской): ⋯ → Connections → добавить интеграцию.")
        if self.status == 403:
            return "У интеграции Notion нет прав на изменение этой страницы."
        if self.status == 429:
            return "Notion временно ограничил частоту запросов — повторите чуть позже."
        return f"Notion вернул ошибку {self.status} ({self.code}): {self.message[:300]}"


def parse_page_id(value: str) -> str | None:
    """ID страницы из ссылки Notion или из самого ID (с дефисами или без)."""
    if not isinstance(value, str):
        return None
    matches = _ID_RE.findall(value.split("?")[0].split("#")[0])
    if not matches:
        return None
    h = matches[-1].replace("-", "").lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def guess_content_type(filename: str) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return _EXTRA_TYPES.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def block_type_for(content_type: str) -> str:
    if content_type in IMAGE_TYPES:
        return "image"
    if content_type == "application/pdf":
        return "pdf"
    return "file"


def rich_text(text: str) -> list[dict]:
    # rich text в Notion — куски до 2000 символов
    text = text[:6000]
    return [{"type": "text", "text": {"content": text[i:i + 2000]}} for i in range(0, len(text), 2000)] or []


def page_url(page_id: str) -> str:
    return "https://www.notion.so/" + page_id.replace("-", "")


class NotionClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=API, timeout=httpx.Timeout(300.0, connect=20.0), transport=transport,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
        )

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            raise NotionError(r.status_code, str(body.get("code", "error")), str(body.get("message", r.text)))
        return r.json() if r.content else {}

    def upload(self, data: bytes, filename: str, content_type: str) -> str:
        """Загружает файл и возвращает id file_upload (прикрепить нужно в течение часа)."""
        if len(data) <= SINGLE_PART_MAX:
            up = self._req("POST", "/file_uploads", json={"filename": filename, "content_type": content_type})
            self._req("POST", f"/file_uploads/{up['id']}/send",
                      files={"file": (filename, data, content_type)})
            return up["id"]
        parts = math.ceil(len(data) / PART_SIZE)
        up = self._req("POST", "/file_uploads", json={
            "mode": "multi_part", "number_of_parts": parts,
            "filename": filename, "content_type": content_type})
        for i in range(parts):
            chunk = data[i * PART_SIZE:(i + 1) * PART_SIZE]
            self._req("POST", f"/file_uploads/{up['id']}/send",
                      files={"file": (filename, chunk, content_type)}, data={"part_number": str(i + 1)})
        self._req("POST", f"/file_uploads/{up['id']}/complete")
        return up["id"]

    def get_page(self, page_id: str) -> dict:
        return self._req("GET", f"/pages/{page_id}")

    def append_file_block(self, page_id: str, upload_id: str, content_type: str,
                          filename: str, caption: str) -> str | None:
        kind = block_type_for(content_type)
        body: dict = {"type": "file_upload", "file_upload": {"id": upload_id}, "caption": rich_text(caption)}
        if kind == "file":
            body["name"] = filename
        res = self._req("PATCH", f"/blocks/{page_id}/children",
                        json={"children": [{"object": "block", "type": kind, kind: body}]})
        results = res.get("results") or []
        return results[0].get("id") if results else None

    def set_files_property(self, page_id: str, prop: str, files: list[dict]) -> None:
        self._req("PATCH", f"/pages/{page_id}", json={"properties": {prop: {"files": files}}})
