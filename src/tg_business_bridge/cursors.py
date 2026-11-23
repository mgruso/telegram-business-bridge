"""Курсоры («закладки») агентов: что в каждом чате уже разобрано.

Курсор — пара (consumer, chat_id) → last_processed_id: внутренний монотонный id
строки messages (НЕ время и НЕ Telegram message_id). Всё с id > курсора — новое.
Отдельно хранится last_event_id — номер последнего учтённого события из
message_events (правки/удаления): правка уже разобранного сообщения снова
попадает в выдачу, пока агент не подтвердит её через mark_processed.

Сообщения никогда не удаляются — курсор лишь отмечает границу.
"""
import sqlite3
import time
from dataclasses import dataclass


class CursorError(ValueError):
    """Понятная агенту ошибка движения курсора."""


@dataclass
class Cursor:
    last_id: int
    last_event_id: int
    stored: bool  # False — курсора ещё нет, значения вычислены от начала/CURSOR_START_ISO


def position_at(conn: sqlite3.Connection, chat_id: int, ts: int | None) -> tuple[int, int]:
    """(id, event_id) «закладки на момент ts»: всё строго раньше ts считается разобранным.
    ts=None — начало архива (0, 0)."""
    if ts is None:
        return 0, 0
    mid = conn.execute(
        "SELECT COALESCE(max(id), 0) FROM messages WHERE chat_id=? AND ts < ?", (chat_id, ts)
    ).fetchone()[0]
    eid = conn.execute(
        "SELECT COALESCE(max(id), 0) FROM message_events WHERE chat_id=? AND ts < ?", (chat_id, ts)
    ).fetchone()[0]
    return mid, eid


def get_cursor(conn: sqlite3.Connection, consumer: str, chat_id: int, start_ts: int | None) -> Cursor:
    row = conn.execute(
        "SELECT last_processed_id, last_event_id FROM cursors WHERE consumer=? AND chat_id=?",
        (consumer, chat_id),
    ).fetchone()
    if row is not None:
        return Cursor(row[0], row[1], True)
    mid, eid = position_at(conn, chat_id, start_ts)
    return Cursor(mid, eid, False)


def _save(conn: sqlite3.Connection, consumer: str, chat_id: int, last_id: int, last_event_id: int) -> None:
    conn.execute(
        """INSERT INTO cursors (consumer, chat_id, last_processed_id, last_event_id, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(consumer, chat_id) DO UPDATE SET
             last_processed_id=excluded.last_processed_id,
             last_event_id=excluded.last_event_id,
             updated_at=excluded.updated_at""",
        (consumer, chat_id, last_id, last_event_id, int(time.time())),
    )
    conn.commit()


def max_message_id(conn: sqlite3.Connection, chat_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(max(id), 0) FROM messages WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]


def max_event_id(conn: sqlite3.Connection, chat_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(max(id), 0) FROM message_events WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]


def new_messages(conn: sqlite3.Connection, chat_id: int, after_id: int, limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM messages WHERE chat_id=? AND id > ? ORDER BY id"
    params: list = [chat_id, after_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_new(conn: sqlite3.Connection, chat_id: int, after_id: int) -> tuple[int, int | None, int | None]:
    """(сколько, ts самого старого, ts самого нового) среди сообщений с id > after_id."""
    r = conn.execute(
        "SELECT count(*), min(ts), max(ts) FROM messages WHERE chat_id=? AND id > ?",
        (chat_id, after_id),
    ).fetchone()
    return r[0], r[1], r[2]


def pending_edits(conn: sqlite3.Connection, chat_id: int, cursor: Cursor) -> list[dict]:
    """Правки/удаления УЖЕ разобранных сообщений (id <= курсора), произошедшие после
    last_event_id. По одному элементу на сообщение (последнее событие), по порядку
    последнего события. Правки ещё не разобранных сообщений сюда не входят — эти
    сообщения и так придут целиком, с актуальным текстом и пометкой «(изменено)»."""
    rows = conn.execute(
        """SELECT e.id AS event_id, e.kind AS event_kind, e.ts AS event_ts, m.*
           FROM message_events e
           JOIN messages m ON m.id = (
               SELECT id FROM messages WHERE chat_id = e.chat_id AND message_id = e.message_id
               ORDER BY id DESC LIMIT 1)
           WHERE e.chat_id = ? AND e.id > ? AND e.kind IN ('edited', 'deleted') AND m.id <= ?
           ORDER BY e.id""",
        (chat_id, cursor.last_event_id, cursor.last_id),
    ).fetchall()
    by_msg: dict[int, dict] = {}
    for r in rows:
        d = dict(r)
        prev = by_msg.get(d["id"])
        if prev is not None:
            # удаление важнее правки; номер события — всегда последний
            d["event_kind"] = "deleted" if "deleted" in (prev["event_kind"], d["event_kind"]) else "edited"
        by_msg[d["id"]] = d
    return sorted(by_msg.values(), key=lambda d: d["event_id"])


def message_flags(conn: sqlite3.Connection, chat_id: int, message_id: int | None) -> set[str]:
    if message_id is None:
        return set()
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT kind FROM message_events WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    ).fetchall()}


def pending_chat_ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT DISTINCT chat_id FROM messages").fetchall()]


def mark_processed(
    conn: sqlite3.Connection, consumer: str, chat_id: int, up_to_id: int,
    edits_up_to: int | None, start_ts: int | None,
) -> Cursor:
    """Сдвигает курсор вперёд. Идемпотентно; назад и дальше реальных данных — ошибка."""
    cur = get_cursor(conn, consumer, chat_id, start_ts)
    if up_to_id < cur.last_id:
        raise CursorError(
            f"курсор не двигается назад: сейчас last_processed_id={cur.last_id}, "
            f"передан up_to_id={up_to_id}. Чтобы разобрать заново — reset_cursor."
        )
    top = max_message_id(conn, chat_id)
    if up_to_id > top:
        raise CursorError(
            f"up_to_id={up_to_id} больше последнего сообщения этого чата (id={top}). "
            "Передавайте up_to_id из ответа get_unprocessed."
        )
    new_event = cur.last_event_id
    if edits_up_to is not None:
        if edits_up_to < cur.last_event_id:
            raise CursorError(
                f"edits_up_to не двигается назад: сейчас {cur.last_event_id}, передан {edits_up_to}."
            )
        top_e = max_event_id(conn, chat_id)
        if edits_up_to > top_e:
            raise CursorError(
                f"edits_up_to={edits_up_to} больше последнего события правок этого чата ({top_e})."
            )
        new_event = edits_up_to
    _save(conn, consumer, chat_id, up_to_id, new_event)
    return Cursor(up_to_id, new_event, True)


def reset_cursor(conn: sqlite3.Connection, consumer: str, chat_id: int, to_ts: int | None) -> Cursor:
    mid, eid = position_at(conn, chat_id, to_ts)
    _save(conn, consumer, chat_id, mid, eid)
    return Cursor(mid, eid, True)
