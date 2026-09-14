"""``core.deep_links`` — the payload namespace shared by three buttons.

The module is pure string work, which is exactly why it is worth
pinning: every group→DM button in the bot renders through
:func:`dm_start_url`, and :func:`parse_group_payload` reads back what a
user could have retyped by hand.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.deep_links import (
    GROUP_PREFIX,
    dm_start_url,
    group_payload,
    parse_group_payload,
)


def test_url_without_a_group_is_the_plain_start_link() -> None:
    """The shape every one of these buttons used to hard-code — still
    correct where there is no group behind the tap."""
    assert dm_start_url("kom17bot") == "https://t.me/kom17bot?start"


def test_url_with_a_group_carries_the_payload() -> None:
    assert (
        dm_start_url("kom17bot", group_chat_id=-1001234567890)
        == "https://t.me/kom17bot?start=grp_-1001234567890"
    )


def test_payload_round_trips() -> None:
    assert parse_group_payload(group_payload(-1001234567890)) == -1001234567890


def test_payload_fits_telegrams_limit() -> None:
    """Telegram caps a start payload at 64 characters. A chat id is 14
    digits at the outside, so the prefix has room — asserted rather than
    assumed, because a longer prefix would break silently: Telegram
    drops the whole link rather than truncating it.
    """
    assert len(group_payload(-1009999999999999)) <= 64


@pytest.mark.parametrize(
    "args",
    [
        None,
        "",
        "ref_123",  # another namespace
        "check_123",
        GROUP_PREFIX,  # prefix with no id
        "grp_abc",
        "grp_-",
        "grp_12.5",
        "grp_+100",
        "grp_ 100",  # inner space survives the strip
        "grp_1_000",  # int() would accept this; Telegram never sends it
        "grp_١٢٣",  # arabic-indic digits: str.isdigit() is True
        "grp_0",  # no chat has id 0
    ],
)
def test_malformed_payloads_are_none(args: str | None) -> None:
    """Every rejection is the same ``None`` — the caller's next move is
    identical for all of them, and a deep link is user-editable."""
    assert parse_group_payload(args) is None


def test_positive_ids_parse_too() -> None:
    """Group ids are negative today, but a payload naming a positive id
    is well-formed and the parser is not the place to encode the sign
    convention — the caller checks ``bot_groups``."""
    assert parse_group_payload("grp_100") == 100
