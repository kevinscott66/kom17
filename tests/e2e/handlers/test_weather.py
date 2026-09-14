"""End-to-end ``/weather`` flow: dispatcher → handler → mocked Open-Meteo.

Wires a real ``Dispatcher`` with the production main router, then
patches the module-level ``WeatherService`` reference so the handler
talks to an ``httpx.MockTransport``-backed client instead of the real
Open-Meteo. ``Bot.session.make_request`` is intercepted to capture the
outgoing reply without touching Telegram.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 25. The mock-weather httpx client lifecycle still lives in
this file's own fixture — it's specific to the weather port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.handlers import weather as weather_handler
from telegram_invite_bot.routers import main_router as main_router_module
from telegram_invite_bot.services.weather_service import WeatherService
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


@pytest.fixture
async def mock_weather_clients() -> AsyncIterator[list[httpx.AsyncClient]]:
    """Track ``httpx.AsyncClient`` instances created by
    ``_install_mock_weather`` so they get closed even if the test raises.
    """
    cleanup: list[httpx.AsyncClient] = []
    try:
        yield cleanup
    finally:
        for client in cleanup:
            await client.aclose()


def _message_update(text: str, *, chat_type: str = "private") -> Update:
    """File-local defaults: user 555 named ``Bob`` with ``message_id=200``
    (a few weather assertions reach for the message id). Delegates to
    the shared builder.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=555,
        first_name="Bob",
        message_id=200,
    )


def _install_mock_weather(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    cleanup: list[httpx.AsyncClient],
) -> None:
    """Force the weather pipeline to use an ``AsyncClient`` bound to ``handler``.

    M-P-9: the shared ``WeatherService`` is now instantiated at
    ``build_main_router`` time, so we patch the class at the
    instantiation site (``routers.main_router``). Tests MUST call
    this BEFORE ``make_wired`` — once the router is built, the
    service captured in the handler closure is frozen.

    The legacy ``handlers.weather.WeatherService`` patch is kept
    too, for any callers that construct ``build_router(None)`` and
    let the handler module fall back to its own default.
    """
    transport = httpx.MockTransport(handler)

    class _PinnedService(WeatherService):
        def __init__(self) -> None:
            client = httpx.AsyncClient(transport=transport)
            cleanup.append(client)
            super().__init__(client=client)

    monkeypatch.setattr(weather_handler, "WeatherService", _PinnedService)
    monkeypatch.setattr(main_router_module, "WeatherService", _PinnedService)


async def test_weather_replies_with_formatted_report(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Москва",
                            "country": "Россия",
                            "latitude": 55.75,
                            "longitude": 37.62,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 7.4,
                    "apparent_temperature": 4.1,
                    "relative_humidity_2m": 72,
                    "weather_code": 3,
                    "wind_speed_10m": 14.5,
                },
                "daily": {
                    "temperature_2m_min": [3.2],
                    "temperature_2m_max": [10.1],
                },
            },
        )

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _message_update("/weather Москва"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Москва" in body
    assert "Россия" in body
    assert "7°C" in body  # rounded current temperature
    assert "72%" in body  # humidity
    assert "Пасмурно" in body  # WMO 3


async def test_weather_en_user_gets_english_condition_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ru/en convergence: an English user's /weather must render the
    condition in English with NO Cyrillic (the pre-fix bug leaked the
    pre-rendered RU ``_WMO_RU`` label to en users)."""
    import re

    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Berlin",
                            "country": "Germany",
                            "latitude": 52.5,
                            "longitude": 13.4,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 9.0,
                    "relative_humidity_2m": 60,
                    "weather_code": 3,  # Overcast
                    "wind_speed_10m": 10.0,
                },
                "daily": {
                    "temperature_2m_min": [5.0],
                    "temperature_2m_max": [12.0],
                },
            },
        )

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    update = make_message_update(
        "/weather Berlin",
        chat_type="private",
        user_id=556,
        first_name="Alice",
        message_id=201,
        language_code="en",
    )
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Overcast" in body  # WMO 3 in English
    assert "Weather: Berlin" in body
    assert not re.search(r"[А-Яа-яЁё]", body), f"Cyrillic leaked to en user: {body!r}"


async def test_weather_without_args_replies_usage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bare ``/weather`` now replies a usage prompt instead of silently
    no-opping (the legacy ``/city`` saved-default fallback is unported,
    but the strangler fallback bridge is gone)."""
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _message_update("/weather"))
    assert result is not UNHANDLED
    assert "Укажи город" in sent[-1]["text"]


async def test_weather_city_not_found_replies_apology(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/weather Atlantis-XYZ"))
    assert len(sent) == 1
    assert "не нашёл" in sent[0]["text"].lower()


async def test_weather_upstream_error_replies_unavailable(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "bad gateway"})

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/weather Москва"))
    assert len(sent) == 1
    assert "недоступен" in sent[0]["text"].lower()


def _geo_with_region(request: httpx.Request) -> httpx.Response:
    """Geocoder answer that carries an ``admin1`` region."""
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "name": "Краснодар",
                    "admin1": "Краснодарский край",
                    "country": "Россия",
                    "latitude": 45.03,
                    "longitude": 38.97,
                }
            ]
        },
    )


