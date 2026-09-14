"""Time/timezone helpers (Stage 27).

Mirrors legacy ``get_local_time_str`` from ``bot.py:4465`` — the
``/timezone`` handler uses :func:`format_local_time` both as a *validator*
(returns empty strings → reject the tz name) and as a *renderer* (returns
the formatted local components for display). Keeping the two roles in
one function matches legacy behaviour and avoids the trap of "validation
accepts a name but the renderer can't format it" drift.

Output format is fixed to match legacy strings shown to users:

* ``time_str``  — ``HH:MM:SS``
* ``date_str``  — ``DD.MM.YYYY``
* ``weekday``   — localised weekday name: ``_WEEKDAY_EN`` for
  ``lang='en'``, ``_WEEKDAY_RU`` otherwise (see the resolver below).
  Legacy showed Russian unconditionally; that divergence is deliberate
  and is the whole point of threading ``lang`` down here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

#: Hard-coded, not a setting. Legacy defined
#: ``MOSCOW_TZ = ZoneInfo("Europe/Moscow")`` (``bot.py:4445``) and read
#: every rating-history calendar off it (``bot.py:4448-4450``). The
#: same reasoning as ``handlers/time.py:67-70``: this is the canonical
#: "default clock" of a Russian-speaking product, not a display
#: preference, so it must not follow ``STATS_TIMEZONE`` or the host.
_MOSCOW_TZ: Final = ZoneInfo("Europe/Moscow")


def db_now() -> datetime:
    """Naive-UTC ``now`` — the pipeline's canonical DB timestamp convention.

    New-pipeline tables store wall-clock columns as naive ``datetime``
    values in UTC (``datetime.now(UTC).replace(tzinfo=None)``); comparisons
    and TTL sweeps must use the same frame. Use this helper instead of
    re-deriving the expression at call sites. (Legacy-shared columns that
    intentionally use naive *local* time — e.g. inventory ``expires`` —
    are the documented exception and must NOT switch to this.)
    """
    return datetime.now(UTC).replace(tzinfo=None)


def rating_history_date() -> date:
    """Today on the Moscow calendar — the key of a ``rating_history`` row.

    ``rating_history`` is keyed ``(group_id, date)`` and written with an
    upsert (``DonationsRatingRepo.save_history_snapshot``), so the date
    is not a label: pick the wrong day and the write silently OVERWRITES
    that day's snapshot. Three call sites feed it — the ``/group_pay``
    treasury payout, the two ``/rating`` admin toggles and the
    group-donation router — and they used to disagree: one read
    ``datetime.now(tz=UTC).date()``, the others ``date.today()``. On the
    MSK production host that put every payout between 00:00 and 03:00
    MSK on the *previous* day's row.

    Legacy had one answer for all three (``bot.py:10787``,
    ``get_moscow_time().strftime("%Y-%m-%d")``), so this helper is that
    answer, named once. Deliberately independent of
    ``Settings.stats.timezone``: that setting picks the window for
    *displayed* activity counters, while this picks the storage key of a
    legacy-shared table, and re-pointing it would rewrite history.
    """
    return datetime.now(_MOSCOW_TZ).date()


def unix_ts(now: datetime, *, where: str) -> float:
    """Epoch seconds for ``now``, defensively coercing a naive input.

    The grant-expiry readers compare a stored legacy ``time.time()``
    REAL against ``now``'s epoch. ``datetime.timestamp()`` on a *naive*
    value interprets the wall clock in the HOST's local zone, so a
    naive-UTC ``now`` (:func:`db_now`) yields an epoch 10 800 s in the
    past on the MSK production host — an expired grant then keeps
    paying out for three more hours. That was a real bug: the message
    reward path fed :func:`db_now` into both
    ``PrivilegesRepo.get_active`` and ``VipRepo.get_active_profile``.

    Every caller of the readers this guards is meant to pass an aware
    datetime. A naive one is a programmer error, but raising here would
    take down the highest traffic path in the bot, so this logs loudly
    and assumes UTC. UTC is the right assumption *for these callers*
    only — it is NOT a global property of the codebase. The hourly
    sweeper deliberately runs on a naive-LOCAL clock
    (:func:`scheduler.economy_cleanup._naive_local_now`), whose
    ``.timestamp()`` already yields the correct epoch; that is why this
    guard is wired into the per-request readers (``get_active`` /
    ``get_active_profile``) and NOT into ``delete_expired`` or
    ``list_expiring_global``, which the sweeper calls. ``where`` names
    the reader so the journald line points at the caller rather than at
    this helper.
    """
    if now.tzinfo is None:
        logger.error(
            "naive datetime passed to {} - assuming UTC; the caller must pass "
            "an aware datetime.now(UTC), not utils.time.db_now()",
            where,
        )
        return now.replace(tzinfo=UTC).timestamp()
    return now.timestamp()


def to_naive_utc(value: datetime) -> datetime:
    """Naive-UTC form of ``value`` for a stored ``DateTime`` column.

    The mirror of :func:`unix_ts`. A handler that has to hand ONE
    ``now`` both to an epoch comparison (which needs it aware, see
    :func:`unix_ts`) and to a naive-UTC ``DateTime`` column cannot
    satisfy both with one value; the repo converts at the column
    instead of the handler picking a frame that is wrong for one of
    its two consumers.

    An aware input is converted; a naive one is returned unchanged, on
    the assumption it is already in this pipeline's naive-UTC storage
    convention (:func:`db_now`). Naive-LOCAL values must not be passed
    here — the sweeper's clock is the only such value in the codebase
    and it never reaches a naive-UTC column.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def local_now(tz_name: str | None) -> datetime:
    """Aware ``now`` in ``tz_name``, falling back to UTC.

    Used by the ``/start`` time-of-day greeting (RR-6 #60). Legacy read
    the *server's* local hour (``bot.py:15965``), so a user in another
    timezone got "good morning" at midnight; honouring the ``/timezone``
    preference the user already set is the same feature, done right.

    Silent on failure — an unknown or malformed tz degrades to UTC
    rather than blowing up the highest-traffic command in the bot.
    """
    if tz_name:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            # Same failure set :func:`format_local_time` swallows — a
            # stale/garbage row in ``user_settings`` must not 500 /start.
            pass
    return datetime.now(UTC)


