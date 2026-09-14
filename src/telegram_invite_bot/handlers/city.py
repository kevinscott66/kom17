"""``/city`` — the saved home city (RR-6 #74).

Legacy ``cmd_city`` (bot.py:16861) had three shapes, and this handler
keeps all three plus one:

1. **No argument** → show the saved city (or a nudge to set one).
2. **A city name** → save it.
3. **A blank-ish argument** → the "give me a name" example.
4. *(new)* **A reset token** → clear it. Legacy could only clear the
   value by passing an empty string, which its own argument parser
   rejected before it ever reached ``set_user_city`` — so in practice a
   city, once set, could not be removed. ``/timezone`` already ships the
   vocabulary (``reset`` / ``сброс`` / ``clear`` / ``удалить``); reusing
   it costs nothing and closes a dead end.

Two things this does that legacy did not:

* **The geocoder gets a vote.** The name is resolved through
  :meth:`WeatherService.resolve_city` and the *canonical* spelling is
  what we store, so ``/city краснодар`` saves ``Краснодар`` and the
  profile card reads properly. A name the geocoder can't place is
  refused outright rather than silently saved and then failing on every
  subsequent ``/weather``.
* **The timezone hint.** The geocoding response carries the city's IANA
  zone. If the user has no timezone set, the confirmation offers it as a
  one-tap follow-up. We do NOT write it: quietly changing a second
  setting the user didn't ask about is how ``/time`` starts lying to
  people who deliberately kept theirs elsewhere.

Chat type: any, matching legacy and ``/timezone``. The city is keyed off
the message author, so a group invocation sets that author's city. The
reply does echo the stored value into the group — the user typed the
command there, so that's their call, and it's the same exposure legacy
had.
"""

from __future__ import annotations

import html
import re
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

log = logger.bind(component="handlers.city")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.user_service import UserService

# Same vocabulary ``/timezone`` accepts, for the same reason: a user who
# learned "сброс" on one preference shouldn't have to learn a second word
# for the next one.
_RESET_TOKENS = frozenset({"reset", "сброс", "clear", "удалить", "убрать"})

# Long enough for "Санкт-Петербург" and "Frankfurt am Main", short enough
# that the value can't dominate a profile card or a group reply. The
# geocoder's own names are all well inside this.
MAX_CITY_LENGTH = 64

# Letters (any script), digits, spaces and the punctuation real place
# names use. Everything else — angle brackets, ampersands, slashes,
# newlines, emoji — is refused. The value is HTML-escaped on every render
# anyway; this is the belt to that suspenders, and it also keeps the
# column free of things that are obviously not city names (URLs being
# the one people actually try).
_FORBIDDEN_CITY_RE = re.compile(r"[^\w\s\-'’.,()]|_", re.UNICODE)
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def normalize_city(raw: str) -> str | None:
    """Return a storable city name, or ``None`` if ``raw`` isn't one.

    Rules, in the order a bad input trips them:

    * whitespace (including the tabs and newlines a paste brings along)
      collapses to single spaces and is stripped;
    * at least one letter must survive — ``/city 123`` and ``/city ---``
      are not cities;
    * no character outside letters/digits/space/``-'’.,()`` — this is
      what rejects markup, URLs and emoji;
    * at most :data:`MAX_CITY_LENGTH` characters.

    Returning ``None`` rather than raising keeps the caller's shape flat
    (one ``if``), and there is exactly one thing to say to the user
    regardless of which rule tripped.
    """
    collapsed = _WHITESPACE_RE.sub(" ", raw).strip()
    if not collapsed or len(collapsed) > MAX_CITY_LENGTH:
        return None
    if not _HAS_LETTER_RE.search(collapsed):
        return None
    if _FORBIDDEN_CITY_RE.search(collapsed):
        return None
    return collapsed


def _saved_card(city: str, lang: str) -> str:
    return t("h_city_current", lang, city=html.escape(city))


