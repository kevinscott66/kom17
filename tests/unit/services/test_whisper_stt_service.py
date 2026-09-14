"""Unit tests for :class:`WhisperSttService` — httpx ``MockTransport``-stubbed.

No live OpenAI calls. Branch coverage:

* Happy path → 200 with ``{"text": ...}`` → ``ok=True`` stripped text,
  multipart file + ``model=whisper-1`` + ``language`` sent, Bearer auth.
* No key → ``ok=False, error="no_key"`` and NO network call.
* Non-200 → ``ok=False, error="http_<status>"``.
* Network error → ``ok=False, error="network"``.
* Empty transcript → ``ok=False, error="empty"``.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from telegram_invite_bot.services.whisper_stt_service import WhisperSttService

_AUDIO = b"OggS\x00fake-opus-bytes"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_transcribe_happy_path() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read()
        return httpx.Response(200, json={"text": "  Привет мир  "})

    async with _client(handler) as client:
        svc = WhisperSttService("sk-test", client)
        result = await svc.transcribe(_AUDIO, language="ru")

    assert result.ok is True
    assert result.text == "Привет мир"
    assert result.error is None
    assert result.processing_ms >= 0
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer sk-test"
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b"whisper-1" in body
    assert b'name="language"' in body
    assert b"voice.ogg" in body  # multipart filename


async def test_no_key_is_degraded_no_network() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200, json={"text": "x"})

    async with _client(handler) as client:
        svc = WhisperSttService(None, client)
        result = await svc.transcribe(_AUDIO, language="ru")

    assert result.ok is False
    assert result.error == "no_key"
    assert result.processing_ms == 0
    assert called is False  # short-circuit, no HTTP


async def test_non_200_maps_to_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    async with _client(handler) as client:
        svc = WhisperSttService("sk-test", client)
        result = await svc.transcribe(_AUDIO, language="ru")

    assert result.ok is False
    assert result.error == "http_429"


async def test_network_error_maps_to_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client(handler) as client:
        svc = WhisperSttService("sk-test", client)
        result = await svc.transcribe(_AUDIO, language="ru")

    assert result.ok is False
    assert result.error == "network"


async def test_empty_transcript_is_soft_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    async with _client(handler) as client:
        svc = WhisperSttService("sk-test", client)
        result = await svc.transcribe(_AUDIO, language="ru")

    assert result.ok is False
    assert result.error == "empty"
    assert result.text is None


def test_model_used_is_whisper_1() -> None:
    assert WhisperSttService.model_used() == "whisper-1"
