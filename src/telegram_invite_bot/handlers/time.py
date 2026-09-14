"""``/time`` — Moscow time, your timezone, and time in any named city.

Legacy ``cmd_time`` (bot.py:16793) does three things:

1. Render Moscow time (always).
2. If an argument is supplied, geocode it as a city name and append a
   "time in <city>" block.
3. Otherwise, append a block for the user's stored ``timezone`` *and*
   for their stored ``city`` (geocoding the city to a tz on the fly).

All three are ported (RR-6 #69). Branches 2 and 3-city need a geocoder;
they use the same :class:`WeatherService` instance the weather surface
owns and the same rate limiter, so both surfaces draw on one allowance
(#421). They do NOT share an HTTP client: ``main_router`` builds the
service with no ``client=``, so every uncached lookup opens its own
connection to the geocoding host (#423). They do NOT share a geocode
cache either: the service's TTL cache is keyed by
``(lat, lon, utc-date)`` and holds a forecast, so
:meth:`WeatherService.resolve_city` calls the geocoder every time.
Resolving a city for ``/weather`` therefore does nothing to make the
same city cheaper for ``/time``.

``/timezone Europe/Berlin`` remains the zero-network path: once set,
``/time`` shows both MSK and Berlin without touching the network at all.

Chat type: legacy accepts /time everywhere. Mirror that — the handler
needs only the author's user_id (read-only against ``user_settings``),
no FSM, no economy, no admin-cache. Group ``/time`` users keep working.

Every form carries the geocoder rate limit (#420). The bare form is
not the pure-local branch it looks like: with no stored timezone it
falls back to the stored ``/city`` and geocodes that (see
:func:`handle_time`), and which of the two it will do is only knowable
after two reads of ``user_settings`` — far past the point where a
router filter could tell them apart. The old split gated on the wrong
axis and left that fallback completely open. Charging a token for the
genuinely local answer is the cost of closing it, and at 5/min it is
not a ceiling anyone reaches asking for the time.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.ai_rate_limit import WeatherRateLimitMiddleware
from telegram_invite_bot.services.weather_service import (
    CityNotFoundError,
    WeatherLookupError,
    WeatherService,
)
from telegram_invite_bot.utils.aiogram import command_args, require_from_user
from telegram_invite_bot.utils.time import format_local_time

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.user_service import UserService

log = logger.bind(component="handlers.time")

# Hard-coded — legacy uses ``MOSCOW_TZ = ZoneInfo("Europe/Moscow")`` as
# the canonical "default time" for Russian-speaking users. Keep it the
# same way: not a setting, not user-overridable.
_MOSCOW_TZ = "Europe/Moscow"

# Every alias legacy registered (bot.py:16793).
_ALIASES = ("time", "время", "time_msk", "kom_time")

# How much of a rejected city name we echo back. The error messages
# quote what the user typed so a typo is obvious, and ``/time`` plus a
# 4000-character argument would otherwise build a reply Telegram
# refuses to deliver — leaving the user with no answer at all.
_MAX_CITY_ECHO = 64


def _echo(city: str) -> str:
    """The user's own input, HTML-safe and bounded, for an error line."""
    trimmed = city.strip()
    if len(trimmed) > _MAX_CITY_ECHO:
        trimmed = trimmed[:_MAX_CITY_ECHO] + "…"
    return html.escape(trimmed)


def _clock_block(header: str, tz_name: str, lang: str) -> list[str]:
    """``[header, "", now, date]`` for ``tz_name``, or ``[]`` if it won't resolve.

    Returning an empty list rather than a placeholder block is what
    keeps a broken tz from rendering a hollow "В твоём часовом поясе ()"
    fragment — the caller simply appends nothing.
    """
    loc_time, loc_date, loc_weekday = format_local_time(tz_name, lang)
    if not loc_time:
        return []
    return [
        "",
        header,
        "",
        t("h_time_now", lang, time=loc_time),
        t("h_time_date", lang, date=loc_date, weekday=loc_weekday),
    ]


def _moscow_block(lang: str) -> list[str]:
    """The always-present MSK header. ``—`` if the host tzdata is broken."""
    msk_time, msk_date, msk_weekday = format_local_time(_MOSCOW_TZ, lang)
    return [
        t("h_time_msk_header", lang),
        "",
        t("h_time_now", lang, time=msk_time or "—"),
        t("h_time_date", lang, date=msk_date or "—", weekday=msk_weekday or "—"),
    ]