async def handle_city(
    message: Message,
    command: CommandObject,
    user_service: UserService,
    weather_service: WeatherService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)
    # ``touch`` both refreshes last_seen and guarantees the ``users`` row
    # the ``user_settings`` FK points at exists — without it the very
    # first ``/city`` from a user who has never been seen would fail the
    # UPSERT. Same fix ``/lang`` and ``/timezone`` carry.
    await user_service.touch(tg_user)
    # #1983: end the bookkeeping transaction HERE, not at the geocoder.
    # ``6fa8962`` put it just above the network call it was aiming at,
    # which is three early returns too late: the bare form, the reset
    # and the rejected name all answer above that line and answered
    # holding ``users.db``'s single writer slot. Every write below
    # opens a fresh transaction and is still rolled back by the
    # middleware if this handler raises. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    user_id = tg_user.id
    bound = log.bind(uid=user_id)

    arg = command_args(command)

    # ── No argument → show what's stored ──────────────────────────────
    if not arg:
        stored = await user_service.get_city(user_id)
        if stored:
            await message.answer(_saved_card(stored, lang))
        else:
            await message.answer(t("h_city_unset", lang))
        return

    # ── Reset ─────────────────────────────────────────────────────────
    if arg.lower() in _RESET_TOKENS:
        await user_service.set_city(user_id, None)
        # The clear is what the user asked for; the confirmation below
        # must not be able to hold ``users.db`` while it is delivered.
        if checkpoint is not None:
            await checkpoint()
        await message.answer(t("h_city_reset_done", lang))
        bound.info("city cleared")
        return

    # ── Validate ──────────────────────────────────────────────────────
    candidate = normalize_city(arg)
    if candidate is None:
        await message.answer(t("h_city_bad_name", lang, limit=MAX_CITY_LENGTH))
        return

    # ── Resolve, then persist ─────────────────────────────────────────
    # Nothing has been written since the checkpoint at the top, so the
    # geocoder — allowed ten seconds, twice SQLite's ``busy_timeout`` —
    # is waited on with no write lock held.
    resolved = None
    try:
        resolved = await weather_service.resolve_city(candidate)
    except CityNotFoundError:
        # The one refusal: a name nobody can place would make every
        # later bare ``/weather`` fail with an error the user would
        # blame on the weather, not on the typo they made here.
        bound.info("city not found: {c!r}", c=candidate)
        await message.answer(t("h_city_not_found", lang, city=html.escape(candidate)))
        return
    except WeatherLookupError as exc:
        # The geocoder being down is not the user's problem and not a
        # reason to lose their input. Save what they typed; the next
        # ``/weather`` will geocode it again anyway, and if the name was
        # good it will simply work.
        bound.warning("geocoder unavailable, saving raw input: {e}", e=exc)

    # The geocoder's spelling goes through the SAME gate the user's input
    # did. Upstream is trusted-ish, not trusted: a name that fails the
    # gate (over-long, or carrying a character no renderer expects) would
    # otherwise be the one value in this column that never met the rule,
    # and every future reader of ``user_settings.city`` would have to
    # know that. Falling back to the user's own wording is safe — it
    # already passed.
    stored_city = candidate
    if resolved is not None:
        stored_city = normalize_city(resolved.name) or candidate
    await user_service.set_city(user_id, stored_city)
    # Same as the reset branch: the stored city stands regardless of how
    # the confirmation goes out, and a timezone read plus a send follow.
    if checkpoint is not None:
        await checkpoint()

    safe_city = html.escape(stored_city)
    parts = [t("h_city_saved", lang, city=safe_city)]
    # Offer the zone only when the user has none — an existing choice is
    # a choice, and second-guessing it here would be noise at best.
    if (
        resolved is not None
        and resolved.timezone
        and await user_service.get_timezone(user_id) is None
    ):
        parts.append(t("h_city_tz_hint", lang, tz=html.escape(resolved.timezone)))
    await message.answer("\n\n".join(parts))
    bound.info("city set: {c!r}", c=stored_city)


def build_router(
    service: WeatherService | None = None,
    rate_limit: WeatherRateLimitMiddleware | None = None,
) -> Router:
    """Aliases mirror legacy registration (bot.py:16861) plus ``location``,
    which the legacy command catalog listed (bot.py:42347) but never
    actually registered — a documented alias that did nothing.

    ``service`` is the shared :class:`WeatherService` built in
    :func:`routers.main_router.build_main_router`, the same instance
    :func:`handlers.weather.build_router` receives. Sharing buys the
    per-instance TTL cache, NOT a connection pool: nothing constructs a
    lifespan-scoped ``httpx.AsyncClient`` today, so every geocoding call
    still dials out on its own (#423). Defaults to a private instance
    for tests that don't care.

    The weather rate-limit middleware is mounted here too: ``/city
    <name>`` reaches the same upstream geocoding endpoint ``/weather``
    does, so leaving it ungated would have handed anyone a way around
    that limit. Mounting it was not enough on its own — until #421 this
    router built its OWN instance, and since each instance owns a
    private bucket table, ``/city`` simply handed out a second full
    allowance instead of drawing on the first. ``rate_limit`` is the
    shared instance; the ``None`` default is test convenience, exactly
    as with ``service``.
    """
    weather_service = service if service is not None else WeatherService()
    limiter = rate_limit if rate_limit is not None else WeatherRateLimitMiddleware()

    async def _handle_city(
        message: Message,
        command: CommandObject,
        user_service: UserService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_city(message, command, user_service, weather_service, lang, checkpoint)

    router = Router(name="city")
    router.message.middleware(limiter)
    router.message.register(
        _handle_city,
        Command("city", "город", "location", "kom_city", ignore_case=True),
        F.from_user,
    )
    return router
