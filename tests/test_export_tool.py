import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tg_export_to_text", ROOT / "tools" / "tg_export_to_text.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

BASE = 1772700000  # 2026-03-05 ~ 08:40 UTC


def msg(i, text="привет", ts=None, **extra):
    ts = BASE + i * 60 if ts is None else ts
    # экспорт пишет местное время компьютера; эмулируем UTC+1
    from datetime import datetime, timedelta, timezone
    local = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=1)
    d = {"id": i, "type": "message", "date": local.strftime("%Y-%m-%dT%H:%M:%S"),
         "date_unixtime": str(ts), "from": "Ли Мэй", "from_id": "user777", "text": text}
    d.update(extra)
    return d


def run(tmp_path, data, *args):
    src = tmp_path / "result.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    assert tool.main([str(src), "--out", str(out), *args]) == 0
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(out.iterdir())}


def test_formats_everything(tmp_path, capsys):
    data = {"name": "Ли Мэй", "type": "personal_chat", "id": 777, "messages": [
        msg(1, ["Цена ", {"type": "bold", "text": "10$"}, " за ", {"type": "text_link", "text": "образец", "href": "https://x.y"}]),
        msg(2, "ок, жду", from_="Паша", reply_to_message_id=1),
        msg(3, "", media_type="voice_message", duration_seconds=42, file="(File not included)"),
        msg(4, "образец", photo="(File not included)"),
        msg(5, "", file="(File not included)", file_name="прайс.pdf"),
        msg(6, "", media_type="sticker", sticker_emoji="👍"),
        {"id": 7, "type": "service", "date": "2026-03-05T10:00:00", "date_unixtime": str(BASE + 3600),
         "actor": "Паша", "action": "invite_members"},
    ]}
    data["messages"][1]["from_id"] = "user42"
    files = run(tmp_path, data, "--owner-id", "42")
    assert list(files) == ["Ли_Мэй_2026-03_часть1.txt"]
    t = files["Ли_Мэй_2026-03_часть1.txt"]
    assert "Цена 10$ за образец (https://x.y)" in t
    assert "+01:00 чт]" in t  # 5 марта 2026 — четверг, смещение из date_unixtime
    assert "(tg:777)" in t and "[ИСХ владелец]" in t
    assert "↳ ответ на msg=1: «Цена 10$ за образец (https://x.y)»" in t
    assert "[голосовое 0:42] расшифровки нет" in t
    assert "[фото, подпись: образец]" in t and "[файл: прайс.pdf]" in t and "[стикер 👍]" in t
    assert "[служебное: в группу добавлены участники, Паша]" in t
    out = capsys.readouterr().out
    assert "сообщений 7" in out and "голосовых: 1" in out and "фото: 1" in out


def test_until_and_split(tmp_path):
    long = "x" * 3000
    msgs = [msg(i, long) for i in range(1, 6)]  # март
    msgs.append(msg(100, "апрель", ts=BASE + 40 * 86400))
    msgs.append(msg(101, "после моста", ts=BASE + 300 * 86400))
    files = run(tmp_path, {"name": "Фабрика: Ли", "messages": msgs}, "--until", "2026-12-01", "--max-chars", "5000")
    names = list(files)
    assert "Фабрика_Ли_2026-03_часть1.txt" in names and "Фабрика_Ли_2026-03_часть3.txt" in names
    assert "Фабрика_Ли_2026-04_часть1.txt" in names
    assert all(len(t) <= 5000 for t in files.values())
    joined = "".join(files.values())
    assert joined.count(long) == 5 and "после моста" not in joined


def test_full_account_export(tmp_path):
    data = {"chats": {"list": [{"name": "A", "messages": [msg(1, "a")]},
                               {"name": "B", "messages": [msg(1, "b")]}]}}
    assert set(run(tmp_path, data)) == {"A_2026-03_часть1.txt", "B_2026-03_часть1.txt"}
