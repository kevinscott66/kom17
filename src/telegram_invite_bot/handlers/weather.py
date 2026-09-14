"""``/weather`` / ``/погода`` / ``/forecast`` — the weather surface.

Scope:

* Private + group chats.
* ``/weather Москва`` / ``/погода Berlin`` → today's card.
* Period words are read from the argument, not from which command was
  typed: ``/weather завтра в Казани`` answers tomorrow, ``/weather на
  3 дня`` a three-day range (RR-6 #68, legacy ``bot.py:37523``). The
  command only decides the *default* — today for ``/weather``, seven
  days for ``/forecast``.
* The city is pulled with :func:`normalize_weather_location`, the same
  parser the plain-text form uses, so ``/weather какая погода в
  Краснодаре`` and the slash-less phrase land on identical code.
* Bare ``/weather`` falls back to the city saved via ``/city``
  (RR-6 #74) and only prompts when there isn't one — the legacy
  behaviour, restored. ``/forecast`` shares the same fallback.
* Reply is HTML, single message, in the user's language.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from telegram_invite_bot.core.weather_query import detect_period, normalize_weather_location
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.ai_rate_limit import WeatherRateLimitMiddleware
from telegram_invite_bot.services.weather_service import (
    CityNotFoundError,
    WeatherLookupError,
    WeatherService,
    wmo_label,
)
from telegram_invite_bot.utils.aiogram import command_args
from telegram_invite_bot.utils.html import legacy_md_to_html

# Default horizon for a bare ``/forecast`` — legacy's own default.
_FORECAST_DEFAULT_DAYS = 7

log = logger.bind(component="handlers.weather")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message
    from loguru import Logger

    from telegram_invite_bot.services.user_service import UserService
    from telegram_invite_bot.services.weather_service import (
        ForecastReport,
        WeatherReport,
    )


def _location_title(city: str, region: str | None, country: str | None) -> str:
    """``City (region, country)`` — the legacy title (``bot.py:37570-37589``).

    The region is what disambiguates the dozen Springfields; the port
    had dropped it, so two different cities rendered the same headline.
    Open-Meteo's geocoder is community-edited, so every field is escaped
    before it reaches an HTML message.
    """
    parts = [html.escape(part) for part in (region, country) if part]
    safe_city = html.escape(city)
    return f"{safe_city} ({', '.join(parts)})" if parts else safe_city


def _opt(val: float | int | None, suffix: str = "", fmt: str = "{:.0f}") -> str:
    """Format an optional upstream number, or ``—`` when it's missing.

    Gaps collapse to a dash instead of dropping the whole bullet so the
    card keeps a constant shape no matter which fields Open-Meteo
    happened to return.
    """
    return f"{fmt.format(val)}{suffix}" if val is not None else "—"


def _format(report: WeatherReport, lang: str) -> str:
    """Render today's card as HTML for ``parse_mode=HTML``."""
    title_loc = _location_title(report.city, report.region, report.country)
    range_line = f"{_opt(report.temp_min_c, '°C')} … {_opt(report.temp_max_c, '°C')}"
    feels = (
        t("h_weather_feels_like", lang, feels_like=_opt(report.feels_like_c))
        if report.feels_like_c is not None
        else ""
    )
    # Localized condition via wmo_label: ``report.condition`` is the
    # pre-rendered RU label, kept only for back-compat, and handing it to
    # an en user was the ru/en convergence bug.
    lines = [
        t("h_weather_title", lang, location=title_loc),
        "",
        t("h_weather_today", lang),
        t("h_weather_now", lang, temp=_opt(report.temperature_c, "°C"), feels=feels),
        t("h_weather_condition", lang, condition=wmo_label(report.weather_code, lang)),
        t("h_weather_humidity", lang, humidity=_opt(report.humidity_pct)),
        t("h_weather_wind", lang, wind=_opt(report.wind_kmh, fmt="{:.1f}")),
        t("h_weather_range_today", lang, range_line=range_line),
    ]
    return "\n".join(lines)


def _fmt_date(date_iso: str, lang: str) -> str:
    """``YYYY-MM-DD`` → ``DD-MM`` (ru) / ``MM-DD`` (en)."""
    parts = date_iso.split("-")
    if len(parts) == 3:
        _, mm, dd = parts
        return f"{mm}-{dd}" if lang == "en" else f"{dd}-{mm}"
    return html.escape(date_iso)


