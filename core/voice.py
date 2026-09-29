"""Voice transcription helpers."""

from __future__ import annotations

from .logging_setup import log
from .security import redact_text


async def transcribe_voice(audio_bytes: bytes, groq_api_key: str) -> str | None:
    """Transcribe audio using Groq's Whisper API. Returns text or None on failure."""
    if not groq_api_key:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_api_key}"},
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                data={"model": "whisper-large-v3-turbo"},
            )
            if response.status_code == 200:
                payload = response.json()
                text = payload.get("text") if isinstance(payload, dict) else None
                return text if isinstance(text, str) else None
    except ImportError:
        log.warning("httpx not installed — voice transcription unavailable. pip install httpx")
    except Exception as e:
        detail = redact_text(str(e), {"GROQ_API_KEY": groq_api_key})
        log.error("Voice transcription failed: %s", detail)

    return None
