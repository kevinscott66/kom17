"""Unit tests for the private formatter helpers in ``handlers.profile``.

The e2e suite renders ``/profile`` end-to-end through a real DB, but
the cold-row defensive branches inside :func:`_fmt_dt` and the
"unknown value type" arm of the renderer can't be hit through the
ORM — the column types coerce SQLite TEXT back into ``datetime`` on
read. Yet the helpers are typed against ``object`` precisely because
they accept whatever the legacy renderer used to feed them (string,
int timestamp, ``None``, datetime).

Locking the ``return "—"`` fallback as a unit test keeps the
end-of-history "—" placeholder stable: a regression that returned
``str(value)`` would surface a Python repr (``"datetime.datetime(...)"``
or ``"1700000000"``) into the user-visible card.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from telegram_invite_bot.handlers.profile import _fmt_dt, _user_tz


def test_fmt_dt_renders_datetime_at_minute_precision() -> None:
    assert _fmt_dt(datetime(2025, 1, 2, 14, 30, 45)) == "2025-01-02 14:30"


def test_fmt_dt_returns_em_dash_for_none() -> None:
    """``None`` is the cold-row case (column never written). Must
    render as the em-dash placeholder, not the literal ``"None"``.
    """
    assert _fmt_dt(None) == "—"


def test_fmt_dt_returns_em_dash_for_unknown_value_type() -> None:
    """The helper is typed ``object`` because the legacy renderer
    occasionally fed it ints (Unix timestamps) and ISO strings (the
    TEXT-column era). The current contract: anything that isn't a
    real ``datetime`` collapses to "—". A future refactor that tried
    to parse strings here would need to be intentional, not silent.
    """
    assert _fmt_dt("2025-01-02 14:30") == "—"
    assert _fmt_dt(1_700_000_000) == "—"
    assert _fmt_dt(object()) == "—"


def test_fmt_dt_converts_naive_utc_into_the_requested_zone() -> None:
    """Regression (#476): stored stamps are naive UTC, not display-local.

    The DM card prints the user's ``/timezone`` preference two lines
    above these stamps, so rendering the raw UTC value under that label
    told a Moscow reader the wrong hour.
    """
    stamp = datetime(2025, 1, 2, 23, 30)
    assert _fmt_dt(stamp, ZoneInfo("Europe/Moscow")) == "2025-01-03 02:30"


def test_fmt_dt_without_a_zone_keeps_the_stored_value() -> None:
    """``tz=None`` stays the pre-#476 behaviour: print what is stored.

    Callers that have nothing on the card claiming a timezone (and the
    unit tests above) rely on this, so the parameter is optional rather
    than required.
    """
    stamp = datetime(2025, 1, 2, 23, 30)
    assert _fmt_dt(stamp) == "2025-01-02 23:30"


def test_fmt_dt_does_not_double_shift_an_aware_value() -> None:
    """An already-aware stamp is converted, not re-stamped.

    ``.replace(tzinfo=UTC)`` on an aware value would silently relabel it
    and shift the result by the original offset.
    """
    stamp = datetime(2025, 1, 2, 23, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    assert _fmt_dt(stamp, ZoneInfo("Europe/Moscow")) == "2025-01-02 23:30"


def test_user_tz_falls_back_to_utc_for_a_garbage_preference() -> None:
    """A stale/garbage ``user_settings.timezone`` must not 500 the card.

    Same failure set :func:`utils.time.local_now` swallows.
    """
    assert _user_tz("Europe/Moscow") == ZoneInfo("Europe/Moscow")
    assert _user_tz(None) is UTC
    assert _user_tz("") is UTC
    assert _user_tz("Not/AZone") is UTC
    assert _user_tz("../../etc/passwd") is UTC