async def _city_block(
    weather_service: WeatherService,
    city: str,
    lang: str,
    *,
    header_key: str,
) -> list[str]:
    """Geocode ``city`` and render its clock, or an explanation of why not.

    The three failure modes stay distinct because they need different
    words: a typo the user can fix, an upstream outage they can only
    wait out, and a place the geocoder knows but has no timezone for
    (rare, but ``/timezone`` is the actual workaround there).
    """
    try:
        resolved = await weather_service.resolve_city(city)
    except CityNotFoundError:
        return ["", t("h_time_city_not_found", lang, city=_echo(city))]
    except WeatherLookupError as exc:
        log.warning("time geocode failed: {e}", e=exc)
        return ["", t("h_time_city_unavailable", lang, city=_echo(city))]

    safe_name = _echo(resolved.name)
    if not resolved.timezone:
        return ["", t("h_time_city_no_tz", lang, city=safe_name)]
    block = _clock_block(t(header_key, lang, city=safe_name), resolved.timezone, lang)
    return block or ["", t("h_time_city_no_tz", lang, city=safe_name)]


async def handle_time(
    message: Message,
    command: CommandObject,
    user_service: UserService,
    weather_service: WeatherService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Moscow block, then whichever second block the request implies."""
    tg_user = require_from_user(message)
    # ``touch`` keeps last_seen fresh; the reads below are all against
    # the same settings row.
    await user_service.touch(tg_user)
    # That touch is the only write this command makes, and the city
    # branches below geocode over the network (ten seconds allowed,
    # twice SQLite's ``busy_timeout``). End the write transaction here
    # so ``users.db`` isn't locked for the wait — see
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()

    parts = _moscow_block(lang)

    city_arg = command_args(command)
    if city_arg:
        # ``/time Лондон`` — an explicit ask always wins over stored
        # preferences; nothing else is appended.
        parts.extend(
            await _city_block(weather_service, city_arg, lang, header_key="h_time_city_header")
        )
        await message.answer("\n".join(parts))
        return

    stored_tz = await user_service.get_timezone(tg_user.id)
    if stored_tz and stored_tz != _MOSCOW_TZ:
        # Skip the second block if the user explicitly set their tz to
        # Moscow — would duplicate the first block verbatim. Their
        # stored choice still gets honoured (the absence of the block
        # confirms it matches MSK).
        #
        # stored_tz comes from our DB; it's an IANA name the user passed
        # through /timezone's validator. No external input reaches this
        # branch, so the interpolation is safe under HTML parse_mode
        # without escaping — but be defensive anyway, in case a future
        # code path admits user-controlled values.
        header = t("h_time_tz_header", lang, tz=html.escape(stored_tz))
        parts.extend(_clock_block(header, stored_tz, lang))

    if not stored_tz:
        # Legacy branch 3, city half: no explicit timezone, but a saved
        # ``/city`` is enough to show the user their own clock. A stored
        # tz wins because the user set it deliberately and it costs no
        # network call.
        saved_city = await user_service.get_city(tg_user.id)
        if saved_city:
            parts.extend(
                await _city_block(
                    weather_service, saved_city, lang, header_key="h_time_own_city_header"
                )
            )
        else:
            parts.extend(["", t("time_city_hint", lang)])

    await message.answer("\n".join(parts))


def build_router(
    service: WeatherService | None = None,
    rate_limit: WeatherRateLimitMiddleware | None = None,
) -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    ``service`` is the shared :class:`WeatherService` — the same instance
    the weather router gets, so both surfaces run the same lookup policy.
    They do NOT reuse one HTTP client: production passes no ``client=``,
    so each uncached call dials the geocoding host on its own (#423).
    ``rate_limit`` is the shared :class:`WeatherRateLimitMiddleware`, and
    sharing that one matters (#421): a private instance here would own a
    private bucket table and hand ``/time`` a full allowance of its own
    on top of ``/weather``'s.
    Both default to ``None`` for test convenience; production wiring in
    :mod:`routers.main_router` always passes the singletons.
    """
    weather_service = service if service is not None else WeatherService()
    limiter = rate_limit if rate_limit is not None else WeatherRateLimitMiddleware()

    async def _handle_time(
        message: Message,
        command: CommandObject,
        user_service: UserService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_time(message, command, user_service, weather_service, lang, checkpoint)

    router = Router(name="time")
    # #420: one router, one gate, every form. The split that used to
    # live here (a ``magic=F.args`` sub-router carrying the middleware,
    # the bare form registered ungated on the parent) was aimed at the
    # wrong branch: the bare form geocodes the user's stored city
    # whenever no timezone is set, so the ungated half could reach the
    # geocoder just as readily as the gated one. With the split gone
    # the ``magic``/``~F.args`` pair has nothing left to separate —
    # ``handle_time`` has always branched on the argument itself.
    router.message.middleware(limiter)
    router.message.register(
        _handle_time,
        Command(*_ALIASES, ignore_case=True),
        F.from_user,
    )
    return router
