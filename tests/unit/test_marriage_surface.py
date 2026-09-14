"""Unit tests for the L-03..L-07 marriage command-surface parse helpers.

These pin the pure parse/clamp logic added to
:mod:`telegram_invite_bot.handlers.marriage` (Wave 1-A) in isolation from
Telegram I/O and the DB. The handlers themselves are thin parse-and-
delegate shells over ``BondsWriteRepo`` (covered by the repo integration
tests); what is worth pinning here is:

* ``_parse_extend_days`` — first positive int, clamped to [1, 365]
  (mirrors bot.py:22774).
* the ``_AUTO_DIVORCE_MODES`` ru/en/digit alias collapse
  (mirrors bot.py:22804-22816).
* ``_resolve_target_user`` — reply / text_mention resolution, bot skip
  (mirrors bot.py:22714).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from aiogram.filters import CommandObject

from telegram_invite_bot.handlers.marriage import (
    _AUTO_DIVORCE_MODES,
    _MARRIAGE_EXTEND_COST_PER_DAY,
    _MARRIAGE_EXTEND_MAX_DAYS,
    _parse_extend_days,
    _resolve_target_user,
)


def _cmd(args: str | None) -> CommandObject:
    return CommandObject(command="marry_extend", args=args)


# --- _parse_extend_days ----------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("5", 5),
        ("1", 1),
        ("365", 365),
        # Clamp high → 365
        ("1000", _MARRIAGE_EXTEND_MAX_DAYS),
        # Clamp low: "0" is a digit but max(1, 0) → 1
        ("0", 1),
        # First positive integer wins; leading non-digit token skipped
        ("days 7", 7),
        ("7 14", 7),
        # No digit anywhere → None (usage prompt)
        ("", None),
        (None, None),
        ("abc", None),
        # A negative is not ``.isdigit()`` so it is skipped
        ("-3", None),
    ],
)
def test_parse_extend_days(args: str | None, expected: int | None) -> None:
    assert _parse_extend_days(_cmd(args)) == expected


def test_extend_cost_constant() -> None:
    # Locks the legacy 10-coins/day rate (bot.py:22725).
    assert _MARRIAGE_EXTEND_COST_PER_DAY == 10


# --- auto-divorce mode alias table -----------------------------------------


@pytest.mark.parametrize(
    ("token", "canonical"),
    [
        ("один", "one"),
        ("one", "one"),
        ("1", "one"),
        ("два", "two"),
        ("two", "two"),
        ("2", "two"),
        ("выключить", "off"),
        ("off", "off"),
        ("отключить", "off"),
    ],
)
def test_auto_divorce_mode_aliases(token: str, canonical: str) -> None:
    assert _AUTO_DIVORCE_MODES[token] == canonical


def test_auto_divorce_canonicals_are_cyrillic_free_values() -> None:
    # The stored value (written to the marriages.auto_divorce column) must
    # be one of the three ascii canonicals regardless of input language.
    assert set(_AUTO_DIVORCE_MODES.values()) == {"one", "two", "off"}


def test_auto_divorce_unknown_token_absent() -> None:
    assert "three" not in _AUTO_DIVORCE_MODES
    assert "три" not in _AUTO_DIVORCE_MODES


# --- _resolve_target_user --------------------------------------------------


@dataclass
class _FakeUser:
    id: int
    is_bot: bool = False


@dataclass
class _FakeEntity:
    type: str
    user: _FakeUser | None = None


@dataclass
class _FakeReply:
    from_user: _FakeUser | None


@dataclass
class _FakeMessage:
    reply_to_message: _FakeReply | None = None
    entities: list[_FakeEntity] | None = None


def test_resolve_target_prefers_reply() -> None:
    u = _FakeUser(id=42)
    msg = _FakeMessage(reply_to_message=_FakeReply(from_user=u))
    assert _resolve_target_user(msg) is u  # type: ignore[arg-type]


def test_resolve_target_text_mention_fallback() -> None:
    u = _FakeUser(id=7)
    msg = _FakeMessage(
        reply_to_message=None,
        entities=[_FakeEntity(type="text_mention", user=u)],
    )
    assert _resolve_target_user(msg) is u  # type: ignore[arg-type]


def test_resolve_target_plain_mention_unresolved() -> None:
    # A bare @username mention carries no user id → not resolvable, like
    # legacy. Only text_mention entities have a ``.user``.
    msg = _FakeMessage(
        reply_to_message=None,
        entities=[_FakeEntity(type="mention", user=None)],
    )
    assert _resolve_target_user(msg) is None  # type: ignore[arg-type]


def test_resolve_target_none_when_no_reply_no_entities() -> None:
    assert _resolve_target_user(_FakeMessage()) is None  # type: ignore[arg-type]
