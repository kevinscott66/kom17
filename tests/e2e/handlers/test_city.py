"""End-to-end ``/city`` — the saved home city (RR-6 #74).

What this proves:

* All four aliases (``city``, ``город``, ``location``, ``kom_city``)
  route, including ``location``, which the legacy catalog advertised but
  never registered.
* Bare ``/city`` with nothing stored → the "tell me where you are" card;
  with a value stored → the card that echoes it.
* A name is resolved through the geocoder and the *canonical* spelling
  is what lands in ``user_settings.city`` — asserted by reading the
  column back, not by trusting the confirmation text.
* A name the geocoder can't place is refused **without writing**, the
  same contract ``/timezone`` has for a bad zone.
* The geocoder being *down* is different from the city being wrong: the
  user's input is saved anyway rather than lost.
* Reset tokens clear the value.
* The timezone hint appears only when the user has no zone of their own.
* Junk (markup, URLs) never reaches the column.
* An ``en`` user gets zero Cyrillic.
* Bare ``/weather`` and ``/forecast`` fall back to the saved city — the
  half of RR-6 #68 that this store makes possible.

The mocked-Open-Meteo harness is borrowed from ``test_weather.py``:
``/city`` and ``/weather`` share ONE ``WeatherService`` instance built in
``main_router``, so patching the class at that instantiation site pins
both.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update
from sqlalchemy import update

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User as DBUser
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import city as city_handler
from telegram_invite_bot.handlers import weather as weather_handler
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.routers import main_router as main_router_module
from telegram_invite_bot.services.weather_service import WeatherService
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

USER_ID = 6161

_CYRILLIC = re.compile("[А-Яа-яЁё]")


@pytest.fixture
async def mock_clients() -> AsyncIterator[list[httpx.AsyncClient]]:
    """Close every client ``_install_mock`` hands out, even on failure."""
    cleanup: list[httpx.AsyncClient] = []
    try:
        yield cleanup
    finally:
        for client in cleanup:
            await client.aclose()


def _update(
    text: str, *, chat_type: str = "private", user_id: int = USER_ID, language_code: str = "ru"
) -> Update:
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Cy",
        language_code=language_code,
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    cleanup: list[httpx.AsyncClient],
) -> None:
    """Pin the geocoder. MUST run before ``make_wired`` — the service is
    captured in the router closure at build time.

    ``handlers.city`` is patched too, for the ``build_router(None)``
    fallback path that a unit-level caller could take.
    """
    transport = httpx.MockTransport(handler)

    class _PinnedService(WeatherService):
        def __init__(self) -> None:
            client = httpx.AsyncClient(transport=transport)
            cleanup.append(client)
            super().__init__(client=client)

    monkeypatch.setattr(city_handler, "WeatherService", _PinnedService)
    monkeypatch.setattr(weather_handler, "WeatherService", _PinnedService)
    monkeypatch.setattr(main_router_module, "WeatherService", _PinnedService)


def _geocoder(
    name: str = "Краснодар",
    country: str = "Россия",
    timezone: str | None = "Europe/Moscow",
    *,
    found: bool = True,
) -> Callable[[httpx.Request], httpx.Response]:
    """A geocoder that always resolves to one city, plus a forecast stub
    so the same transport can serve a follow-up ``/weather``."""

    def upstream(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            if not found:
                return httpx.Response(200, json={"results": []})
            result: dict[str, Any] = {
                "name": name,
                "country": country,
                "latitude": 45.03,
                "longitude": 38.98,
            }
            if timezone is not None:
                result["timezone"] = timezone
            return httpx.Response(200, json={"results": [result]})
        return httpx.Response(
            200,
            json={
                "current": {"temperature_2m": 21.0, "weather_code": 0},
                "daily": {
                    "temperature_2m_min": [14.0],
                    "temperature_2m_max": [26.0],
                    "time": ["2026-08-08"],
                    "weather_code": [0],
                },
            },
        )

    return upstream


async def _stored_city(registry: Any, user_id: int = USER_ID) -> str | None:
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        return await UserSettingsRepo(session).get_city(user_id)


# ── routing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("alias", ["/city", "/город", "/location", "/kom_city"])
async def test_aliases_reach_the_unset_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    """``location`` is in this list deliberately: the legacy catalog
    listed it (bot.py:42347) but no handler was ever bound to it."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Город пока не указан" in sent[0]["text"]
    assert "/city Краснодар" in sent[0]["text"]


