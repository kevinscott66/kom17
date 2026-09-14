"""End-to-end ``/time`` (Stage 28).

What this proves:

* All four legacy aliases route — bare command only.
* MSK block is always present, with a HH:MM:SS shape and a Russian
  weekday word. Time-of-day values are NOT asserted (test isn't
  time-sensitive).
* A user with a stored ``timezone`` (set via /timezone — Stage 27) gets
  a second block showing local time, and the tz name is HTML-escaped.
* A user without a stored tz gets only the MSK block (no city geocoding
  — that path isn't ported).
* If the user's stored tz happens to be Europe/Moscow, the second block
  is omitted (would duplicate MSK).
* ``/time London`` geocodes the name and appends that city's clock
  (RR-6 #69); an unknown name and an unreachable geocoder produce two
  different, actionable messages.
* A user with a saved ``/city`` and no stored tz gets their own city's
  clock (legacy branch 3, city half).
* Group calls work (read-only handler; legacy accepts groups too).

The geocoder is always mocked here — a test that reaches Open-Meteo
would be flaky offline and slow online.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update
from sqlalchemy import update

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User as DBUser
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.routers import main_router as main_router_module
from telegram_invite_bot.services.weather_service import WeatherService
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


@pytest.fixture
async def mock_geocoder_clients() -> AsyncIterator[list[httpx.AsyncClient]]:
    """Close every client ``_install_mock_geocoder`` created, even on error."""
    cleanup: list[httpx.AsyncClient] = []
    try:
        yield cleanup
    finally:
        for client in cleanup:
            await client.aclose()


def _install_mock_geocoder(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    cleanup: list[httpx.AsyncClient],
) -> None:
    """Pin the shared ``WeatherService`` to a mock transport.

    Must be called BEFORE ``make_wired``: the service is constructed at
    ``build_main_router`` time and captured in the handler closure.
    """
    transport = httpx.MockTransport(handler)

    class _PinnedService(WeatherService):
        def __init__(self) -> None:
            client = httpx.AsyncClient(transport=transport)
            cleanup.append(client)
            super().__init__(client=client)

    monkeypatch.setattr(main_router_module, "WeatherService", _PinnedService)


def _geo_response(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json=payload)


_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def _update(text: str, *, chat_type: str = "private", user_id: int = 6262) -> Update:
    """File-local defaults: user 6262 named ``Time`` (ru). Delegates to
    the shared builder.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Time",
        language_code="ru",
    )