def _format_tomorrow(report: ForecastReport, lang: str) -> str:
    """Tomorrow's card — day ``[1]`` of the two-day forecast.

    A forecast that came back with only today in it is a data gap, not
    an error: the upstream call succeeded, so we say "no data for
    tomorrow yet" rather than "the service is down".
    """
    title_loc = _location_title(report.city, report.region, report.country)
    lines = [
        t("h_weather_title", lang, location=title_loc),
        "",
        t("h_weather_tomorrow", lang),
    ]
    if len(report.days) < 2:
        lines.append(t("h_weather_no_tomorrow", lang))
        return "\n".join(lines)

    day = report.days[1]
    range_line = f"{_opt(day.temp_min_c, '°C')} … {_opt(day.temp_max_c, '°C')}"
    lines.extend(
        [
            t("h_weather_date", lang, date=_fmt_date(day.date_iso, lang)),
            t("h_weather_condition", lang, condition=wmo_label(day.weather_code, lang)),
            t("h_weather_temp_range", lang, range_line=range_line),
        ]
    )
    return "\n".join(lines)


def _format_forecast(report: ForecastReport, lang: str) -> str:
    """Render a multi-day forecast as an HTML card.

    Title line with the city (+region, country), then one bullet per
    day: ``• {DD-MM}: {tmin}°…{tmax}°C, {condition}``.
    """
    title_loc = _location_title(report.city, report.region, report.country)
    lines: list[str] = [
        t("h_forecast_title", lang, location=title_loc),
        t("h_forecast_period", lang, days=len(report.days)),
        "",
    ]
    if not report.days:
        lines.append(t("h_forecast_empty", lang))
        return "\n".join(lines)
    for day in report.days:
        cond = wmo_label(day.weather_code, lang)
        lines.append(
            f"• {_fmt_date(day.date_iso, lang)}: "
            f"{_opt(day.temp_min_c)}°…{_opt(day.temp_max_c)}°C, {cond}"
        )
    return "\n".join(lines)


async def _resolve_query(
    message: Message,
    city: str,
    user_service: UserService | None,
) -> str:
    """The city to look up: the argument, else the saved one, else ``""``.

    RR-6 #74 closed the loop legacy had all along — ``/city Краснодар``
    once, then bare ``/weather`` forever. An unreadable settings row
    degrades to ``""`` (the prompt), never to an error: the prompt is
    what the user got yesterday, and a weather command that fails
    because a *preference* table hiccuped would be a worse product than
    the one that had no preference at all.
    """
    if city:
        return city
    if user_service is None or message.from_user is None:
        return ""
    try:
        return await user_service.get_city(message.from_user.id) or ""
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        log.bind(uid=message.from_user.id).warning(f"saved city unreadable: {exc}")
        return ""


async def _render(
    weather_service: WeatherService,
    query: str,
    mode: str,
    days: int,
    lang: str,
) -> str:
    """Fetch and render the card ``mode`` asks for.

    ``tomorrow`` and ``range`` are the same upstream call with a
    different horizon, so there is no separate "tomorrow" service
    method — only a different slice of the same forecast.
    """
    if mode == "today":
        return _format(await weather_service.lookup(query), lang)
    report = await weather_service.forecast(query, days=days)
    if mode == "tomorrow":
        return _format_tomorrow(report, lang)
    return _format_forecast(report, lang)


async def _answer(
    message: Message,
    weather_service: WeatherService,
    query: str,
    mode: str,
    days: int,
    lang: str,
    bound: Logger,
) -> None:
    """Shared tail of both commands: look up, render, reply, log.

    The two failure modes need different words on screen — "check the
    spelling" is actionable, "the service is down" is not the user's
    fault — so they stay separate exceptions all the way down.
    """
    try:
        text = await _render(weather_service, query, mode, days, lang)
    except CityNotFoundError:
        bound.info("city not found")
        await message.answer(t("h_weather_not_found", lang))
        return
    except WeatherLookupError as exc:
        bound.warning("weather lookup failed: {e}", e=exc)
        await message.answer(t("h_weather_unavailable", lang))
        return

    await message.answer(text)
    bound.info("weather replied: mode={m} days={d}", m=mode, d=days)


async def handle_weather(
    message: Message,
    command: CommandObject,
    weather_service: WeatherService,
    lang: str,
    user_service: UserService | None = None,
) -> None:
    """``/weather [period] [city]`` — today by default, any period on request."""
    raw = command_args(command)
    mode, days = detect_period(raw)
    query = await _resolve_query(message, normalize_weather_location(raw), user_service)
    # Still nothing to look up — prompt, and point at ``/city`` so the
    # next bare call works.
    if not query:
        await message.answer(legacy_md_to_html(t("weather_city_prompt", lang)))
        return

    uid = message.from_user.id if message.from_user else None
    await _answer(message, weather_service, query, mode, days, lang, log.bind(uid=uid, query=query))


