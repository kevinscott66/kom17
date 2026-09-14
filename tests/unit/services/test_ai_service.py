"""Unit tests for :class:`AiService` — httpx ``MockTransport``-stubbed.

Each branch in the service has a dedicated test:

* Happy path → upstream content returned unchanged.
* Empty/missing API key → :class:`AiNotConfiguredError` (handler maps it).
* Non-200 / timeout / network → :class:`AiRequestError` carrying the
  machine-readable ``reason`` (and ``status`` for HTTP), which the
  handler turns into copy in the reader's language (#1597 — these
  used to be hardcoded Russian sentences returned as the answer).
* Malformed / empty response → the same exception, ``bad_response``
  / ``empty_choices`` / ``empty_content``.

These cover the full surface area Stage 13 ships; no live DeepSeek
calls in CI.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import AiConfig
from telegram_invite_bot.services.ai_service import (
    AiNotConfiguredError,
    AiRequestError,
    AiService,
)


def _config(api_key: str | None = "sk-test") -> AiConfig:
    return AiConfig(DEEPSEEK_API_KEY=SecretStr(api_key) if api_key else None)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ask_returns_content_on_200() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "  Привет!  "}}],
            },
        )

    async with _client(handler) as client:
        service = AiService(_config(), client)
        out = await service.ask("Hello")

    assert out == "Привет!"  # whitespace stripped, exactly like legacy
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    assert b'"messages"' in captured["body"]  # type: ignore[operator]
    assert b'"system"' in captured["body"]  # type: ignore[operator]


async def test_ask_raises_when_api_key_missing() -> None:
    async with _client(lambda _r: httpx.Response(200)) as client:
        service = AiService(_config(api_key=None), client)
        with pytest.raises(AiNotConfiguredError):
            await service.ask("anything")


async def test_ask_raises_on_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated")

    async with _client(handler) as client:
        with pytest.raises(AiRequestError) as caught:
            await AiService(_config(), client).ask("Hello")
    assert caught.value.reason == "timeout"
    assert caught.value.status is None


async def test_ask_raises_on_network_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated dns failure")

    async with _client(handler) as client:
        with pytest.raises(AiRequestError) as caught:
            await AiService(_config(), client).ask("Hello")
    assert caught.value.reason == "network"


@pytest.mark.parametrize("status", [401, 402, 500, 503])
async def test_ask_raises_on_non_200(status: int) -> None:
    """The code has to survive the trip: only the handler can tell
    401/402 (the owner must act) from 5xx (retry later)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream said nope")

    async with _client(handler) as client:
        with pytest.raises(AiRequestError) as caught:
            await AiService(_config(), client).ask("Hello")
    assert caught.value.reason == "http"
    assert caught.value.status == status


async def test_ask_raises_on_no_choices() -> None:
    async with _client(lambda _r: httpx.Response(200, json={"choices": []})) as client:
        with pytest.raises(AiRequestError) as caught:
            await AiService(_config(), client).ask("Hello")
    assert caught.value.reason == "empty_choices"


async def test_ask_raises_on_blank_content() -> None:
    async with _client(
        lambda _r: httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})
    ) as client:
        with pytest.raises(AiRequestError) as caught:
            await AiService(_config(), client).ask("Hello")
    assert caught.value.reason == "empty_content"


async def test_ask_threads_config_through_payload() -> None:
    """``max_tokens`` / ``temperature`` / ``model`` must reach DeepSeek."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.update(_json.loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    config = AiConfig(
        DEEPSEEK_API_KEY=SecretStr("sk-test"),
        DEEPSEEK_MODEL="deepseek-pro",
        DEEPSEEK_MAX_TOKENS=2048,
        DEEPSEEK_TEMPERATURE=0.1,
    )
    async with _client(handler) as client:
        await AiService(config, client).ask("hi")

    assert captured["model"] == "deepseek-pro"
    assert captured["max_tokens"] == 2048
    assert captured["temperature"] == pytest.approx(0.1)


# ---------------------------------------------------------------------
# #1532: malformed 200 bodies
# ---------------------------------------------------------------------
# Every shape below used to escape ``_complete`` as ``AttributeError``
# (``None.strip()``, ``str.get``, ``list.get``) or ``json.JSONDecodeError``
# — and no wrapper catches either, so ``complete_or_none`` broke its own
# "returns ``None`` on failure" contract and ``/quote`` showed the global
# error card AFTER committing the user's daily AI slot. ``content: null``
# is not hypothetical: it is what an OpenAI-compatible endpoint returns
# for a filtered or tool-call-only completion.

_MALFORMED_JSON_BODIES: list[tuple[str, object]] = [
    ("content_is_null", {"choices": [{"message": {"content": None}}]}),
    ("content_is_object", {"choices": [{"message": {"content": {"text": "hi"}}}]}),
    ("message_missing", {"choices": [{}]}),
    ("message_is_null", {"choices": [{"message": None}]}),
    ("choice_is_string", {"choices": ["just a string"]}),
    ("choices_is_object", {"choices": {"0": {"message": {"content": "hi"}}}}),
    ("choices_is_null", {"choices": None}),
    ("top_level_list", [{"message": {"content": "hi"}}]),
    ("top_level_string", "upstream returned a bare string"),
]

_malformed = pytest.mark.parametrize(
    "body",
    [body for _name, body in _MALFORMED_JSON_BODIES],
    ids=[name for name, _body in _MALFORMED_JSON_BODIES],
)

_NON_JSON = "<html><body>502 Bad Gateway (proxy, not DeepSeek)</body></html>"


@_malformed
async def test_ask_raises_on_malformed_body(body: object) -> None:
    async with _client(lambda _r: httpx.Response(200, json=body)) as client:
        with pytest.raises(AiRequestError):
            await AiService(_config(), client).ask("Hello")


@_malformed
async def test_complete_or_none_is_none_on_malformed_body(body: object) -> None:
    """The contract that matters: ``/quote`` must reach its static pool."""
    async with _client(lambda _r: httpx.Response(200, json=body)) as client:
        out = await AiService(_config(), client).complete_or_none("Hi", system_prompt="s")
    assert out is None


@_malformed
async def test_ask_with_context_raises_on_malformed_body(body: object) -> None:
    async with _client(lambda _r: httpx.Response(200, json=body)) as client:
        with pytest.raises(AiRequestError):
            await AiService(_config(), client).ask_with_context("Hi", system_prompt="s")


async def test_ask_raises_on_non_json_body() -> None:
    async with _client(lambda _r: httpx.Response(200, text=_NON_JSON)) as client:
        with pytest.raises(AiRequestError) as caught:
            await AiService(_config(), client).ask("Hello")
    assert caught.value.reason == "bad_response"


async def test_complete_or_none_is_none_on_non_json_body() -> None:
    async with _client(lambda _r: httpx.Response(200, text=_NON_JSON)) as client:
        out = await AiService(_config(), client).complete_or_none("Hi", system_prompt="s")
    assert out is None


async def test_valid_body_still_returns_content() -> None:
    """Guard against the guards: the happy path must survive them."""
    body = {"choices": [{"message": {"content": " ok "}}, {"message": {"content": "second"}}]}
    async with _client(lambda _r: httpx.Response(200, json=body)) as client:
        out = await AiService(_config(), client).complete_or_none("Hi", system_prompt="s")
    assert out == "ok"
