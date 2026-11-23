#!/usr/bin/env python3
"""Конвертер экспорта Telegram Desktop (result.json) в компактный текст.

Работает только с локальным файлом, ничего не отправляет в интернет.
Нужен только Python 3.10+ (без дополнительных пакетов).

Запуск:
    python tg_export_to_text.py путь/к/result.json [--until 2026-11-21] [--out папка]

Формат строк — как у get_unprocessed в мосте:
    [msg=4411 | 2026-03-05T14:05+01:00 чт] Имя (tg:777): текст
Время берётся из экспорта (местное время компьютера, где делали экспорт),
смещение от UTC вычисляется по полю date_unixtime.
Результат режется по месяцам, большие месяцы — на части до --max-chars символов.
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MEDIA_LABEL = {
    "voice_message": "голосовое", "video_message": "видеокружок", "audio_file": "аудио",
    "video_file": "видео", "animation": "GIF", "sticker": "стикер",
}
SERVICE_LABEL = {
    "invite_members": "в группу добавлены участники", "remove_members": "участник удалён",
    "join_group_by_link": "участник вошёл по ссылке", "create_group": "группа создана",
    "edit_group_title": "изменено название группы", "edit_group_photo": "изменено фото группы",
    "pin_message": "закреплено сообщение", "phone_call": "звонок", "group_call": "групповой звонок",
    "migrate_to_supergroup": "группа преобразована", "migrate_from_group": "группа преобразована",
}


def plain_text(t) -> str:
    """Поле text бывает строкой или списком кусков (строки и словари с type/text)."""
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        parts = []
        for piece in t:
            if isinstance(piece, str):
                parts.append(piece)
            elif isinstance(piece, dict):
                txt = piece.get("text", "")
                if piece.get("type") == "text_link" and piece.get("href") and piece.get("href") != txt:
                    txt = f"{txt} ({piece['href']})"
                parts.append(txt if isinstance(txt, str) else "")
        return "".join(parts)
    return ""


def local_dt(msg: dict) -> datetime | None:
    """Дата сообщения с правильным смещением: местное время экспорта + разница с UTC."""
    raw = msg.get("date")
    if not isinstance(raw, str):
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        return None
    unix = msg.get("date_unixtime")
    try:
        utc = datetime.fromtimestamp(int(unix), tz=timezone.utc).replace(tzinfo=None)
        offset = naive - utc
        minutes = round(offset.total_seconds() / 60 / 15) * 15
        return naive.replace(tzinfo=timezone(timedelta(minutes=minutes)))
    except (TypeError, ValueError, OSError, OverflowError):
        return naive


def fmt_dt(dt: datetime) -> str:
    return f"{dt.isoformat(timespec='minutes')} {WEEKDAYS[dt.weekday()]}"


def user_id(from_id) -> str:
    if isinstance(from_id, str):
        m = re.fullmatch(r"(user|channel|chat)(\d+)", from_id)
        if m:
            return m[2] if m[1] == "user" else from_id
        return from_id
    return "?"


def one_line(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def dur(sec) -> str:
    return f" {sec // 60}:{sec % 60:02d}" if isinstance(sec, int) else ""


def format_message(msg: dict, by_id: dict, owner_ids: set, stats: Counter) -> str | None:
    dt = local_dt(msg)
    if dt is None:
        return None
    head = f"[msg={msg.get('id')} | {fmt_dt(dt)}]"
    if msg.get("type") == "service":
        stats["service"] += 1
        label = SERVICE_LABEL.get(msg.get("action"), msg.get("action") or "служебное событие")
        actor = msg.get("actor") or ""
        return f"{head} [служебное: {label}{', ' + actor if actor else ''}]"
    name = msg.get("from") or "?"
    uid = user_id(msg.get("from_id"))
    who = f"{name} (tg:{uid})"
    if uid in owner_ids:
        who = f"[ИСХ владелец] {who}"
    text = plain_text(msg.get("text"))
    marks = ""
    if msg.get("edited"):
        marks += " (изменено)"
    if msg.get("forwarded_from"):
        marks += f" [переслано от {msg['forwarded_from']}]"
    media = msg.get("media_type")
    label = None
    if msg.get("photo"):
        stats["photo"] += 1
        label = "фото"
    elif media in MEDIA_LABEL:
        stats[media] += 1
        label = MEDIA_LABEL[media]
        if media == "sticker" and msg.get("sticker_emoji"):
            label += f" {msg['sticker_emoji']}"
        label += dur(msg.get("duration_seconds")) if media != "sticker" else ""
    elif msg.get("file") or msg.get("file_name"):
        stats["file"] += 1
        label = f"файл: {msg.get('file_name') or 'без имени'}"
    elif msg.get("poll"):
        stats["poll"] += 1
        poll = msg["poll"]
        opts = "; ".join(a.get("text", "") for a in poll.get("answers", []) if isinstance(a, dict))
        label = f"опрос: {poll.get('question', '')} — {opts}"
    elif msg.get("location_information"):
        label = "геопозиция"
    elif msg.get("contact_information"):
        c = msg["contact_information"]
        label = f"контакт: {c.get('first_name', '')} {c.get('last_name', '')} {c.get('phone_number', '')}".strip()
    if label and media == "voice_message":
        body = f"[{label}] расшифровки нет (в экспорте её нет)" + (f" {text}" if text else "")
    elif label:
        body = f"[{label}" + (f", подпись: {text}" if text else "") + "]"
    else:
        body = text
        stats["text"] += 1
    line = f"{head} {who}{marks}: {body}"
    reply = msg.get("reply_to_message_id")
    if reply is not None:
        parent = by_id.get(reply)
        snippet = one_line(plain_text(parent.get("text")), 80) if parent else ""
        line += f"\n    ↳ ответ на msg={reply}" + (f": «{snippet}»" if snippet else "")
    return line


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip(" ._")
    return s[:60] or "chat"


def load_chats(data: dict) -> list[dict]:
    if isinstance(data.get("messages"), list):
        return [data]
    chats = data.get("chats", {})
    lst = chats.get("list") if isinstance(chats, dict) else None
    if isinstance(lst, list):
        return [c for c in lst if isinstance(c, dict) and isinstance(c.get("messages"), list)]
    return []


def convert_chat(chat: dict, until: datetime | None, out_dir: Path, max_chars: int,
                 owner_ids: set) -> dict:
    name = chat.get("name") or f"chat_{chat.get('id')}"
    msgs = [m for m in chat["messages"] if isinstance(m, dict)]
    by_id = {m.get("id"): m for m in msgs}
    stats: Counter = Counter()
    months: dict[str, list[str]] = defaultdict(list)
    first = last = None
    cut = 0
    for m in msgs:
        dt = local_dt(m)
        if dt is None:
            continue
        if until is not None and dt.replace(tzinfo=None) >= until:
            cut += 1
            continue
        line = format_message(m, by_id, owner_ids, stats)
        if line is None:
            continue
        stats["total"] += 1
        first = first or dt
        last = dt
        months[dt.strftime("%Y-%m")].append(line)
    header = f"=== Чат: {name} (экспорт Telegram) ===\n"
    files = []
    base = safe_name(name)
    for month in sorted(months):
        parts, cur, size = [], [], len(header)
        for line in months[month]:
            if cur and size + len(line) + 1 > max_chars:
                parts.append(cur)
                cur, size = [], len(header)
            cur.append(line)
            size += len(line) + 1
        if cur:
            parts.append(cur)
        for i, lines in enumerate(parts, 1):
            path = out_dir / f"{base}_{month}_часть{i}.txt"
            path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
            files.append(path)
    return {"name": name, "stats": stats, "first": first, "last": last, "files": files, "cut": cut}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Экспорт Telegram (result.json) → компактный текст по месяцам.")
    ap.add_argument("result_json", help="путь к result.json из Telegram Desktop")
    ap.add_argument("--until", help="отрезать всё с этой даты включительно (ГГГГ-ММ-ДД), "
                                    "например дату подключения моста")
    ap.add_argument("--out", help="папка для результата (по умолчанию — рядом с result.json)")
    ap.add_argument("--max-chars", type=int, default=60000, help="максимум символов в одном файле")
    ap.add_argument("--owner-id", action="append", default=[],
                    help="ваш Telegram user id — ваши сообщения будут помечены [ИСХ владелец]")
    args = ap.parse_args(argv)

    src = Path(args.result_json)
    if not src.is_file():
        print(f"Не найден файл: {src}")
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"Не получилось прочитать JSON: {e}")
        return 1
    until = None
    if args.until:
        try:
            until = datetime.fromisoformat(args.until)
        except ValueError:
            print("Дата в --until должна быть вида 2026-11-21")
            return 1
    chats = load_chats(data)
    if not chats:
        print("В файле не найдено сообщений. Это точно result.json из «Экспорта истории чата» в формате JSON?")
        return 1
    out_dir = Path(args.out) if args.out else src.parent / "текст_для_ИИ"
    out_dir.mkdir(parents=True, exist_ok=True)

    owner_ids = set(args.owner_id)
    total = Counter()
    all_files = []
    for chat in chats:
        r = convert_chat(chat, until, out_dir, max(5000, args.max_chars), owner_ids)
        s = r["stats"]
        total.update(s)
        all_files += r["files"]
        period = (f"{r['first']:%Y-%m-%d} — {r['last']:%Y-%m-%d}" if r["first"] else "нет сообщений")
        print(f"\nЧат «{r['name']}»: сообщений {s['total']} ({period})")
        print(f"  голосовых: {s['voice_message']}, видеокружков: {s['video_message']}, фото: {s['photo']}, "
              f"файлов: {s['file']}, служебных: {s['service']}")
        if r["cut"]:
            print(f"  отрезано по --until: {r['cut']}")
        print(f"  файлов текста: {len(r['files'])}")
    print(f"\nИтого: сообщений {total['total']}, голосовых {total['voice_message']} "
          f"(их содержание в экспорте не сохраняется — только факт), фото {total['photo']}, "
          f"файлов-вложений {total['file']}.")
    print(f"Готово: {len(all_files)} файл(ов) в папке {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