@pytest.mark.parametrize("alias", ["/time", "/время", "/time_msk", "/kom_time"])
async def test_aliases_render_msk_block(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Московское время" in body
    assert re.search(r"\d{2}:\d{2}:\d{2}", body)
    assert any(wd in body for wd in _WEEKDAYS_RU)


async def test_no_stored_tz_renders_only_msk(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Without /timezone configured, the user-tz block must NOT appear
    (it would otherwise leak a confusing "В твоём часовом поясе ()"
    fragment).
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/time"))
    body = sent[-1]["text"]
    assert "В твоём часовом поясе" not in body


async def test_stored_tz_appends_local_block(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """After /timezone Europe/Berlin, /time must render BOTH blocks."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/timezone Europe/Berlin"))
    sent.clear()
    await dispatcher.feed_update(bot, _update("/time"))
    body = sent[-1]["text"]
    assert "Московское время" in body
    assert "В твоём часовом поясе" in body
    assert "Europe/Berlin" in body
    # Two HH:MM:SS occurrences (MSK + Berlin), not one.
    assert len(re.findall(r"\d{2}:\d{2}:\d{2}", body)) >= 2


async def test_stored_moscow_does_not_duplicate_block(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """If the user explicitly sets their tz to Europe/Moscow, /time
    should NOT show a second identical block. Their choice is still
    honoured — the absence-of-duplication is the proof.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/timezone Europe/Moscow"))
    sent.clear()
    await dispatcher.feed_update(bot, _update("/time"))
    body = sent[-1]["text"]
    assert "В твоём часовом поясе" not in body


async def test_time_with_city_argument_appends_that_citys_clock(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_geocoder_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/time Лондон`` — MSK block plus the named city's clock (RR-6 #69)."""
    _install_mock_geocoder(
        monkeypatch,
        _geo_response(
            {
                "results": [
                    {
                        "name": "Лондон",
                        "country": "Великобритания",
                        "timezone": "Europe/London",
                        "latitude": 51.5,
                        "longitude": -0.12,
                    }
                ]
            }
        ),
        mock_geocoder_clients,
    )
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/time Лондон"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Московское время" in body
    assert "В городе Лондон" in body
    # MSK + London, not one clock.
    assert len(re.findall(r"\d{2}:\d{2}:\d{2}", body)) >= 2


async def test_time_does_not_hold_the_write_lock_across_the_geocoder(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_geocoder_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``touch`` runs before the geocode, and both live on users.db.

    Under ``BEGIN IMMEDIATE`` that bookkeeping write owns the busiest DB
    in the bot until the middleware commits — i.e. for the whole
    geocoder round-trip, which is allowed ten seconds, twice SQLite's
    ``busy_timeout``. Every other update writing users.db would wait out
    the timeout and fail with ``database is locked``. The probe writes
    from a separate session at the exact moment the geocoder would be
    thinking.
    """
    registry_box: list[Any] = []
    other_updates_could_write: list[bool] = []

    async def probing_geocoder(_request: httpx.Request) -> httpx.Response:
        async with registry_box[0].session(DBName.USERS)() as other:
            await other.execute(
                update(DBUser).where(DBUser.user_id == 999).values(messages_count=1)
            )
            await other.commit()
        other_updates_could_write.append(True)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Лондон",
                        "country": "Великобритания",
                        "timezone": "Europe/London",
                        "latitude": 51.5,
                        "longitude": -0.12,
                    }
                ]
            },
        )

    _install_mock_geocoder(monkeypatch, probing_geocoder, mock_geocoder_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    registry_box.append(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/time Лондон"))
    assert result is not UNHANDLED

    assert other_updates_could_write == [True]
    # And the answer is still the real two-clock reply.
    assert "В городе Лондон" in sent[-1]["text"]


async def test_time_with_unknown_city_says_so(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_geocoder_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name the geocoder can't place is a typo the user can fix — say
    that, don't blame the service."""
    _install_mock_geocoder(monkeypatch, _geo_response({"results": []}), mock_geocoder_clients)
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/time Atlantis-XYZ"))
    body = sent[-1]["text"]
    assert "Московское время" in body
    assert "не нашёл" in body.lower()


async def test_time_geocoder_down_says_service_unavailable(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_geocoder_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream outage must not read like "you typed it wrong"."""
    _install_mock_geocoder(
        monkeypatch,
        lambda _request: httpx.Response(502, json={"error": "bad gateway"}),
        mock_geocoder_clients,
    )
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/time Лондон"))
    body = sent[-1]["text"]
    assert "Московское время" in body
    assert "недоступен" in body.lower()


async def test_saved_city_without_tz_shows_own_city_clock(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_geocoder_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy branch 3, city half: ``/city`` alone is enough for a
    personal clock — no ``/timezone`` required."""
    _install_mock_geocoder(
        monkeypatch,
        _geo_response(
            {
                "results": [
                    {
                        "name": "Берлин",
                        "country": "Германия",
                        "timezone": "Europe/Berlin",
                        "latitude": 52.5,
                        "longitude": 13.4,
                    }
                ]
            }
        ),
        mock_geocoder_clients,
    )
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city Берлин"))
    sent.clear()
    await dispatcher.feed_update(bot, _update("/time"))
    body = sent[-1]["text"]
    assert "В твоём городе (Берлин)" in body
    assert len(re.findall(r"\d{2}:\d{2}:\d{2}", body)) >= 2


async def test_no_tz_no_city_hints_at_city_command(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Nothing stored — nudge toward /city rather than showing MSK alone."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/time"))
    assert "/city" in sent[-1]["text"]


async def test_en_user_gets_english_card_without_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The card used to be hardcoded Russian — including the weekday
    word, which leaked Cyrillic into an otherwise English UI."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    update = make_message_update(
        "/time",
        chat_type="private",
        user_id=6263,
        first_name="Alice",
        language_code="en",
    )
    await dispatcher.feed_update(bot, update)
    body = sent[-1]["text"]
    assert "Moscow time" in body
    assert not re.search(r"[А-Яа-яЁё]", body), f"Cyrillic leaked to en user: {body!r}"


async def test_group_time_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Read-only handler — works in groups too. Legacy accepts /time in
    groups; parity preserved.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, _update("/time", chat_type="supergroup", user_id=-2001)
    )
    assert result is not UNHANDLED
    assert "Московское время" in sent[-1]["text"]


async def test_bare_time_is_rate_limited_too(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The argless form is gated as well (#420).

    It used to be registered on the parent router with no middleware,
    on the theory that a bare ``/time`` is pure local arithmetic. It
    is not: with no stored timezone the handler falls back to the
    user's stored ``/city`` and geocodes it, so the ungated half could
    reach the geocoder exactly like ``/time <city>``. Whether it will
    is only knowable after two reads of ``user_settings`` — no router
    filter can tell the two apart, so the gate covers both.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    for _ in range(5):
        await dispatcher.feed_update(bot, _update("/time"))
    assert all("Московское время" in m["text"] for m in sent), sent

    sent.clear()
    await dispatcher.feed_update(bot, _update("/time"))
    assert "Слишком часто" in sent[-1]["text"], sent


async def test_the_geocoder_allowance_is_shared_across_time_city_and_weather(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """One bucket for every command that can geocode (#421).

    Each middleware instance owns a private bucket table, so building
    one per router — as ``/weather``, ``/city`` and ``/time`` each did —
    handed a single user three full allowances against one upstream
    host: fifteen geocoder calls a minute where the code documents
    five. Production now builds the limiter once, next to the shared
    ``WeatherService``, and injects it into all three.

    Spending the allowance on the cheapest of the three is enough to
    prove it: if the buckets were still separate, ``/weather`` and
    ``/city`` would answer normally here.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    for _ in range(5):
        await dispatcher.feed_update(bot, _update("/time"))
    sent.clear()

    await dispatcher.feed_update(bot, _update("/weather Лондон"))
    assert "Слишком часто" in sent[-1]["text"], sent
    await dispatcher.feed_update(bot, _update("/city Лондон"))
    assert "Слишком часто" in sent[-1]["text"], sent
