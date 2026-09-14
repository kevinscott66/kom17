"""Argument-parsing pins for ``/cpc`` (#1666, #1667).

``handlers/rps._parse_args`` is the only place a ``/cpc`` argument
becomes a number, and for a long time it did that with a bare ``int()``.
Two separate problems rode on that:

* ``int()`` is wider than this package's parse policy — Unicode decimal
  digits, underscore separators and a leading sign all get through it
  while every other money command in the package refuses them.
* Telegram spells group and channel ids negative, and nothing below the
  parser checked the sign. ``/cpc -100... 100`` therefore made the bot
  post a live challenge card into an arbitrary chat.

These tests pin the parser directly rather than through a wired
dispatcher: the parser is the whole fix, and a unit test says so without
the cost of a fake update.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers.rps import _parse_args


def test_a_username_recipient_still_parses() -> None:
    assert _parse_args("@alice 100") == ("username", "alice", 100)


def test_a_positive_numeric_recipient_still_parses() -> None:
    assert _parse_args("123456789 100") == ("id", 123456789, 100)


def test_a_trailing_comment_is_still_ignored() -> None:
    # ``split(maxsplit=2)`` keeps the third field out of the parse; the
    # command has never had a comment field, and this pins that adding
    # text does not change the two values that matter.
    assert _parse_args("@alice 100 привет") == ("username", "alice", 100)


@pytest.mark.parametrize(
    "raw",
    [
        "-1001234567890 100",  # supergroup
        "-100 100",  # any negative
        "0 100",  # the one non-positive value the sign check misses
    ],
)
def test_a_non_positive_recipient_is_refused(raw: str) -> None:
    # #1667: without this the bot posts a challenge card into a chat the
    # caller merely knows the id of. No money can move through it — the
    # accept handler compares the clicker's user id against
    # ``opponent_id`` — but the message injection is real.
    assert _parse_args(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "@alice ١٠٠",  # Arabic-Indic digits: int() reads this as 100
        "@alice 1_0_0",  # underscore separators: int() reads this as 100
        "@alice +100",  # leading sign
        "@alice ²",  # str.isdigit() says True, int() raises
        "@alice 10.5",
        "@alice сто",
    ],
)
def test_a_bet_outside_the_parse_policy_is_refused(raw: str) -> None:
    # #1666: each of these is something bare ``int()`` either accepts
    # outright or crashes on. The policy is one ASCII digit run.
    assert _parse_args(raw) is None


@pytest.mark.parametrize("raw", ["١٠٠ 100", "1_0_0 100", "+100 100"])
def test_a_recipient_id_outside_the_parse_policy_is_refused(raw: str) -> None:
    assert _parse_args(raw) is None


@pytest.mark.parametrize("raw", ["@alice 0", "@alice -5", "@alice", "", "@ 100", "100"])
def test_the_old_rejections_still_hold(raw: str) -> None:
    # Nothing the parser used to refuse became acceptable.
    assert _parse_args(raw) is None
