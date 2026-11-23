import logging
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_DEFAULT_MODEL = "whisper-large-v3-turbo"
_TIMEOUT = aiohttp.ClientTimeout(total=60)

_CONTENT_TYPES = {
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
}


def _content_type(path: str) -> str:
    return _CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


async def transcribe_file(path: str, api_key: str) -> str | None:
    """Распознаёт речь в аудиофайле через Deepgram. Best-effort: любая ошибка -> None, без исключений."""
    try:
        audio_bytes = Path(path).read_bytes()
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                DEEPGRAM_URL,
                params={"model": "nova-3", "language": "multi", "smart_format": "true"},
                headers={
                    "Authorization": f"Token {api_key}",
                    "Content-Type": _content_type(path),
                },
                data=audio_bytes,
            ) as resp:
                if resp.status != 200:
                    log.warning("deepgram transcription failed for %s: %s %s", path, resp.status, await resp.text())
                    return None
                data = await resp.json()
        transcript = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        return transcript or None
    except (OSError, aiohttp.ClientError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as e:
        log.warning("deepgram transcription error for %s: %s", path, e)
        return None


async def transcribe_groq(path: str, api_key: str, model: str = GROQ_DEFAULT_MODEL) -> str | None:
    """Распознаёт речь через Groq (Whisper, OpenAI-совместимый API). Best-effort: ошибка -> None."""
    try:
        p = Path(path)
        audio_bytes = p.read_bytes()
        # Голосовые Telegram — Ogg/Opus с расширением .oga, которое Groq не принимает;
        # содержимое то же, поэтому отправляем под именем .ogg
        name = p.stem + (".ogg" if p.suffix.lower() == ".oga" else p.suffix)
        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename=name, content_type=_content_type(path))
        form.add_field("model", model or GROQ_DEFAULT_MODEL)
        form.add_field("response_format", "json")
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                GROQ_URL, headers={"Authorization": f"Bearer {api_key}"}, data=form,
            ) as resp:
                if resp.status != 200:
                    log.warning("groq transcription failed for %s: %s %s", path, resp.status, await resp.text())
                    return None
                data = await resp.json()
        text = data.get("text") if isinstance(data, dict) else None
        return text.strip() or None if isinstance(text, str) else None
    except (OSError, aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as e:
        log.warning("groq transcription error for %s: %s", path, e)
        return None


def enabled(settings) -> bool:
    return bool(getattr(settings, "groq_api_key", "") or getattr(settings, "deepgram_api_key", ""))


async def transcribe(settings, path: str) -> str | None:
    """Groq, если задан BRIDGE_GROQ_API_KEY, иначе Deepgram; без ключей — None."""
    if getattr(settings, "groq_api_key", ""):
        return await transcribe_groq(path, settings.groq_api_key, getattr(settings, "groq_model", "") or GROQ_DEFAULT_MODEL)
    if getattr(settings, "deepgram_api_key", ""):
        return await transcribe_file(path, settings.deepgram_api_key)
    return None