async def handle_forecast(
    message: Message,
    command: CommandObject,
    weather_service: WeatherService,
    lang: str,
    user_service: UserService | None = None,
) -> None:
    """``/forecast [period] <city>`` — multi-day card. Works in any chat.

    Same parser as ``/weather``; only the default differs. ``/forecast
    завтра`` still means tomorrow — the command name is a default, not
    an override of what the user actually asked for.
    """
    raw = command_args(command)
    mode, days = detect_period(raw)
    if mode == "today":
        mode, days = "range", _FORECAST_DEFAULT_DAYS
    query = await _resolve_query(message, normalize_weather_location(raw), user_service)
    # Neither an argument nor a saved city — prompt (RR-6 #74).
    if not query:
        await message.answer(legacy_md_to_html(t("forecast_city_prompt", lang)))
        return

    uid = message.from_user.id if message.from_user else None
    await _answer(message, weather_service, query, mode, days, lang, log.bind(uid=uid, query=query))


def build_router(
    service: WeatherService | None = None,
    rate_limit: WeatherRateLimitMiddleware | None = None,
) -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    M-P-9: the caller injects a shared :class:`WeatherService` so the
    per-instance TTL cache is reused across requests. A fresh instance
    per ``build_router`` call would defeat the cache — that was the
    pre-fix bug the audit flagged.

    What sharing does NOT buy today is a warm connection. The service
    takes an optional ``client``, but no production call site fills it
    and nothing builds a lifespan-scoped ``httpx.AsyncClient``, so an
    uncached lookup pays a fresh TCP+TLS handshake (#423). Wiring a real
    long-lived client is a separate change; until it lands, read
    "shared" as "shared cache", not "shared socket".

    The parameter defaults to ``None`` purely for test convenience
    (existing call sites that don't care about cache reuse keep
    working); production wiring in :mod:`routers.main_router` always
    passes the single instance built there.

    ``rate_limit`` is injected the same way — built once, shared — and
    for a parallel reason (#421): every instance of the middleware owns a private
    bucket table, so a per-router instance would grant each geocoder
    surface its own full allowance instead of sharing one. Production
    passes the single instance built next to the service; the ``None``
    default keeps standalone tests self-contained.
    """
    weather_service = service if service is not None else WeatherService()
    limiter = rate_limit if rate_limit is not None else WeatherRateLimitMiddleware()

    # ``user_service`` is injected by the session middleware in
    # production; it stays optional so the many tests that wire only a
    # weather service keep working (and so a bare ``/weather`` in a
    # context without the middleware degrades to the prompt).
    async def _handle_weather(
        message: Message,
        command: CommandObject,
        lang: str,
        user_service: UserService | None = None,
    ) -> None:
        await handle_weather(message, command, weather_service, lang, user_service)

    async def _handle_forecast(
        message: Message,
        command: CommandObject,
        lang: str,
        user_service: UserService | None = None,
    ) -> None:
        await handle_forecast(message, command, weather_service, lang, user_service)

    router = Router(name="weather")
    # M-P-9: per-user rate limit BEFORE the lookup so a flood doesn't
    # even reach the upstream-cached path. 5/min — matches the
    # legacy posture. Shared with /city and /time (#421): those reach
    # the same geocoding endpoint, so they must draw on one allowance.
    router.message.middleware(limiter)
    # No ``magic=F.args`` — bare ``/weather`` must reach the handler so
    # it can reply the usage prompt (legacy had no fallback bridge to
    # absorb it anymore). ``kom_weather`` mirrors the legacy alias.
    # #489: ``F.from_user`` is load-bearing, not cosmetic. Both the
    # bucket limiter above and the global throttle let an update with
    # no ``from_user`` straight through (``_bucket_rate_limit.py:172-176``,
    # ``throttling.py:229-231``), so without this filter a message sent
    # on behalf of a channel could drive the upstream geocoder without
    # any limit and without being attributable to a user. Legacy closed
    # the same path on the handler's first line (``bot.py:16944`` →
    # ``ensure_user_access`` → ``bot.py:1621``). ``/city`` and ``/time``
    # already carry the filter and share this allowance (#421).
    router.message.register(
        _handle_weather,
        Command("weather", "погода", "kom_weather", ignore_case=True),
        F.from_user,
    )
    # /forecast shares the weather service (geocode + daily endpoint).
    router.message.register(
        _handle_forecast,
        Command("forecast", "прогноз", "fc", "kom_forecast", ignore_case=True),
        F.from_user,
    )
    return router
