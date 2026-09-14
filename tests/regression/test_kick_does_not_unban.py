"""A kick must never lift a ban it did not place (#2031).

Telegram has no "kick", so the bot spells one as a ban followed by an
unban (``utils/telegram_kick.py``). Read the pair from the outside and
the second half is a privileged *lift*, run unconditionally, on whatever
ban happens to be standing at that moment. The pair's own guard —
``only_if_banned=True`` — cannot see the difference, because by the time
it is evaluated the bot itself is the banner.

Nothing upstream caught it either. Every guard on the way into ``/kick``
asks about the target's *rank*: ``_check_target_ok`` refuses admins and
bots, ``_check_rank_target_ok`` refuses someone ranked above the caller.
None of them asks whether the target is in the chat at all, and a banned
user answers ``kicked``, which is not an admin status. So:

* a moderator whose rank grants ``can_kick`` and explicitly withholds
  ``can_ban`` (``core/ranks.py:311-312``) could run ``/kick <banned id>``
  and lift a ban they were not trusted to place — logged as
  ``action="kick"``, so ``/modlog`` showed a removal where an unban had
  happened, and none of the state ``/unban`` clears was cleared;
* and the captcha timeout (``handlers/group_events.py``) reached the same
  end with nobody typing anything: ban a joiner while their captcha is
  still pending and the timer, which nothing cancels on a ban, fired and
  handed the ban back.

The fix is one round trip: probe the membership before banning, and
refuse outright when the target is already banned. These tests pin both
halves — that the primitive refuses, and that neither caller papers over
the refusal with a success line, a success log or an audit row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.models.moderation import ModerationLog
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.group_events import _PENDING_CAPTCHA, _expire_captcha
from telegram_invite_bot.utils.telegram_kick import KickOutcome, kick_member

_CHAT = -1005550009
_USER = 5151
_BOT_ID = 1000
_NOTICE_ID = 88


class _FakeBot:
    """Duck-typed Bot that answers the membership probe and records calls.

    ``status`` is the whole point: it is what the probe reads, and the
    difference between ``member`` and ``kicked`` has to change what the
    rest of the pair does.
    """

    def __init__(self, *, status: str = ChatMemberStatus.MEMBER, fail_probe: bool = False) -> None:
        self.id = _BOT_ID
        self.calls: list[str] = []
        self._status = status
        self._fail_probe = fail_probe

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls.append("probe")
        if self._fail_probe:
            raise TelegramBadRequest(method=cast("Any", None), message="bad gateway")
        return SimpleNamespace(status=self._status, user=SimpleNamespace(id=user_id))

    async def ban_chat_member(self, chat_id: int, user_id: int, **_kw: Any) -> None:
        self.calls.append("ban")

    async def unban_chat_member(self, chat_id: int, user_id: int, **_kw: Any) -> None:
        self.calls.append("unban")

    async def restrict_chat_member(self, chat_id: int, user_id: int, **_kw: Any) -> None:
        self.calls.append("restrict")

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append("delete")


@pytest.fixture(autouse=True)
def _clean_pending() -> Any:
    _PENDING_CAPTCHA.clear()
    yield
    _PENDING_CAPTCHA.clear()


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'moderation.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(ModerationBase.metadata.create_all)
    reg = EngineRegistry(
        engines={DBName.MODERATION: engine},
        sessions={DBName.MODERATION: async_sessionmaker(engine, expire_on_commit=False)},
    )
    try:
        yield reg
    finally:
        await reg.dispose()


async def _log_rows(reg: EngineRegistry) -> list[ModerationLog]:
    async with reg.session(DBName.MODERATION)() as session:
        result = await session.execute(select(ModerationLog))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------


async def test_a_kick_aimed_at_a_banned_user_lifts_nothing() -> None:
    """The whole bug in one assertion: no unban leaves the fake.

    ``KICKED`` is Telegram's word for "banned", which is why the status
    reads backwards here and why every rank guard upstream waved it
    through.
    """
    bot = _FakeBot(status=ChatMemberStatus.KICKED)

    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, backoff_sec=0)

    assert outcome is KickOutcome.ALREADY_BANNED
    assert bot.calls == ["probe"]


async def test_the_refusal_is_not_spelled_as_a_failure() -> None:
    """``FAILED`` means "retry might help"; this is not that case.

    Callers compensate for ``FAILED`` — the captcha path lifts its mute
    and leaves the joiner in the chat. Doing that for a banned user would
    be an unmute for somebody who is not there.
    """
    bot = _FakeBot(status=ChatMemberStatus.KICKED)

    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, backoff_sec=0)

    assert outcome is not KickOutcome.FAILED
    assert outcome is not KickOutcome.REMOVED


async def test_an_ordinary_member_is_still_kicked() -> None:
    """The probe is a gate, not a brake: the normal path is unchanged."""
    bot = _FakeBot(status=ChatMemberStatus.MEMBER)

    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, backoff_sec=0)

    assert outcome is KickOutcome.REMOVED
    assert bot.calls == ["probe", "ban", "unban"]


async def test_a_restricted_member_is_still_kicked() -> None:
    """Muted is not banned.

    The captcha mutes every joiner, so ``restricted`` is the status the
    timeout path meets on its own happy path — a probe that refused it
    would disable the captcha kick entirely.
    """
    bot = _FakeBot(status=ChatMemberStatus.RESTRICTED)

    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, backoff_sec=0)

    assert outcome is KickOutcome.REMOVED
    assert bot.calls == ["probe", "ban", "unban"]


async def test_an_unreadable_membership_refuses_to_guess() -> None:
    """A lost probe means the kick does not run.

    This deliberately widens what can fail. The alternative is running
    the unban half without knowing whose ban it would lift, which is the
    defect itself — and ``FAILED`` already means "nothing happened" to
    every caller.
    """
    bot = _FakeBot(fail_probe=True)

    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, backoff_sec=0)

    assert outcome is KickOutcome.FAILED
    assert bot.calls == ["probe"]


# ---------------------------------------------------------------------------
# The captcha timeout — the caller nobody has to invoke
# ---------------------------------------------------------------------------


async def test_the_captcha_timeout_does_not_hand_back_a_moderators_ban(
    registry: EngineRegistry,
) -> None:
    """Ban a joiner mid-captcha and the timer used to undo it.

    No command, no callback, no attacker: the ban is ordinary moderation
    and the timer is already armed. Whoever banned them got their ban
    silently reverted seconds later by the bot.
    """
    bot = _FakeBot(status=ChatMemberStatus.KICKED)
    _PENDING_CAPTCHA[(_CHAT, _USER)] = cast("Any", object())

    await _expire_captcha(cast("Any", bot), registry, _CHAT, _USER, _NOTICE_ID)

    assert "unban" not in bot.calls
    assert "ban" not in bot.calls


async def test_the_timeout_does_not_unmute_the_banned_joiner_either(
    registry: EngineRegistry,
) -> None:
    """``ALREADY_BANNED`` must not fall into the ``FAILED`` branch.

    That branch lifts the captcha restriction, on the theory that a kick
    which never landed left a joiner sitting muted (#285). Here the
    joiner is not sitting anywhere, and restoring permissions would be
    the mute-shaped twin of the ban-shaped bug above.
    """
    bot = _FakeBot(status=ChatMemberStatus.KICKED)
    _PENDING_CAPTCHA[(_CHAT, _USER)] = cast("Any", object())

    await _expire_captcha(cast("Any", bot), registry, _CHAT, _USER, _NOTICE_ID)

    assert "restrict" not in bot.calls


async def test_the_audit_row_says_which_ending_this_was(
    registry: EngineRegistry,
) -> None:
    """One row, and it does not claim a removal this timer performed."""
    bot = _FakeBot(status=ChatMemberStatus.KICKED)
    _PENDING_CAPTCHA[(_CHAT, _USER)] = cast("Any", object())

    await _expire_captcha(cast("Any", bot), registry, _CHAT, _USER, _NOTICE_ID)

    rows = await _log_rows(registry)
    assert len(rows) == 1
    # ``details`` carries the timeout's own prefix, then the outcome —
    # substring rather than equality so this pins the outcome, not the
    # prefix's spelling.
    assert rows[0].details is not None
    assert rows[0].details.endswith(KickOutcome.ALREADY_BANNED.value)
