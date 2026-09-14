"""Unit tests for the two admin predicates in ``utils/telegram_admin`` (#337).

The narrow one (:func:`has_moderation_rights` / :func:`is_user_admin`)
answers "may this person moderate?" and ports legacy
``telegram_admin_has_mod_rights`` (bot.py:7455-7476). The wide one
(:func:`is_chat_admin_any`) answers "is this person staff?" and has no
legacy counterpart — it exists so a non-authority caller (antiflood's
mute exemption) cannot accidentally reuse the authority verdict.

Both keep the three-valued contract: a Telegram-API error is ``None``,
never a bool, so every caller picks its own fail direction.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.enums import ChatMemberStatus

from telegram_invite_bot.utils.telegram_admin import (
    _MODERATION_RIGHTS,
    has_moderation_rights,
    is_chat_admin_any,
    is_user_admin,
)

CHAT = -100123
USER = 777


class _FakeBot:
    """``get_chat_member`` returning a fixed member, or raising."""

    def __init__(self, member: object | None) -> None:
        self._member = member
        self.calls = 0

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls += 1
        if self._member is None:
            raise RuntimeError("telegram down")
        return self._member


def _member(status: str, **rights: bool) -> SimpleNamespace:
    return SimpleNamespace(status=status, user=SimpleNamespace(id=USER), **rights)


def _bot(member: object | None) -> Bot:
    return cast("Bot", _FakeBot(member))


# ---------------------------------------------------------------------------
# has_moderation_rights — the pure predicate
# ---------------------------------------------------------------------------


def test_creator_needs_no_explicit_right() -> None:
    """bot.py:7457-7458 short-circuits on status before reading any flag."""
    assert has_moderation_rights(_member(ChatMemberStatus.CREATOR)) is True


@pytest.mark.parametrize("right", _MODERATION_RIGHTS)
def test_any_single_right_is_enough(right: str) -> None:
    """Legacy ORs the five flags (bot.py:7469-7473) — one suffices."""
    assert has_moderation_rights(_member(ChatMemberStatus.ADMINISTRATOR, **{right: True})) is True


def test_titular_administrator_has_no_moderation_rights() -> None:
    """The #337 case: promoted for a title, granted nothing that moderates."""
    titular = _member(
        ChatMemberStatus.ADMINISTRATOR,
        can_invite_users=True,
        can_change_info=True,
        can_manage_topics=True,
    )
    assert has_moderation_rights(titular) is False


def test_administrator_with_all_rights_false_is_refused() -> None:
    off = dict.fromkeys(_MODERATION_RIGHTS, False)
    assert has_moderation_rights(_member(ChatMemberStatus.ADMINISTRATOR, **off)) is False


@pytest.mark.parametrize(
    "status",
    [
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    ],
)
def test_non_admin_statuses_refused_even_with_rights(status: str) -> None:
    """A restricted user can carry permission flags; status decides first."""
    assert has_moderation_rights(_member(status, can_restrict_members=True)) is False


def test_missing_attributes_do_not_raise() -> None:
    """aiogram models the statuses as distinct classes, so flags are absent."""
    assert has_moderation_rights(SimpleNamespace()) is False
    assert has_moderation_rights(_member(ChatMemberStatus.ADMINISTRATOR)) is False


# ---------------------------------------------------------------------------
# is_user_admin / is_chat_admin_any — the two probes over the same call
# ---------------------------------------------------------------------------


async def test_narrow_and_wide_disagree_on_a_titular_admin() -> None:
    """The whole point of the split: same member, two answers."""
    titular = _member(ChatMemberStatus.ADMINISTRATOR, can_invite_users=True)
    assert await is_user_admin(_bot(titular), CHAT, USER) is False
    assert await is_chat_admin_any(_bot(titular), CHAT, USER) is True


async def test_both_agree_on_a_real_moderator() -> None:
    mod = _member(ChatMemberStatus.ADMINISTRATOR, can_restrict_members=True)
    assert await is_user_admin(_bot(mod), CHAT, USER) is True
    assert await is_chat_admin_any(_bot(mod), CHAT, USER) is True


async def test_both_agree_on_a_plain_member() -> None:
    plain = _member(ChatMemberStatus.MEMBER)
    assert await is_user_admin(_bot(plain), CHAT, USER) is False
    assert await is_chat_admin_any(_bot(plain), CHAT, USER) is False


async def test_api_error_is_none_from_both_probes() -> None:
    """Never a bool — an outage must not become a permission decision."""
    assert await is_user_admin(_bot(None), CHAT, USER) is None
    assert await is_chat_admin_any(_bot(None), CHAT, USER) is None


async def test_each_probe_makes_exactly_one_api_call() -> None:
    fake = _FakeBot(_member(ChatMemberStatus.CREATOR))
    await is_user_admin(cast("Bot", fake), CHAT, USER)
    await is_chat_admin_any(cast("Bot", fake), CHAT, USER)
    assert fake.calls == 2