def day_period(now: datetime) -> str:
    """Bucket ``now`` into ``morning`` / ``day`` / ``evening`` / ``night``.

    Boundaries are legacy ``get_greeting``'s verbatim (``bot.py:15966``):
    05–12 morning, 12–18 day, 18–23 evening, everything else night. The
    return value is an i18n key *suffix* (``h_start_greet_<period>``), so
    the copy stays in YAML while the arithmetic stays testable here.
    """
    hour = now.hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "day"
    if 18 <= hour < 23:
        return "evening"
    return "night"


_WEEKDAY_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


_WEEKDAY_EN = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def format_local_time(tz_name: str, lang: str = "ru") -> tuple[str, str, str]:
    """Return ``(time, date, weekday)`` strings for ``tz_name``.

    On any failure (unknown tz, malformed name) returns three empty
    strings — the handler tests truthiness of the first element to
    decide whether the name is valid. Legacy semantics; not a
    Pythonic ``raise`` because callers want the "either show or
    reject" branch to be a single bool check.

    ``lang`` picks the weekday vocabulary. It defaults to Russian so
    every existing call site keeps its exact output; an ``en`` caller
    gets an English weekday instead of the Cyrillic word that used to
    leak into otherwise-English cards.
    """
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # ZoneInfoNotFoundError covers "Etc/UnknownCity"; ValueError
        # covers e.g. embedded NUL bytes; OSError covers tzdata read
        # errors on broken systems. Anything else (TypeError on a
        # non-str arg) is a programmer bug — let it surface.
        return "", "", ""
    now = datetime.now(zone)
    weekdays = _WEEKDAY_EN if lang == "en" else _WEEKDAY_RU
    return (
        now.strftime("%H:%M:%S"),
        now.strftime("%d.%m.%Y"),
        weekdays[now.weekday()],
    )
