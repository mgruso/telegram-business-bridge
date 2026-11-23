"""Один и тот же момент времени выводится одинаково во всех инструментах:
часовой пояс владельца (OWNER_TZ), ISO со смещением и день недели."""
import json
import re

import pytest

from tg_business_bridge import db, mcp_server

CHAT = 777
# 2026-11-23 14:51:00 UTC = 17:51 по Москве, понедельник
TS = 1795445460
EXPECTED = "2026-11-23T17:51+03:00 пн"
OLD_UTC = "2026-11-23 14:51"


@pytest.fixture()
def c(conn, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_conn", lambda: conn)
    monkeypatch.setenv("OWNER_TZ", "Europe/Moscow")
    monkeypatch.delenv("CURSOR_START_ISO", raising=False)
    db.insert_message(conn, dict(
        connection_id="c1", chat_id=CHAT, message_id=10, sender_id=555, sender_name="Глеб",
        ts=TS, direction="in", is_auto=0, media_type="text", text="образцы в пятницу",
        raw_json=json.dumps({}),
    ))
    did = db.create_draft(conn, CHAT, "версия агента", "awaiting")
    conn.execute("UPDATE drafts SET created_ts=? WHERE id=?", (TS, did))
    conn.commit()
    db.update_draft_text(conn, did, "версия владельца")
    db.set_draft_status(conn, did, "sent")
    return conn


def test_same_moment_same_text_everywhere(c):
    outputs = {
        "list_chats": mcp_server.list_chats_impl(),
        "get_history": mcp_server.get_history_impl(CHAT),
        "search_messages": mcp_server.search_messages_impl("образцы"),
        "get_context": mcp_server.get_context_impl(CHAT, 10),
        "list_pending": mcp_server.list_pending_impl("dots"),
        "get_unprocessed": mcp_server.get_unprocessed_impl("dots", CHAT),
        "list_recent_edits": mcp_server.list_recent_edits_impl(),
    }
    for name, out in outputs.items():
        assert EXPECTED in out, f"{name}: {out}"
        assert OLD_UTC not in out, f"{name} всё ещё пишет UTC без смещения: {out}"


def test_no_other_time_formats_leak(c):
    # в выводе не должно остаться «голых» дат без смещения вида 2026-11-23 14:51
    for out in (mcp_server.list_chats_impl(), mcp_server.get_history_impl(CHAT),
                mcp_server.search_messages_impl("образцы"), mcp_server.get_context_impl(CHAT, 10),
                mcp_server.list_recent_edits_impl()):
        assert not re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d", out), out


@pytest.mark.parametrize("from_iso,to_iso,found", [
    ("2026-11-23T17:50", "2026-11-23T17:52", True),            # без смещения = Москва
    ("2026-11-23T14:50", "2026-11-23T14:52", False),           # то же число как Москва — мимо
    ("2026-11-23T14:50+00:00", "2026-11-23T14:52Z", True),     # со смещением UTC
    ("2026-11-23T17:50+03:00", "2026-11-23T17:52+03:00", True),
    ("2026-11-23", None, True),                                # дата без времени — полночь по Москве
])
def test_naive_iso_is_owner_time(c, from_iso, to_iso, found):
    out = mcp_server.get_history_impl(CHAT, from_iso, to_iso)
    assert ("образцы" in out) is found, out
    out = mcp_server.search_messages_impl("образцы", from_iso=from_iso, to_iso=to_iso)
    assert ("образцы" in out) is found, out


def test_utc_when_owner_tz_missing(c, monkeypatch):
    monkeypatch.delenv("OWNER_TZ")
    assert "2026-11-23T14:51+00:00 пн" in mcp_server.get_history_impl(CHAT)
