import json
import re
import sqlite3

import pytest

from tg_business_bridge import db, mcp_server
from tg_business_bridge.mcp_server import (
    get_unprocessed_impl, list_pending_impl, mark_processed_impl, reset_cursor_impl,
)

CHAT = 777
GROUP = -100500


@pytest.fixture()
def c(conn, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_conn", lambda: conn)
    monkeypatch.setenv("OWNER_TZ", "Europe/Madrid")
    monkeypatch.delenv("CURSOR_START_ISO", raising=False)
    return conn


def add(conn, chat_id=CHAT, mid=1, text="привет", ts=1_800_000_000, direction="in",
        sender_id=555, sender_name="Ли Мэй", media_type="text", raw=None):
    return db.insert_message(conn, dict(
        connection_id="c1", chat_id=chat_id, message_id=mid, sender_id=sender_id,
        sender_name=sender_name, ts=ts, direction=direction, is_auto=0,
        media_type=media_type, text=text, raw_json=json.dumps(raw or {}),
    ))


def footer(out: str, chat_id: int = CHAT) -> dict:
    m = re.search(rf"chat_id={chat_id}: up_to_id=(\d+) edits_up_to=(\d+) has_more=(true|false)", out)
    assert m, out
    return {"up_to_id": int(m[1]), "edits_up_to": int(m[2]), "has_more": m[3] == "true"}


def test_new_messages_then_mark(c):
    ids = [add(c, mid=i, text=f"сообщение {i}", ts=1_800_000_000 + i) for i in range(1, 4)]
    assert f"chat_id={CHAT}" in list_pending_impl("dots")
    out = get_unprocessed_impl("dots", CHAT)
    assert all(f"id={i} " in out for i in ids)
    assert "<<<UNTRUSTED>сообщение 1</UNTRUSTED>>>" in out
    assert "(tg:555)" in out
    assert "+01:00" in out or "+02:00" in out  # время в часовом поясе владельца
    f = footer(out)
    assert f == {"up_to_id": ids[-1], "edits_up_to": 0, "has_more": False}
    # get_unprocessed курсор не двигает
    assert footer(get_unprocessed_impl("dots", CHAT))["up_to_id"] == ids[-1]
    res = mark_processed_impl("dots", CHAT, f["up_to_id"], f["edits_up_to"])
    assert res.startswith("OK") and "Осталось неразобранных сообщений: 0" in res
    assert "Неразобранного нет" in get_unprocessed_impl("dots", CHAT)
    assert "Неразобранного нет" in list_pending_impl("dots")
    new_id = add(c, mid=4, text="ещё", ts=1_800_000_100)
    out = get_unprocessed_impl("dots", CHAT)
    assert f"id={new_id} " in out and f"id={ids[0]} " not in out


def test_batches_with_has_more(c):
    ids = [add(c, mid=i, ts=1_800_000_000 + i) for i in range(1, 8)]
    out = get_unprocessed_impl("dots", CHAT, max_messages=3)
    f = footer(out)
    assert f["up_to_id"] == ids[2] and f["has_more"] is True
    assert "осталось новых: 4" in out
    mark_processed_impl("dots", CHAT, f["up_to_id"])
    out = get_unprocessed_impl("dots", CHAT, max_messages=3)
    assert f"id={ids[3]} " in out and f"id={ids[2]} " not in out
    f = footer(out)
    mark_processed_impl("dots", CHAT, f["up_to_id"])
    f = footer(get_unprocessed_impl("dots", CHAT, max_messages=3))
    assert f["up_to_id"] == ids[-1] and f["has_more"] is False


def test_mark_idempotent(c):
    i = add(c)
    assert mark_processed_impl("dots", CHAT, i).startswith("OK")
    assert mark_processed_impl("dots", CHAT, i).startswith("OK")
    assert c.execute("SELECT count(*) FROM cursors").fetchone()[0] == 1


def test_mark_never_backwards_and_not_past_end(c):
    a = add(c, mid=1)
    b = add(c, mid=2)
    mark_processed_impl("dots", CHAT, b)
    err = mark_processed_impl("dots", CHAT, a)
    assert err.startswith("Ошибка") and "назад" in err
    err = mark_processed_impl("dots", CHAT, b + 100)
    assert err.startswith("Ошибка") and "больше последнего" in err
    assert c.execute("SELECT last_processed_id FROM cursors").fetchone()[0] == b


def test_two_consumers_independent(c):
    ids = [add(c, mid=i, ts=1_800_000_000 + i) for i in range(1, 4)]
    mark_processed_impl("dots", CHAT, ids[-1])
    assert "Неразобранного нет" in get_unprocessed_impl("dots", CHAT)
    out = get_unprocessed_impl("claude", CHAT)
    assert all(f"id={i} " in out for i in ids)


def test_max_chars_never_cuts_message(c):
    ids = [add(c, mid=i, text="x" * 900, ts=1_800_000_000 + i) for i in range(1, 11)]
    out = get_unprocessed_impl("dots", CHAT, max_chars=3000)
    assert len(out) <= 3000
    f = footer(out)
    shown = [i for i in ids if f"id={i} " in out]
    assert 0 < len(shown) < len(ids) and f["has_more"] is True
    assert f["up_to_id"] == shown[-1]
    assert out.count("x" * 900) == len(shown)  # каждое показанное — целиком


def test_single_huge_message_still_delivered(c):
    i = add(c, text="y" * 5000)
    out = get_unprocessed_impl("dots", CHAT, max_chars=1000)
    assert "y" * 5000 in out and footer(out)["up_to_id"] == i


def test_all_chats_grouped(c):
    a = add(c, chat_id=CHAT, mid=1, ts=1_800_000_000)
    g = add(c, chat_id=GROUP, mid=1, ts=1_800_000_050, sender_id=9,
            raw={"chat": {"id": GROUP, "type": "supergroup", "title": "Фабрика"}})
    out = get_unprocessed_impl("dots")
    assert out.index(f"chat_id={CHAT} ===") < out.index(f"chat_id={GROUP} ===")
    assert "(группа)" in out and "Фабрика" in out
    assert footer(out, CHAT)["up_to_id"] == a and footer(out, GROUP)["up_to_id"] == g


def test_outgoing_marked_and_media_hints(c):
    add(c, mid=1, direction="out", sender_id=42, sender_name="Паша", text="ок, жду до пятницы",
        raw={"reply_to_message": {"message_id": 9, "text": "когда образцы?"}})
    add(c, mid=2, media_type="photo", text="образец")
    add(c, mid=3, media_type="voice", text=None, raw={"voice": {"duration": 42}})
    add(c, mid=4, media_type="sticker", text=None, raw={"sticker": {"emoji": "👍"}})
    add(c, mid=5, media_type="other", text=None, raw={"new_chat_members": [{"id": 1}]})
    out = get_unprocessed_impl("dots", CHAT)
    assert "[ИСХ владелец] <<<UNTRUSTED>Паша</UNTRUSTED>>> (tg:42)" in out
    assert "↳ ответ на msg=9: <<<UNTRUSTED>когда образцы?</UNTRUSTED>>>" in out
    assert "[фото, подпись: <<<UNTRUSTED>образец</UNTRUSTED>>>] (read_media: chat_id=777, message_id=2)" in out
    assert "[голосовое 0:42] расшифровки нет" in out
    assert "[стикер 👍]" in out
    assert "[служебное: в группу добавлены участники]" in out


def test_edit_after_processing_comes_back(c):
    i = add(c, mid=1, text="цена 10")
    mark_processed_impl("dots", CHAT, i)
    db.add_message_event(c, CHAT, 1, "edited", 1_800_000_500, new_text="цена 12")
    db.set_message_text(c, i, "цена 12")
    assert "правок разобранного: 1" in list_pending_impl("dots")
    out = get_unprocessed_impl("dots", CHAT)
    assert "(изменено после разбора)" in out and "цена 12" in out
    f = footer(out)
    assert f["up_to_id"] == i and f["edits_up_to"] > 0
    mark_processed_impl("dots", CHAT, f["up_to_id"], f["edits_up_to"])
    assert "Неразобранного нет" in get_unprocessed_impl("dots", CHAT)


def test_edit_of_unprocessed_message_marked_inline(c):
    i = add(c, mid=1, text="новая цена")
    db.add_message_event(c, CHAT, 1, "edited", 1_800_000_500, new_text="новая цена")
    out = get_unprocessed_impl("dots", CHAT)
    assert f"id={i} " in out and "(изменено)" in out and "после разбора" not in out


def test_cursor_start_iso(c, monkeypatch):
    add(c, mid=1, ts=1_700_000_000)  # 2023
    new = add(c, mid=2, ts=1_800_000_000)  # 2027
    monkeypatch.setenv("CURSOR_START_ISO", "2025-01-01T00:00:00+00:00")
    out = get_unprocessed_impl("dots", CHAT)
    assert f"id={new} " in out and "id=1 " not in out
    assert "новых: 1" in list_pending_impl("dots")


def test_reset_cursor(c):
    a = add(c, mid=1, ts=1_800_000_000)
    b = add(c, mid=2, ts=1_800_086_400)
    mark_processed_impl("dots", CHAT, b)
    res = reset_cursor_impl("dots", CHAT, "2027-01-15T12:00:00+00:00")  # между a и b
    assert res.startswith("OK")
    out = get_unprocessed_impl("dots", CHAT)
    assert f"id={b} " in out and f"id={a} " not in out
    reset_cursor_impl("dots", CHAT)
    assert f"id={a} " in get_unprocessed_impl("dots", CHAT)


def test_bad_consumer_rejected(c):
    assert get_unprocessed_impl("bad name; drop").startswith("Ошибка")


def test_migration_backs_up_existing_db(tmp_path):
    path = tmp_path / "bridge.db"
    old = db.connect(path)  # «старая» база: обычная схема, но без таблицы cursors
    add(old, text="старое")
    old.execute("DROP TABLE cursors")
    old.commit()
    old.close()
    assert not (tmp_path / "backups").exists()
    conn = db.connect(path)
    conn.close()
    backups = list((tmp_path / "backups").glob("bridge-before-cursors-*.db"))
    assert len(backups) == 1
    b = sqlite3.connect(backups[0])
    assert b.execute("SELECT text FROM messages").fetchone()[0] == "старое"
    b.close()
    conn = db.connect(path)  # повторный запуск — новая копия не нужна
    conn.close()
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