async def test_plain_text_alias_sets_the_city(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``город Краснодар`` in a DM — the legacy plain-text shortcut,
    restored through ``TextAliasMiddleware``."""
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("город Краснодар"))
    assert result is not UNHANDLED
    assert await _stored_city(registry) == "Краснодар"


# ── the happy path ────────────────────────────────────────────────────


async def test_set_persists_canonical_spelling(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user types lowercase; the geocoder's spelling is what we keep.
    Legacy stored the raw input, so profile cards read ``краснодар``."""
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/city краснодар"))
    assert result is not UNHANDLED
    assert "Краснодар" in sent[-1]["text"]
    assert await _stored_city(registry) == "Краснодар"


async def test_geocoder_spelling_goes_through_the_same_gate(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream is trusted-ish, not trusted. A geocoder name that fails
    ``normalize_city`` must not become the one value in this column that
    never met the rule — we keep the user's own wording instead."""
    _install_mock(monkeypatch, _geocoder(name="<b>Краснодар</b>"), mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city Краснодар"))
    assert await _stored_city(registry) == "Краснодар"
    assert "<b>Краснодар</b>" not in sent[-1]["text"]


async def test_bare_command_shows_the_saved_city(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city Краснодар"))
    sent.clear()
    result = await dispatcher.feed_update(bot, _update("/city"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Твой город" in body
    assert "Краснодар" in body


async def test_reset_clears_the_column(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy had no way out at all: an empty argument was rejected by
    its own parser before it could clear anything."""
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city Краснодар"))
    assert await _stored_city(registry) == "Краснодар"

    sent.clear()
    result = await dispatcher.feed_update(bot, _update("/city СБРОС"))
    assert result is not UNHANDLED
    assert "убран" in sent[-1]["text"]
    assert await _stored_city(registry) is None


async def test_set_does_not_hold_the_write_lock_across_the_geocoder(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
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
    upstream = _geocoder()

    async def probing_geocoder(request: httpx.Request) -> httpx.Response:
        async with registry_box[0].session(DBName.USERS)() as other:
            await other.execute(
                update(DBUser).where(DBUser.user_id == 999).values(messages_count=1)
            )
            await other.commit()
        other_updates_could_write.append(True)
        return upstream(request)

    _install_mock(monkeypatch, probing_geocoder, mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    registry_box.append(registry)
    capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/city Краснодар"))
    assert result is not UNHANDLED

    assert other_updates_could_write == [True]
    # Committing the touch early must not lose the write the handler
    # makes *after* the call.
    assert await _stored_city(registry) == "Краснодар"


# ── refusals ──────────────────────────────────────────────────────────


async def test_unknown_city_is_refused_without_writing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saving a name nobody can place would break every later bare
    ``/weather``, and the user would blame the weather, not the typo."""
    _install_mock(monkeypatch, _geocoder(found=False), mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/city Атлантида-XYZ"))
    assert result is not UNHANDLED
    assert "Не нашёл город" in sent[-1]["text"]
    assert await _stored_city(registry) is None


@pytest.mark.parametrize("junk", ["<b>Москва</b>", "https://example.com", "12345"])
async def test_junk_never_reaches_the_column(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    junk: str,
) -> None:
    """No geocoder is mocked here on purpose: validation must reject
    these before any HTTP call is attempted. If one slipped through, the
    test would fail on a real network call rather than pass quietly.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(f"/city {junk}"))
    assert result is not UNHANDLED
    assert "не похоже на город" in sent[-1]["text"]
    assert await _stored_city(registry) is None


async def test_geocoder_outage_still_saves_the_input(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream 502 is our problem, not the user's — losing their
    input over it would be the worst of both worlds. The next
    ``/weather`` geocodes the name again anyway."""

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "bad gateway"})

    _install_mock(monkeypatch, upstream, mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/city Краснодар"))
    assert result is not UNHANDLED
    assert "Запомнил" in sent[-1]["text"]
    assert await _stored_city(registry) == "Краснодар"


# ── the timezone hint ─────────────────────────────────────────────────


async def test_tz_hint_offered_when_user_has_no_zone(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offered, never applied: silently setting a second preference the
    user didn't ask for is how ``/time`` starts lying to people who
    deliberately keep their zone elsewhere."""
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city Краснодар"))
    body = sent[-1]["text"]
    assert "/timezone Europe/Moscow" in body

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        assert await UserSettingsRepo(session).get_timezone(USER_ID) is None


async def test_tz_hint_suppressed_when_user_already_chose(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/timezone Asia/Tokyo"))
    sent.clear()
    await dispatcher.feed_update(bot, _update("/city Краснодар"))
    body = sent[-1]["text"]
    assert "Запомнил" in body
    assert "Часовой пояс там" not in body


async def test_no_tz_hint_when_geocoder_omits_the_zone(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open-Meteo usually returns ``timezone``, but the field is not
    guaranteed. A missing zone must degrade to "no hint", not to a hint
    reading ``/timezone None``."""
    _install_mock(monkeypatch, _geocoder(timezone=None), mock_clients)
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city Краснодар"))
    body = sent[-1]["text"]
    assert "Запомнил" in body
    assert "/timezone" not in body


# ── what the store unlocks (RR-6 #68, saved-city half) ────────────────


async def test_bare_weather_uses_the_saved_city(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city Краснодар"))
    sent.clear()
    result = await dispatcher.feed_update(bot, _update("/weather"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Краснодар" in body
    assert "Укажи город" not in body


async def test_bare_forecast_uses_the_saved_city(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city Краснодар"))
    sent.clear()
    result = await dispatcher.feed_update(bot, _update("/forecast"))
    assert result is not UNHANDLED
    assert "Укажи город" not in sent[-1]["text"]


async def test_bare_weather_still_prompts_without_a_saved_city(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The fallback must not swallow the prompt for users who never set
    one — that prompt is where they learn ``/city`` exists."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/weather"))
    assert result is not UNHANDLED
    assert "Укажи город" in sent[-1]["text"]
    assert "/city" in sent[-1]["text"]


# ── ru/en parity ──────────────────────────────────────────────────────


async def test_english_flow_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset card, save confirmation and reset confirmation, all in
    English. The saved *value* may of course be Cyrillic — that's the
    user's own city name — so this flow uses an English one."""
    _install_mock(
        monkeypatch,
        _geocoder(name="London", country="United Kingdom", timezone="Europe/London"),
        mock_clients,
    )
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    for text in ("/city", "/city london", "/city reset"):
        sent.clear()
        result = await dispatcher.feed_update(bot, _update(text, language_code="en"))
        assert result is not UNHANDLED
        body = sent[-1]["text"]
        assert not _CYRILLIC.search(body), f"Cyrillic leaked to en user on {text!r}: {body!r}"

    assert await _stored_city(registry) is None


async def test_setting_a_city_does_not_switch_an_en_user_to_russian(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION PIN, and the reason ``set_city`` writes
    ``language=NULL``.

    ``/city`` creates the user's first ``user_settings`` row. When that
    row seeded ``language='ru'`` as a placeholder,
    :class:`LanguageMiddleware` read it back as an explicit ``/lang ru``
    choice and served Russian to an English user forever after.

    The cache is cleared between the two updates on purpose: in
    production the flip is invisible for up to five minutes, which is
    exactly what made the bug survive the first round of English
    coverage in this file.
    """
    from telegram_invite_bot.middlewares.language import clear_language_cache

    _install_mock(monkeypatch, _geocoder(name="London", country="United Kingdom"), mock_clients)
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    clear_language_cache()
    await dispatcher.feed_update(bot, _update("/city london", language_code="en"))

    clear_language_cache()
    sent.clear()
    await dispatcher.feed_update(bot, _update("/city", language_code="en"))
    body = sent[-1]["text"]
    assert not _CYRILLIC.search(body), f"en user flipped to ru by /city: {body!r}"


async def test_english_rejections_have_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, _geocoder(found=False), mock_clients)
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/city <b>x</b>", language_code="en"))
    assert not _CYRILLIC.search(sent[-1]["text"]), sent[-1]["text"]

    await dispatcher.feed_update(bot, _update("/city Atlantis", language_code="en"))
    body = sent[-1]["text"]
    assert not _CYRILLIC.search(body), body
    assert "Atlantis" in body


# ── chat types ────────────────────────────────────────────────────────


async def test_group_invocation_sets_the_author_city(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mock_clients: list[httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parity with legacy and with ``/timezone``: the city is a per-user
    preference, so a group invocation is meaningful — it sets the
    author's own."""
    _install_mock(monkeypatch, _geocoder(), mock_clients)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/city Краснодар", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert "Запомнил" in sent[-1]["text"]
    assert await _stored_city(registry) == "Краснодар"
