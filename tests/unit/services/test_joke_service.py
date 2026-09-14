"""Unit tests for :mod:`services.joke_service` (RR-6 #72).

The whole point of this service is that it talks to three free, keyless
third parties that owe us nothing. So the properties worth pinning are
about *degradation*, not about happy-path parsing:

* every upstream failure mode returns ``None`` (never raises, never
  produces a user-facing error string) — the caller's local pool is the
  fallback;
* a Russian user never receives untranslated English;
* a hostile or broken payload cannot produce an empty or unbounded
  message.

Everything runs through ``httpx.MockTransport`` — no network.
"""

from __future__ import annotations

import httpx
import pytest

from telegram_invite_bot.services.joke_service import (
    _MAX_CHARS,
    JokeService,
    parse_jokeapi_payload,
)


def _service(handler: object, **kwargs: object) -> JokeService:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return JokeService(client=client, **kwargs)  # type: ignore[arg-type]


def _route(request: httpx.Request) -> str:
    """Which upstream a request is aimed at."""
    host = request.url.host
    if "jokeapi" in host:
        return "jokeapi"
    if "icanhaz" in host:
        return "icanhaz"
    return "lingva"


# --------------------------------------------------------------------
# parse_jokeapi_payload — the two documented response shapes
# --------------------------------------------------------------------


def test_parse_single() -> None:
    assert parse_jokeapi_payload({"type": "single", "joke": " one-liner "}) == "one-liner"


def test_parse_twopart_joins_setup_and_delivery() -> None:
    payload = {"type": "twopart", "setup": "Setup?", "delivery": "Delivery."}
    assert parse_jokeapi_payload(payload) == "Setup?\n\nDelivery."


@pytest.mark.parametrize(
    "payload",
    [
        {"error": True, "message": "no matching joke"},
        {"type": "single", "joke": "   "},
        {"type": "unknown-future-shape"},
        "not a dict",
        None,
    ],
)
def test_parse_rejects_unusable_payloads(payload: object) -> None:
    assert parse_jokeapi_payload(payload) is None


# --------------------------------------------------------------------
# fetch — degradation
# --------------------------------------------------------------------


async def test_fetch_returns_english_joke_for_en_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _route(request) == "jokeapi"
        return httpx.Response(200, json={"type": "single", "joke": "A dad joke."})

    assert await _service(handler).fetch("en") == "A dad joke."


async def test_fetch_translates_for_ru_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _route(request) == "jokeapi":
            return httpx.Response(200, json={"type": "single", "joke": "A dad joke."})
        return httpx.Response(200, json={"translation": "Шутка про папу."})

    assert await _service(handler).fetch("ru") == "Шутка про папу."


async def test_ru_user_never_gets_untranslated_english() -> None:
    """A failing translator degrades to the pool, not to English text.

    Shipping the English original to a Russian speaker is the kind of
    "technically answered" reply that reads as a bug.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if _route(request) == "jokeapi":
            return httpx.Response(200, json={"type": "single", "joke": "A dad joke."})
        return httpx.Response(503)

    assert await _service(handler).fetch("ru") is None


async def test_falls_back_to_icanhaz_when_jokeapi_is_empty() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        route = _route(request)
        seen.append(route)
        if route == "jokeapi":
            return httpx.Response(200, json={"error": True})
        return httpx.Response(200, json={"joke": "Reserve joke."})

    assert await _service(handler, attempts=2).fetch("en") == "Reserve joke."
    # Both JokeAPI attempts, then the reserve — the retry budget is spent
    # before the fallback, never instead of it.
    assert seen == ["jokeapi", "jokeapi", "icanhaz"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(429),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"type": "single", "joke": ""}),
    ],
)
async def test_every_upstream_failure_mode_returns_none(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": response.headers.get("content-type", "text/plain")},
        )

    assert await _service(handler).fetch("en") is None


async def test_transport_exception_returns_none() -> None:
    """A connect error must not escape — a joke is never worth a traceback
    reaching the dispatcher.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable", request=request)

    assert await _service(handler).fetch("en") is None


async def test_disabled_service_short_circuits() -> None:
    """``JOKE_OFFLINE_ONLY`` must not merely fail — it must not call out
    at all, which is the point of an ops kill switch.
    """
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"type": "single", "joke": "nope"})

    assert await _service(handler, enabled=False).fetch("en") is None
    assert not called


async def test_output_is_bounded() -> None:
    """A broken or hostile upstream cannot hand us a message Telegram
    would reject.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "single", "joke": "x" * 99_000})

    text = await _service(handler).fetch("en")
    assert text is not None
    assert len(text) == _MAX_CHARS
    assert text.endswith("...")


async def test_whitespace_only_payload_is_not_a_joke() -> None:
    """Newlines and control characters strip to nothing; an empty message
    is one the Telegram API rejects, so it must degrade to the pool.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if _route(request) == "jokeapi":
            return httpx.Response(200, json={"type": "single", "joke": "\n\n\x00\t \n"})
        return httpx.Response(200, json={"joke": "\x07"})

    assert await _service(handler).fetch("en") is None


async def test_control_characters_are_stripped_from_a_usable_joke() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"type": "single", "joke": "Kn\x00ock\r\nknock\n\n\n\nWho?"}
        )

    assert await _service(handler).fetch("en") == "Knock\nknock\n\nWho?"


async def test_joke_text_cannot_change_the_translator_endpoint() -> None:
    """The joke goes in the URL PATH, so ``/`` and ``?`` in the text must
    be percent-encoded or a punchline could redirect the call.
    """
    paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _route(request) == "jokeapi":
            return httpx.Response(200, json={"type": "single", "joke": "a/b?c=d"})
        # ``raw_path`` is what goes on the wire; ``url.path`` is the
        # percent-DECODED view and would pass this assertion even if the
        # encoding were missing entirely.
        paths.append(request.url.raw_path)
        return httpx.Response(200, json={"translation": "ок"})

    assert await _service(handler).fetch("ru") == "ок"
    assert paths == [b"/api/v1/en/ru/a%2Fb%3Fc%3Dd"]
