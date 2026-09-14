"""``/ban`` duration vocabulary — RR-4 #39.

The port had dropped legacy's ``[duration]`` argument entirely, so every
ban was permanent. Restoring it puts a parser in front of a destructive
moderation action, and the failure modes of that parser are asymmetric:
reading a duration where the admin meant something else silently changes
who gets banned and for how long, while failing to read one only costs a
retype. These tests pin the asymmetry.

The sharpest case is the bare number. ``/ban 999999`` is a numeric user
id, but it is also exactly the shape of an hour count — legacy resolved
the collision by demanding an explicit unit outside reply form
(bot.py:31263) and so do we, which is why
:func:`parse_ban_duration` takes ``allow_bare_number`` rather than
guessing.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers.moderation import (
    BAN_PERMANENT,
    effective_ban_seconds,
    effective_mute_seconds,
    parse_ban_duration,
)

_HOUR = 3600
_DAY = 86400


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("30m", 1800),
        ("2h", 2 * _HOUR),
        ("7d", 7 * _DAY),
        ("1w", 7 * _DAY),
        ("45s", 45),
        # Long forms come free with the /mute regex — an admin who
        # writes them out should not be told they are wrong.
        ("10min", 600),
        ("3hr", 3 * _HOUR),
        ("2days", 2 * _DAY),
        ("1week", 7 * _DAY),
        # Case is not a signal.
        ("2H", 2 * _HOUR),
        ("7D", 7 * _DAY),
        # Trailing punctuation from "/ban 24, спам" (legacy
        # _moderation_clean_token, bot.py:31252).
        ("24h,", 24 * _HOUR),
        ("7d.", 7 * _DAY),
    ],
)
def test_units_parse_to_seconds(token: str, expected: int) -> None:
    # The unit is explicit, so the bare-number flag cannot matter.
    assert parse_ban_duration(token, allow_bare_number=False) == expected
    assert parse_ban_duration(token, allow_bare_number=True) == expected


@pytest.mark.parametrize(
    "token",
    ["0", "forever", "permanent", "permanently", "∞", "навсегда", "FOREVER", "Навсегда"],
)
def test_permanent_words(token: str) -> None:
    """A permanent token is unambiguous in every position.

    ``"0"`` included: it cannot be a user id (Telegram ids start at 1)
    and it cannot be an hour count worth honouring, so reading it as
    "no expiry" costs nothing and matches legacy's set (bot.py:31221).
    """
    assert parse_ban_duration(token, allow_bare_number=False) == BAN_PERMANENT
    assert parse_ban_duration(token, allow_bare_number=True) == BAN_PERMANENT


def test_explicit_zero_length_is_permanent() -> None:
    """``/ban 0h`` is not a zero-second ban — nothing sensible is."""
    assert parse_ban_duration("0h", allow_bare_number=False) == BAN_PERMANENT
    assert parse_ban_duration("0d", allow_bare_number=False) == BAN_PERMANENT


@pytest.mark.parametrize("token", ["24", "168", "1", "999999"])
def test_bare_number_is_hours_only_when_allowed(token: str) -> None:
    """Hours, matching the command help and the FAQ — never minutes.

    Legacy's parser defaulted a unitless token to minutes while legacy's
    own copy promised hours ("/ban [часы]", ru.yaml:871). An admin who
    types 168 wants a week; giving them 2.8 hours lets the troll back in
    before anyone notices, so the documented promise wins.
    """
    assert parse_ban_duration(token, allow_bare_number=True) == int(token) * _HOUR
    # …and in argument form the very same token is a user id.
    assert parse_ban_duration(token, allow_bare_number=False) is None


@pytest.mark.parametrize(
    "token",
    ["", "   ", "@user", "spam", "спам", "7x", "d7", "-5h", "1.5h", "abc123"],
)
def test_non_durations_return_none(token: str) -> None:
    """``None`` is "not a duration", and it must stay distinguishable
    from ``BAN_PERMANENT`` — the caller uses one to keep looking for a
    target or a reason and the other to skip ``until_date`` entirely.
    """
    assert parse_ban_duration(token, allow_bare_number=True) is None


def test_none_and_permanent_are_not_interchangeable() -> None:
    """Guards the sentinel choice itself: ``BAN_PERMANENT`` is falsy."""
    assert BAN_PERMANENT is not None
    assert parse_ban_duration("forever", allow_bare_number=False) is not None


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (BAN_PERMANENT, BAN_PERMANENT),
        (3600, 3600),
        (7 * _DAY, 7 * _DAY),
        (366 * _DAY, 366 * _DAY),
        # Past Telegram's ceiling the ban IS permanent — reporting a
        # finite term the API will not honour would be a lie.
        (367 * _DAY, BAN_PERMANENT),
        (10 * 365 * _DAY, BAN_PERMANENT),
        # Under Telegram's 30s floor a ban silently becomes permanent,
        # which is the worst possible surprise for "/ban 5s". Round up.
        (5, 60),
        (30, 60),
        (59, 60),
        (61, 61),
    ],
)
def test_effective_seconds_clamps_to_what_telegram_honours(requested: int, expected: int) -> None:
    assert effective_ban_seconds(requested) == expected


# --- #1734: /mute shares the floor, not just the ceiling -----------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        # The bug: under Telegram's 30s floor an ``until_date`` means
        # PERMANENT, so "/mute 10s" silenced a member forever and then
        # reported ten seconds. ``/ban`` has rounded up since RR-4 #39.
        (0, 60),
        (1, 60),
        (10, 60),
        (29, 60),
        (59, 60),
        # Above the floor nothing is touched.
        (60, 60),
        (61, 61),
        (1800, 1800),
        (366 * _DAY, 366 * _DAY),
        # The ceiling keeps its old behaviour: a mute is always finite,
        # so an over-long request is clamped rather than made permanent
        # the way ``effective_ban_seconds`` does it.
        (367 * _DAY, 366 * _DAY),
        (99_999 * _DAY, 366 * _DAY),
    ],
)
def test_effective_mute_seconds_clamps_both_edges(requested: int, expected: int) -> None:
    assert effective_mute_seconds(requested) == expected


def test_a_mute_is_never_permanent() -> None:
    """``BAN_PERMANENT`` is a legal return for a ban and never for a mute.

    ``group_events._restriction_is_captchas`` discriminates a captcha
    hold from a moderator's mute on exactly this property — the absence
    of an expiry — so a mute that came back permanent would also be
    liftable by anyone who found a stale captcha button.
    """
    for requested in (0, 1, 29, 367 * _DAY, 10 * 365 * _DAY):
        assert effective_mute_seconds(requested) != BAN_PERMANENT
        assert effective_mute_seconds(requested) >= 60
