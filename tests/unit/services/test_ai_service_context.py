"""Unit tests for ``AiService.ask_with_context`` + ``AiResponseCache`` (L-65)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import AiConfig
from telegram_invite_bot.services.ai_service import (
    AiRequestError,
    AiResponseCache,
    AiService,
)


def _config(api_key: str | None = "sk-test") -> AiConfig:
    return AiConfig(DEEPSEEK_API_KEY=SecretStr(api_key) if api_key else None)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok(content: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


async def test_ask_with_context_sends_system_history_and_user() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with _client(handler) as client:
        service = AiService(_config(), client)
        answer = await service.ask_with_context(
            "новый вопрос",
            system_prompt="БАЗА",
            history=[{"role": "user", "content": "прошлый"}],
            extra_system="\n\n[REPLY TARGET]",
            max_tokens=2000,
        )

    assert answer == "ok"
    body = captured["body"]
    assert isinstance(body, dict)
    messages = body["messages"]
    # system (with extra appended) + 1 history turn + new user turn.
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].endswith("[REPLY TARGET]")
    assert messages[0]["content"].startswith("БАЗА")
    assert messages[1] == {"role": "user", "content": "прошлый"}
    assert messages[-1] == {"role": "user", "content": "новый вопрос"}
    assert body["max_tokens"] == 2000


async def test_ask_with_context_401_raises_with_the_status() -> None:
    """#1597: the 401 used to come back as an answer naming the key.

    It reaches the handler as an exception now, and the handler is
    the layer that decides the reader is told "unavailable, ask the
    administrator" while the owner gets the status in the log.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    async with _client(handler) as client:
        service = AiService(_config(), client)
        with pytest.raises(AiRequestError) as caught:
            await service.ask_with_context("q", system_prompt="s")
    assert caught.value.reason == "http"
    assert caught.value.status == 401


async def test_ask_with_context_timeout_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    async with _client(handler) as client:
        service = AiService(_config(), client)
        with pytest.raises(AiRequestError) as caught:
            await service.ask_with_context("q", system_prompt="s")
    assert caught.value.reason == "timeout"


def test_response_cache_hit_and_user_scope() -> None:
    cache = AiResponseCache()
    cache.put(1, "Привет", "ответ")
    assert cache.get(1, "привет") == "ответ"  # case-insensitive key
    # Different user → miss (key includes user id).
    assert cache.get(2, "привет") is None


def test_response_cache_eviction() -> None:
    cache = AiResponseCache(max_entries=2)
    cache.put(1, "a", "1")
    cache.put(1, "b", "2")
    cache.put(1, "c", "3")  # evicts "a"
    assert cache.get(1, "a") is None
    assert cache.get(1, "b") == "2"
    assert cache.get(1, "c") == "3"


def test_response_cache_ttl_expiry() -> None:
    cache = AiResponseCache(ttl_seconds=0.0)
    cache.put(1, "a", "1")
    # TTL of 0 means anything older than now is stale.
    assert cache.get(1, "a") is None
