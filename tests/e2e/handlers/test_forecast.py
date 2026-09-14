"""End-to-end ``/forecast`` flow: dispatcher → handler → mocked Open-Meteo.

Mirrors ``test_weather.py``'s mocking approach: the shared
``WeatherService`` is patched at the ``routers.main_router`` instantiation
site with a ``_PinnedService`` bound to an ``httpx.MockTransport`` so the
handler talks to a fake Open-Meteo. ``capture_outgoing`` intercepts the
reply without touching Telegram.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.handlers import weather as weather_handler
from telegram_invite_bot.routers import main_router as main_router_module
from telegram_invite_bot.services.weather_service import WeatherService
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@pytest.fixture
async def mock_weather_clients() -> AsyncIterator[list[httpx.AsyncClient]]:
    cleanup: list[httpx.AsyncClient] = []
    try:
        yield cleanup
    finally:
        for client in cleanup:
            await client.aclose()


def _install_mock_weather(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    cleanup: list[httpx.AsyncClient],
) -> None:
    transport = httpx.MockTransport(handler)

    class _PinnedService(WeatherService):
        def __init__(self) -> None:
            client = httpx.AsyncClient(transport=transport)
            cleanup.append(client)
            super().__init__(client=client)

    monkeypatch.setattr(weather_handler, "WeatherService", _PinnedService)
    monkeypatch.setattr(main_router_module, "WeatherService", _PinnedService)


def _daily_payload() -> dict[str, Any]:
    return {
        "daily": {
            "time": ["2026-06-07", "2026-06-08", "2026-06-09"],
            "weather_code": [0, 3, 61],
            "temperature_2m_min": [12.0, 13.5, 11.0],
            "temperature_2m_max": [22.0, 20.5, 18.0],
        }
    }


def _geo(name: str, country: str, lat: float, lon: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": [{"name": name, "country": country, "latitude": lat, "longitude": lon}]},
    )


async def test_forecast_ru_multi_day_render(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return _geo("Москва", "Россия", 55.75, 37.62)
        return httpx.Response(200, json=_daily_payload())

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    update = make_message_update("/forecast Москва", user_id=600, message_id=300)
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Москва" in body
    assert "Прогноз" in body
    assert "Ясно" in body  # WMO 0 (ru)
    assert "Пасмурно" in body  # WMO 3 (ru)
    # Three per-day bullet lines.
    assert body.count("•") == 3
    assert "07-06" in body  # DD-MM for ru


async def test_forecast_en_render_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_weather_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return _geo("Berlin", "Germany", 52.5, 13.4)
        return httpx.Response(200, json=_daily_payload())

    _install_mock_weather(monkeypatch, upstream, mock_weather_clients)
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    update = make_message_update(
        "/forecast Berlin", user_id=601, message_id=301, language_code="en"
    )
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Forecast: Berlin" in body
    assert "Clear" in body  # WMO 0 (en)
    assert "Overcast" in body  # WMO 3 (en)
    assert not _CYRILLIC.search(body), f"Cyrillic leaked to en user: {body!r}"


async def test_forecast_without_args_replies_prompt(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    update = make_message_update("/forecast", user_id=602, message_id=302)
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "город" in sent[-1]["text"].lower()


async def test_forecast_city_not_found_replies_error(
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

    update = make_message_update("/forecast Atlantis-XYZ", user_id=603, message_id=303)
    await dispatcher.feed_update(bot, update)
    assert len(sent) == 1
    assert "не нашёл" in sent[0]["text"].lower()
