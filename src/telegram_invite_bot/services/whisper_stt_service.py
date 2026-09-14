"""OpenAI Whisper Speech-To-Text client for group voice transcription (L-70).

The legacy monolith ran a LOCAL ``faster-whisper`` model (with optional
DeepSeek text-cleanup). The strangler port instead calls the OpenAI
``/v1/audio/transcriptions`` endpoint with model ``whisper-1``: it accepts
Telegram's ogg/opus voice bytes directly as a multipart file, so there is
NO ffmpeg, NO local model, and NO per-host GPU/CPU knob to carry over.

Degraded posture mirrors the Phase A payment providers: when
``OPENAI_API_KEY`` is absent the service short-circuits to
``SttResult(ok=False, error="no_key")`` WITHOUT any network call, and the
handler silently skips transcription (debug log, no group spam).

The optional ``AsyncClient`` seam follows :class:`AiService` /
:class:`WeatherService`, but NOTHING injects one today: the only prod
construction (``handlers/voice_transcribe.py:597``) passes no client, so
every call opens a short-lived one under ``async with`` and releases its
pool on exit. Tests pass an ``httpx.MockTransport``-backed client.

DeepSeek text-cleanup from legacy is intentionally OUT OF SCOPE here (kept
tight); a possible follow-up if operators want punctuation/formatting
polish on the raw Whisper output.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from loguru import logger

from telegram_invite_bot.utils.http_read import send_capped

log = logger.bind(component="services.whisper_stt")

_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
_MODEL = "whisper-1"


@dataclass(frozen=True, slots=True)
class SttResult:
    """Outcome of a single transcription attempt.

    ``ok`` True → ``text`` holds the (stripped) transcript, ``error`` is
    ``None``. ``ok`` False → ``text`` is ``None`` and ``error`` is a short
    machine code (``"no_key"`` degraded, ``"http_<status>"``, ``"network"``,
    ``"bad_response"``, ``"empty"``). The list is exhaustive and has to
    stay that way: ``"empty"`` — Whisper transcribed silence or noise —
    is the likeliest of the five in practice, and a caller that
    branched over only the first four fell through on it (#1628).
    ``processing_ms`` is the wall-clock spent in this call (0 on the
    no-key short-circuit).
    """

    ok: bool
    text: str | None
    error: str | None
    processing_ms: int


class WhisperSttService:
    """Single-shot OpenAI Whisper caller.

    Constructed per call with no client today — ``client`` is the test
    seam (``httpx.MockTransport``), not prod wiring. Holding the OpenAI
    key as a plain ``str | None`` keeps the degraded check a single
    truthiness test — callers resolve ``settings.openai.api_key`` (a
    ``SecretStr``) to its plaintext at construction.
    """

    def __init__(
        self,
        api_key: str | None,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._timeout = timeout

    async def transcribe(
        self, audio: bytes, *, language: str, filename: str = "voice.ogg"
    ) -> SttResult:
        """Transcribe ``audio`` (Telegram ogg/opus bytes) via Whisper.

        ``language`` is passed as the OpenAI ``language`` hint (ISO-639-1,
        e.g. ``"ru"``/``"en"``). ``filename`` is the multipart filename —
        OpenAI infers the container from the extension, so the default
        ``voice.ogg`` matches Telegram voice notes.

        Degraded (no key) returns immediately with ``ok=False,
        error="no_key"`` and makes NO network call.
        """
        if not self._api_key:
            log.debug("whisper transcribe skipped: OPENAI_API_KEY unset (degraded)")
            return SttResult(ok=False, text=None, error="no_key", processing_ms=0)

        if self._client is not None:
            return await self._transcribe_with(self._client, audio, language, filename)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._transcribe_with(client, audio, language, filename)

    async def _transcribe_with(
        self,
        client: httpx.AsyncClient,
        audio: bytes,
        language: str,
        filename: str,
    ) -> SttResult:
        started = time.monotonic()

        def _elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        files = {"file": (filename, audio, "audio/ogg")}
        data = {"model": _MODEL, "language": language}
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            response = await send_capped(
                client,
                "POST",
                _TRANSCRIPTIONS_URL,
                headers=headers,
                files=files,
                data=data,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            log.warning("whisper HTTP error: {e}", e=exc)
            return SttResult(ok=False, text=None, error="network", processing_ms=_elapsed_ms())

        if response.status_code != 200:
            log.warning("whisper HTTP {s}", s=response.status_code)
            return SttResult(
                ok=False,
                text=None,
                error=f"http_{response.status_code}",
                processing_ms=_elapsed_ms(),
            )

        try:
            payload = response.json()
        except ValueError:
            return SttResult(ok=False, text=None, error="bad_response", processing_ms=_elapsed_ms())

        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            return SttResult(ok=False, text=None, error="bad_response", processing_ms=_elapsed_ms())
        stripped = text.strip()
        if not stripped:
            # Whisper returned an empty transcript (silence / noise). Treat
            # as a soft failure so the handler skips routing rather than
            # posting an empty quote.
            return SttResult(ok=False, text=None, error="empty", processing_ms=_elapsed_ms())
        return SttResult(ok=True, text=stripped, error=None, processing_ms=_elapsed_ms())

    @staticmethod
    def model_used() -> str:
        """The OpenAI model id stored alongside each transcription."""
        return _MODEL