def _daily_payload() -> dict[str, Any]:
    return {
        "daily": {
            "time": ["2026-06-07", "2026-06-08", "2026-06-09"],
            "weather_code": [0, 3, 61],
            "temperature_2m_min": [12.0, 13.5, 11.0],
            "temperature_2m_max": [22.0, 20.5, 18.0],
        }
    }


async def test_weather_title_carries_the_region(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #68: legacy titled the card ``City (region, country)``. The
    region is what tells two same-named cities apart."""

    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return _geo_with_region(request)
        return httpx.Response(
            200,
            json={
                "current": {"temperature_2m": 21.0, "weather_code": 0},
                "daily": {"temperature_2m_min": [14.0], "temperature_2m_max": [26.0]},
            },
        )

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/weather Краснодар"))
    assert "Краснодар (Краснодарский край, Россия)" in sent[0]["text"]


async def test_weather_tomorrow_renders_tomorrows_day(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/weather завтра Сочи`` answers tomorrow — the period word wins
    over the command name (RR-6 #68)."""

    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Сочи",
                            "country": "Россия",
                            "latitude": 43.6,
                            "longitude": 39.73,
                        }
                    ]
                },
            )
        return httpx.Response(200, json=_daily_payload())

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/weather завтра Сочи"))
    body = sent[0]["text"]
    assert "Завтра" in body
    assert "08-06" in body  # day[1], DD-MM for ru
    assert "Пасмурно" in body  # WMO 3 — day[1]'s code, not day[0]'s
    # Not the today card.
    assert "Влажность" not in body


async def test_weather_multi_day_renders_the_forecast_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/weather на 3 дня в Сочи`` — same card ``/forecast`` renders."""

    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Сочи",
                            "country": "Россия",
                            "latitude": 43.6,
                            "longitude": 39.73,
                        }
                    ]
                },
            )
        return httpx.Response(200, json=_daily_payload())

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/weather на 3 дня в Сочи"))
    body = sent[0]["text"]
    assert "Прогноз" in body
    assert body.count("•") == 3


async def test_plain_text_weather_question_answers(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #68: a slash-less question in a DM must answer, and the
    oblique-case city name must still geocode (the live «погода в
    Москве» bug)."""
    seen: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            name = request.url.params.get("name", "")
            seen.append(name)
            if name != "Москва":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Москва",
                            "country": "Россия",
                            "latitude": 55.75,
                            "longitude": 37.62,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "current": {"temperature_2m": 7.4, "weather_code": 3},
                "daily": {"temperature_2m_min": [3.2], "temperature_2m_max": [10.1]},
            },
        )

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _message_update("какая погода в Москве"))
    assert result is not UNHANDLED
    assert "Москва" in sent[-1]["text"]
    # Raw spelling first, nominative only as a fallback.
    assert seen[0] == "Москве"
    assert "Москва" in seen


async def test_weather_works_in_group_chat(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike ``/start``, weather is allowed in groups."""

    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Berlin",
                            "country": "Germany",
                            "latitude": 52.5,
                            "longitude": 13.4,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "current": {"temperature_2m": 12.0, "weather_code": 0},
                "daily": {},
            },
        )

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _message_update("/погода Berlin", chat_type="group"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Berlin" in sent[0]["text"]


async def test_weather_renders_a_dash_when_the_reading_is_missing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap in the current temperature is a gap, not a freezing day (#422).

    It used to degrade to ``0.0`` and print as a real measurement — a
    number indistinguishable from an actual zero. Every other reading on
    the card already collapses to a dash; this one now does too, which
    is also what legacy did (``bot.py:37597`` printed «н/д»).
    """

    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Москва",
                            "country": "Россия",
                            "latitude": 55.75,
                            "longitude": 37.62,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "current": {"relative_humidity_2m": 72, "weather_code": 3},
                "daily": {},
            },
        )

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _message_update("/weather Москва"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "0°C" not in body, body
    assert "—" in body, body
    assert "72%" in body, body


def _senderless_weather_update(text: str, *, update_id: int = 900) -> Update:
    """A message posted on behalf of a channel: ``sender_chat``, no ``from``.

    This is the shape both rate limiters wave through
    (``_bucket_rate_limit.py:139-143``, ``throttling.py:229-231``), so
    ``F.from_user`` on the registration is the only thing standing
    between it and the upstream geocoder (#489).
    """
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": -100, "type": "supergroup", "title": "G"},
                "sender_chat": {"id": -777, "type": "channel", "title": "News"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
            },
        }
    )


@pytest.mark.parametrize("command", ["/weather Москва", "/forecast Москва на 3 дня"])
async def test_anonymous_channel_post_never_reaches_the_geocoder(
    make_wired: WiredFactory,
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """#489: no ``from_user`` → no handler, and no upstream request.

    Legacy closed this on the handler's first line (``bot.py:16944`` →
    ``ensure_user_access`` → ``bot.py:1621``). The port relies on
    ``F.from_user``; without it an unattributable sender could drive
    Open-Meteo's geocoding endpoint with no limit at all.
    """
    calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, json={"results": []})

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()

    result = await dispatcher.feed_update(bot, _senderless_weather_update(command))

    assert result is UNHANDLED
    assert calls == []
