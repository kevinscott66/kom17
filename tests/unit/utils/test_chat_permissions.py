"""Guards for the shared restriction/lift permission sets (#247).

The bug these replace was invisible for a reason worth restating: the
old literals passed ``can_send_media_messages``, a field aiogram 3.28
no longer models, and ``extra="allow"`` let it through in silence. No
exception, no warning, no failing test — the flag simply stopped
meaning anything. A test that only checked the flags we remembered to
write would have kept passing throughout.

So these assert against ``ChatPermissions.model_fields`` itself: every
field the installed Bot API knows about must appear in both sets. When
Telegram adds the next permission and aiogram models it, this fails
instead of quietly leaving it ungranted.
"""

from __future__ import annotations

from aiogram.types import ChatPermissions

from telegram_invite_bot.utils.chat_permissions import (
    MUTED_PERMS,
    UNRESTRICTED_PERMS,
)


def test_muted_perms_denies_every_known_permission() -> None:
    dumped = MUTED_PERMS.model_dump(exclude_none=True)

    assert set(dumped) == set(ChatPermissions.model_fields)
    assert all(value is False for value in dumped.values())


def test_unrestricted_perms_grants_every_known_permission() -> None:
    """All-True is the documented way to lift a restriction outright.

    Anything less leaves the user in ``ChatMemberRestricted`` rather
    than handing them back plain ``member`` status.
    """
    dumped = UNRESTRICTED_PERMS.model_dump(exclude_none=True)

    assert set(dumped) == set(ChatPermissions.model_fields)
    assert all(value is True for value in dumped.values())


def test_the_two_sets_are_exact_inverses() -> None:
    """The comment that claimed this used to be false — now it holds."""
    muted = MUTED_PERMS.model_dump(exclude_none=True)
    lifted = UNRESTRICTED_PERMS.model_dump(exclude_none=True)

    assert set(muted) == set(lifted)
    assert all(muted[key] is not lifted[key] for key in muted)


def test_no_set_carries_the_pre_bot_api_65_media_flag() -> None:
    """``can_send_media_messages`` split into six flags in Bot API 6.5.

    aiogram accepts it as an ``extra`` and Telegram ignores it, so a
    reintroduced copy would look correct and grant nothing.
    """
    for perms in (MUTED_PERMS, UNRESTRICTED_PERMS):
        assert "can_send_media_messages" not in perms.model_dump()
