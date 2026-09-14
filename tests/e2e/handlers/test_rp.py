"""End-to-end tests for the romance RP-action subsystem (FEAT-RP).

Each test feeds an aiogram :class:`Update` through the full dispatcher
(including the ``SessionMiddleware`` that attaches ``bonds_write_repo`` /
``users_repo``) and asserts on the outgoing Telegram wire calls.

Scenarios:
* married pair ``.обнять`` → flavoured render + marriage XP +10
* the same action writes a row into the pair's joint-activity log (#231)
* relationship below the action's level ``.поцеловать`` → level_required
* universal verb on a stranger ``.обнять`` → ``_general`` variant, no XP
* relationship-only verb on a stranger, caller is VIP → ``_general``
  variant (#270), and the same verb from a non-VIP, or in a group that
  switched the perk off, still refuses
* no reply ``.обнять`` → rel_rp_reply usage hint
* rate-limit: the 21st action in the window is blocked
* private ``/rp_commands`` → lists levels (private header)
* group ``.обнять`` as ordinary chatter (no prefix) falls through
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.bond_activity import (
    MarriageActivityLog,
    RelationshipActivityLog,
)
from telegram_invite_bot.db.models.economy import EconomyUser, UserGroupVip
from telegram_invite_bot.db.models.users import (
    GroupSettings,
    Marriage,
    Relationship,
    User,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _group_msg(
    text: str,
    *,
    user_id: int = 10,
    first_name: str = "Alice",
    chat_id: int = -100,
    reply_to_user_id: int | None = None,
    reply_to_first_name: str = "Bob",
    reply_to_is_bot: bool = False,
    update_id: int = 1,
) -> Any:
    return make_message_update(
        text,
        chat_id=chat_id,
        chat_type="supergroup",
        user_id=user_id,
        first_name=first_name,
        reply_to_user_id=reply_to_user_id,
        reply_to_first_name=reply_to_first_name,
        reply_to_is_bot=reply_to_is_bot,
        update_id=update_id,
    )


async def _add_marriage(registry: Any, *, exp: int = 0) -> None:
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Marriage(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=exp,
                status="active",
            )
        )
        await session.commit()


async def _enable_rp18(registry: Any, *, chat_id: int = -100) -> None:
    """Flip the per-group 18+ gate ON (AUD-4) so 18+ actions reach their
    own level/pair checks instead of the gate refusal."""
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(GroupSettings(group_id=chat_id, rp_18_enabled=1))
        await session.commit()


async def _set_vip_outside(registry: Any, *, enabled: bool, chat_id: int = -100) -> None:
    """Write the #270 per-group flag explicitly.

    Only ever called with ``enabled=False`` in these tests — the ON case
    is the *absence* of a row, which is what a real group that has never
    opened /groupadmin looks like and the whole reason the column's
    default is 1. Writing 1 here would prove the flag is readable, not
    that a never-configured group is served.
    """
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            GroupSettings(
                group_id=chat_id,
                rp_18_enabled=1,
                rp_vip_outside_enabled=int(enabled),
            )
        )
        await session.commit()


async def _grant_global_vip(registry: Any, *, user_id: int = 10, days: float = 30) -> None:
    """The grant the PORT issues (``VipRepo.grant_global``)."""
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        session.add(EconomyUser(user_id=user_id, vip_till=time.time() + days * 86400))
        await session.commit()


async def _grant_group_vip(
    registry: Any, *, user_id: int = 10, chat_id: int = -100, days: float = 30
) -> None:
    """The grant LEGACY issued for an in-group purchase (``user_group_vip``).

    Prod holds zero of these today, which is exactly why the handler
    cannot read this table alone — but a legacy-era row must keep working.
    """
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        session.add(
            UserGroupVip(user_id=user_id, group_id=chat_id, vip_till=time.time() + days * 86400)
        )
        await session.commit()


async def _add_relationship(registry: Any, *, exp: int) -> None:
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=exp,
                status="active",
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Marriage XP grant
# ---------------------------------------------------------------------------


async def test_married_pair_hug_grants_marriage_xp(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _add_marriage(registry, exp=0)

    sent = capture_outgoing(bot)
    result = await dp.feed_update(
        bot,
        _group_msg(".обнять", user_id=10, reply_to_user_id=20),
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "обнял" in body.lower()
    assert "+10" in body  # hug grants 10 XP to the couple

    # Marriage experience bumped by 10.
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        from sqlalchemy import select

        row = (await session.execute(select(Marriage).where(Marriage.chat_id == -100))).scalar_one()
        assert row.experience == 10


# ---------------------------------------------------------------------------
# Relationship below required level
# ---------------------------------------------------------------------------


async def test_relationship_below_level_blocks_kiss(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    # kiss needs level 5 (>= 30000 XP). exp=150 → level 1.
    await _add_relationship(registry, exp=150)
    # kiss is an 18+ action — enable the per-group 18+ gate so the test
    # exercises the *level* refusal (AUD-4), not the 18+-disabled refusal.
    await _enable_rp18(registry)

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".поцеловать", user_id=10, reply_to_user_id=20),
    )
    assert len(sent) == 1
    assert "уровень" in sent[0]["text"].lower()

    # No XP granted — experience unchanged.
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        from sqlalchemy import select

        row = (
            await session.execute(select(Relationship).where(Relationship.chat_id == -100))
        ).scalar_one()
        assert row.experience == 150


# ---------------------------------------------------------------------------
# Universal action on a stranger → _general, no XP
# ---------------------------------------------------------------------------


async def test_universal_stranger_hug_general_no_xp(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".обнять", user_id=10, reply_to_user_id=20),
    )
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "обнял" in body.lower()
    # The _general variant carries no "+N XP" suffix.
    assert "+" not in body


async def test_relationship_only_action_on_stranger_no_pair(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No pair AND no VIP → the refusal, unchanged (#270 leaves this alone).

    ``EconomyBase`` is in the schema list because the #270 branch now
    asks economy.db whether the caller is a VIP before it gives up. It
    is not there so the test can grant anything — nothing is granted,
    and that is the point.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    # "цветы" (flowers, level 7) is an 18+ action — enable the gate so the
    # test exercises the no-pair refusal, not the 18+-disabled refusal.
    await _enable_rp18(registry)

    sent = capture_outgoing(bot)
    # "цветы" (flowers) is relationship-only — no pair → rel_rp_no_pair.
    await dp.feed_update(
        bot,
        _group_msg(".цветы", user_id=10, reply_to_user_id=20),
    )
    assert len(sent) == 1
    assert "только для пар" in sent[0]["text"].lower()


# ---------------------------------------------------------------------------
# VIP outside a relationship (#270)
# ---------------------------------------------------------------------------


async def test_vip_performs_relationship_only_action_without_a_pair(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The perk ``/rp_commands`` advertises actually works now (#270).

    The group is never configured for VIP-outside — only the 18+ gate is
    flipped — so this also pins the default-ON reading of a missing
    ``rp_vip_outside_enabled``.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _enable_rp18(registry)
    await _grant_global_vip(registry)

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".цветы", user_id=10, reply_to_user_id=20),
    )
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "только для пар" not in body.lower()
    assert "цветы" in body.lower()
    # The _general variant carries no "+N XP" suffix — the perk buys the
    # action, not pair progress the buyer has no pair to bank.
    assert "+" not in body


async def test_legacy_group_scoped_vip_grant_still_works(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A ``user_group_vip`` row — legacy's in-group purchase — counts too."""
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _enable_rp18(registry)
    await _grant_group_vip(registry)

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".цветы", user_id=10, reply_to_user_id=20),
    )
    assert len(sent) == 1
    assert "только для пар" not in sent[0]["text"].lower()


async def test_group_that_switched_the_perk_off_still_refuses(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``rp_vip_outside_enabled = 0`` beats an active VIP grant."""
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _set_vip_outside(registry, enabled=False)
    await _grant_global_vip(registry)

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".цветы", user_id=10, reply_to_user_id=20),
    )
    assert len(sent) == 1
    assert "только для пар" in sent[0]["text"].lower()


async def test_expired_vip_grant_does_not_open_the_perk(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A lapsed grant is not a grant — ``days=-1`` puts ``vip_till`` behind us."""
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _enable_rp18(registry)
    await _grant_global_vip(registry, days=-1)

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".цветы", user_id=10, reply_to_user_id=20),
    )
    assert len(sent) == 1
    assert "только для пар" in sent[0]["text"].lower()


async def test_vip_perk_does_not_grant_xp_to_a_real_pair_path(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A VIP who DOES have a relationship keeps the ordinary XP render.

    The #270 branch sits after the relationship branch, so a pair must
    never fall into it — the flavoured "+N XP" line is the proof it did
    not.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _enable_rp18(registry)
    await _grant_global_vip(registry)
    # Level 7 ("Отношения") starts at 150 000 XP — flowers' own min_level.
    await _add_relationship(registry, exp=150_000)

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".цветы", user_id=10, reply_to_user_id=20),
    )
    assert len(sent) == 1
    assert "+" in sent[0]["text"]


# ---------------------------------------------------------------------------
# No reply
# ---------------------------------------------------------------------------


async def test_no_reply_sends_usage_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])

    sent = capture_outgoing(bot)
    await dp.feed_update(bot, _group_msg(".обнять", user_id=10))
    assert len(sent) == 1
    assert "ответь" in sent[0]["text"].lower()


async def test_self_target_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".обнять", user_id=10, reply_to_user_id=10, reply_to_first_name="Alice"),
    )
    assert len(sent) == 1
    assert "партнёр" in sent[0]["text"].lower()


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


async def test_rate_limit_blocks_21st_action(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    import telegram_invite_bot.handlers.rp as rp_module

    # Freeze the clock so all 21 attempts land in the same 60s window.
    frozen = time.monotonic()
    monkeypatch.setattr(
        rp_module,
        "_limiter",
        rp_module.RpRateLimiter(time_fn=lambda: frozen),
    )

    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    # 20 allowed universal actions (stranger → _general).
    for i in range(20):
        await dp.feed_update(
            bot,
            _group_msg(".обнять", user_id=10, reply_to_user_id=20, update_id=i + 1),
        )
    assert len(sent) == 20
    assert all("слишком много" not in m["text"].lower() for m in sent)

    # 21st is blocked.
    await dp.feed_update(
        bot,
        _group_msg(".обнять", user_id=10, reply_to_user_id=20, update_id=100),
    )
    assert len(sent) == 21
    assert "слишком много" in sent[20]["text"].lower()


# ---------------------------------------------------------------------------
# /rp_commands
# ---------------------------------------------------------------------------


async def test_rp_commands_private_lists_levels(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        make_message_update("/rp_commands", chat_type="private", user_id=10),
    )
    assert len(sent) == 1
    body = sent[0]["text"]
    # Private header + the open-to-all block + per-level lines. The block
    # header was reworded from «Универсальные» when its contents were
    # narrowed to genuinely no-relationship actions.
    assert "рп-команды" in body.lower()
    assert "доступны всем" in body.lower()
    assert "ур. 1" in body.lower()
    assert "ур. 10" in body.lower()


async def test_rp_commands_group_shows_max_level(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _add_relationship(registry, exp=30000)  # level 5

    sent = capture_outgoing(bot)
    await dp.feed_update(bot, _group_msg("/rp_commands", user_id=10))
    assert len(sent) == 1
    assert "макс. уровень" in sent[0]["text"].lower()


# ---------------------------------------------------------------------------
# No-chatter-theft
# ---------------------------------------------------------------------------


async def test_plain_chatter_falls_through(
    make_wired: WiredFactory,
    assert_no_outgoing: Callable[[Bot, str], None],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    assert_no_outgoing(bot, "plain group chatter must fall through")
    # "обнять" with no prefix is not an RP action → filter rejects it.
    result = await dp.feed_update(
        bot,
        _group_msg("обнять тебя сильно", user_id=10, reply_to_user_id=20),
    )
    assert result is UNHANDLED


# ---------------------------------------------------------------------------
# 18+ gate (AUD-4)
# ---------------------------------------------------------------------------

_CHAT = -100


def _attach_rp18_api(
    bot: Any,
    monkeypatch: Any,
    sink: list[dict[str, Any]],
    *,
    admin_ids: set[int],
) -> None:
    """Mock the Telegram surface the 18+ flow touches: get_chat_member
    (admin classification), send/edit message, answer callback, get_me."""

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "GetChatMember":
            from aiogram.types import ChatMemberAdministrator, ChatMemberMember
            from aiogram.types import User as TGUser

            uid = method.user_id
            user = TGUser(id=uid, is_bot=False, first_name="U")
            if uid in admin_ids:
                return ChatMemberAdministrator(
                    user=user,
                    can_be_edited=False,
                    is_anonymous=False,
                    can_manage_chat=True,
                    can_delete_messages=True,
                    can_manage_video_chats=True,
                    can_restrict_members=True,
                    can_promote_members=False,
                    can_change_info=True,
                    can_invite_users=True,
                    can_post_stories=False,
                    can_edit_stories=False,
                    can_delete_stories=False,
                )
            return ChatMemberMember(user=user)
        if name in ("SendMessage", "EditMessageText"):
            sink.append({"method": name, "chat_id": method.chat_id, "text": method.text})
            from aiogram.types import Chat, Message
            from aiogram.types import User as TGUser

            return Message(
                message_id=10,
                date=1_700_000_000,
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TGUser(id=777, is_bot=True, first_name="Bot"),
                text=method.text,
            )
        if name == "AnswerCallbackQuery":
            sink.append({"method": name, "text": method.text})
            return True
        if name == "GetMe":
            from aiogram.types import User as TGUser

            return TGUser(id=777, is_bot=True, first_name="Bot", username="bot")
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


def _rp18_callback(user_id: int) -> Any:
    """Build a group-chat callback update for the rp18 enable button."""
    from aiogram.types import Update

    return Update.model_validate(
        {
            "update_id": 5,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": user_id, "is_bot": False, "first_name": "A"},
                "chat_instance": "ci-1",
                "data": "rp18_enable",
                "message": {
                    "message_id": 10,
                    "date": 1_700_000_000,
                    "chat": {"id": _CHAT, "type": "supergroup", "title": "T"},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "prompt",
                },
            },
        }
    )


async def _read_rp18_enabled(registry: Any, *, chat_id: int = _CHAT) -> bool:
    from sqlalchemy import select

    sm = registry.session(DBName.USERS)
    async with sm() as session:
        val = (
            await session.execute(
                select(GroupSettings.rp_18_enabled).where(GroupSettings.group_id == chat_id)
            )
        ).scalar_one_or_none()
    return bool(val)


async def test_rp18_action_default_group_refused_with_prompt_for_admin(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_rp18_api(bot, monkeypatch, sink, admin_ids={10})

    # kiss (level 5) is 18+. Default group → refused; admin → one-time prompt.
    await dp.feed_update(bot, _group_msg(".поцеловать", user_id=10, reply_to_user_id=20))
    sends = [m for m in sink if m["method"] == "SendMessage"]
    assert len(sends) == 1
    body = sends[0]["text"].lower()
    # The prompt (not the plain rp18_disabled refusal) — carries the enable
    # affordance and the HTML-bold-converted prompt text.
    assert "<b>" in sends[0]["text"]
    assert "18+" in body
    # prompt_sent persisted so it shows at most once.
    sm = registry.session(DBName.USERS)
    from sqlalchemy import select

    async with sm() as session:
        ps = (
            await session.execute(
                select(GroupSettings.rp_18_prompt_sent).where(GroupSettings.group_id == _CHAT)
            )
        ).scalar_one()
        assert bool(ps) is True
    # Not yet enabled.
    assert await _read_rp18_enabled(registry) is False


async def test_rp18_non_admin_first_hit_plain_refusal(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_rp18_api(bot, monkeypatch, sink, admin_ids=set())

    await dp.feed_update(bot, _group_msg(".поцеловать", user_id=11, reply_to_user_id=20))
    sends = [m for m in sink if m["method"] == "SendMessage"]
    assert len(sends) == 1
    # Non-admin → plain rp18_disabled (no enable button, no <b> prompt).
    assert "отключены" in sends[0]["text"].lower()
    # Prompt NOT consumed for a non-admin → an admin can still be prompted.
    assert await _read_rp18_enabled(registry) is False


async def test_rp18_admin_enable_then_action_allowed(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    # Married pair so an enabled 18+ "sex" (level 6) action grants XP.
    await _add_marriage(registry, exp=0)
    sink: list[dict[str, Any]] = []
    _attach_rp18_api(bot, monkeypatch, sink, admin_ids={10})

    # Admin clicks the enable button.
    await dp.feed_update(bot, _rp18_callback(user_id=10))
    assert await _read_rp18_enabled(registry) is True

    # Subsequent 18+ action by a married pair is now allowed (kiss = level 5,
    # marriage path grants the action XP regardless of relationship level).
    sink.clear()
    await dp.feed_update(bot, _group_msg(".поцеловать", user_id=10, reply_to_user_id=20))
    sends = [m for m in sink if m["method"] == "SendMessage"]
    assert len(sends) == 1
    assert "отключены" not in sends[0]["text"].lower()
    assert "поцелова" in sends[0]["text"].lower()


async def test_rp18_non_admin_cannot_enable(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_rp18_api(bot, monkeypatch, sink, admin_ids=set())

    await dp.feed_update(bot, _rp18_callback(user_id=11))
    # Flag NOT flipped; the callback answered with a no-access alert.
    assert await _read_rp18_enabled(registry) is False
    answers = [m for m in sink if m["method"] == "AnswerCallbackQuery"]
    assert answers and "доступ" in (answers[0]["text"] or "").lower()


async def test_rp18_non_18_action_unaffected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    # hug (level 2) is NOT 18+ → no gate, renders the general variant.
    await dp.feed_update(bot, _group_msg(".обнять", user_id=10, reply_to_user_id=20))
    assert len(sent) == 1
    assert "обнял" in sent[0]["text"].lower()
    assert "отключены" not in sent[0]["text"].lower()


# ---------------------------------------------------------------------------
# #271 — the sex verbs' bare ``@username`` target must be IN this chat
# ---------------------------------------------------------------------------


def _mention_msg(
    handle: str,
    *,
    verb: str = ".выебать",
    user_id: int = 10,
    chat_id: int = _CHAT,
) -> Any:
    """Group message ``<verb> @handle`` carrying a real ``mention`` entity.

    ``_first_username_mention`` reads Telegram's entity list, not the raw
    text, so the entity offsets have to be right — hence a hand-built
    update rather than :func:`_group_msg`.
    """
    from aiogram.types import Update

    text = f"{verb} @{handle}"
    return Update.model_validate(
        {
            "update_id": 7,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": chat_id, "type": "supergroup", "title": "T"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Alice"},
                "text": text,
                "entities": [
                    {
                        "type": "mention",
                        "offset": len(verb) + 1,
                        "length": len(handle) + 1,
                    }
                ],
            },
        }
    )


async def _seed_username(registry: Any, *, user_id: int = 20, username: str = "outsider") -> None:
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(User(user_id=user_id, username=username, first_name="Mallory"))
        await session.commit()


def _attach_member_api(
    bot: Any,
    monkeypatch: Any,
    sink: list[dict[str, Any]],
    *,
    status: str,
) -> None:
    """Telegram mock whose ``get_chat_member`` answers with ``status``.

    ``status`` is one of ``"member"``, ``"left"``, ``"kicked"`` or
    ``"error"`` (raises, i.e. the "Telegram has never heard of this user
    in this chat" case).
    """

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "GetChatMember":
            from aiogram.exceptions import TelegramBadRequest
            from aiogram.types import (
                ChatMemberBanned,
                ChatMemberLeft,
                ChatMemberMember,
            )
            from aiogram.types import User as TGUser

            if status == "error":
                raise TelegramBadRequest(method=method, message="user not found")
            user = TGUser(id=method.user_id, is_bot=False, first_name="Mallory")
            if status == "left":
                return ChatMemberLeft(user=user)
            if status == "kicked":
                return ChatMemberBanned(user=user, until_date=datetime(2030, 1, 1))
            return ChatMemberMember(user=user)
        if name == "SendMessage":
            sink.append({"method": name, "text": method.text})
            from aiogram.types import Chat, Message
            from aiogram.types import User as TGUser

            return Message(
                message_id=10,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TGUser(id=777, is_bot=True, first_name="Bot"),
                text=method.text,
            )
        if name == "GetMe":
            from aiogram.types import User as TGUser

            return TGUser(id=777, is_bot=True, first_name="Bot", username="bot")
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def test_sex_username_target_outside_the_chat_is_refused(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """#271: ``.выебать @outsider`` must not name a non-member publicly.

    ``UsersRepo.get_by_username`` is a flat lookup over the WHOLE
    users.db with no ``chat_id`` in the WHERE clause, so it resolves any
    handle the bot has ever seen — in any group, or in a private chat.
    Before this fix that resolved handle became the target outright and
    the group saw ``rel_rp_done_sex_general`` («принудил(а) к интиму»)
    naming someone who is not in the room. Legacy confirmed membership
    with ``get_chat_member`` and failed closed (bot.py:21800-21805).
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _enable_rp18(registry)
    await _seed_username(registry)
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, status="error")

    await dp.feed_update(bot, _mention_msg("outsider"))

    sends = [m for m in sink if m["method"] == "SendMessage"]
    assert len(sends) == 1
    # Unresolved target → the usage hint, NOT the 18+ render.
    assert "Ответь на сообщение" in sends[0]["text"]
    assert "интиму" not in sends[0]["text"]
    assert "Mallory" not in sends[0]["text"]


async def test_sex_username_target_who_left_is_refused(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """#271: a ``left`` member is "not in this chat" for this purpose.

    Deliberate divergence from legacy, which accepted any
    ``get_chat_member`` result carrying a ``user`` and so would have
    rendered the line for someone who had already walked out. See
    ``_member_of_chat``'s docstring.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _enable_rp18(registry)
    await _seed_username(registry)
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, status="left")

    await dp.feed_update(bot, _mention_msg("outsider"))

    sends = [m for m in sink if m["method"] == "SendMessage"]
    assert len(sends) == 1
    assert "Ответь на сообщение" in sends[0]["text"]
    assert "интиму" not in sends[0]["text"]


async def test_sex_username_target_inside_the_chat_still_works(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """#271 must not break the feature it guards: a real member renders."""
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _enable_rp18(registry)
    await _seed_username(registry)
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, status="member")

    await dp.feed_update(bot, _mention_msg("outsider"))

    sends = [m for m in sink if m["method"] == "SendMessage"]
    assert len(sends) == 1
    assert "интиму" in sends[0]["text"]
    assert "Mallory" in sends[0]["text"]


# ---------------------------------------------------------------------------
# #231 — the RP action lands in the pair's joint-activity log
# ---------------------------------------------------------------------------


async def test_married_pair_hug_writes_the_joint_activity_log(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Legacy filed every RP action next to the paid activities (#231).

    ``bot.py:21825`` passed ``activity_key``/``paid_by_user_id`` into
    ``marriage_add_xp``, which INSERTed into ``marriage_activity_log``
    (``bot.py:21656``). The port granted the XP and wrote no row, so a
    couple's history listed only the six purchasable activities while
    the 26 RP verbs — by far the commoner half — left no trace.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _add_marriage(registry, exp=0)

    capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".обнять", user_id=10, reply_to_user_id=20),
    )

    sm = registry.session(DBName.USERS)
    async with sm() as session:
        from sqlalchemy import select

        result = await session.execute(select(MarriageActivityLog))
        rows = result.scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.chat_id == -100
    # Canonical (min, max) pair — the repo normalises, so the row matches
    # regardless of which spouse acted.
    assert (row.user1_id, row.user2_id) == (10, 20)
    assert row.activity_key == "rp_hug"
    assert row.xp_gained == 10
    assert row.paid_by_user_id == 10


async def test_marriage_that_vanishes_mid_action_writes_no_log_row(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is read, then updated — a /divorce can land in between.

    ``add_marriage_xp`` re-checks ``status`` inside its own UPDATE, so it
    answers ``None`` for a marriage that ended after ``get_marriage``
    returned it. The port used to ignore that answer, which filed a
    joint-activity row for a bond that no longer existed and replied
    with a ``+10`` the couple never received — the history then read as
    if the marriage had been alive for an action it never saw.

    Now the ``None`` falls through to the no-pair tail, which is where
    the same request would have gone had the divorce landed one
    statement earlier: a universal verb still renders, at 0 XP.
    """
    import telegram_invite_bot.handlers.rp as rp_module

    # The module limiter outlives the process, so an action spent here
    # would come out of a later test's budget. Same fresh-limiter swap
    # the #598 tests use, but with the real clock: nothing here cares
    # what time it is, only that the window starts empty.
    monkeypatch.setattr(rp_module, "_limiter", rp_module.RpRateLimiter(time_fn=time.monotonic))

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _add_marriage(registry, exp=0)

    async def _vanished(self: Any, chat_id: int, user_id: int, xp: int) -> int | None:
        return None

    monkeypatch.setattr(BondsWriteRepo, "add_marriage_xp", _vanished)

    sent = capture_outgoing(bot)
    result = await dp.feed_update(
        bot,
        _group_msg(".обнять", user_id=10, reply_to_user_id=20),
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    # The universal ``_general`` render — the action happened, the pair
    # progress did not.
    assert "обнял" in body.lower()
    assert "+10" not in body

    sm = registry.session(DBName.USERS)
    async with sm() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(MarriageActivityLog))).scalars().all()
        marriage = (
            await session.execute(select(Marriage).where(Marriage.chat_id == -100))
        ).scalar_one()
    assert rows == []
    assert marriage.experience == 0


async def test_relationship_hug_writes_the_joint_activity_log(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Relationship half of #231 — legacy ``bot.py:21837`` → ``:22393``."""
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    # hug needs level 2; RELATIONSHIP_LEVEL_XP puts that at 1500 XP.
    await _add_relationship(registry, exp=1500)

    capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".обнять", user_id=10, reply_to_user_id=20),
    )

    sm = registry.session(DBName.USERS)
    async with sm() as session:
        from sqlalchemy import select

        result = await session.execute(select(RelationshipActivityLog))
        rows = result.scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.chat_id == -100
    assert (row.user1_id, row.user2_id) == (10, 20)
    assert row.activity_key == "rp_hug"
    assert row.xp_gained == 10
    assert row.paid_by_user_id == 10


# ---------------------------------------------------------------------------
# #598 / #600 — gate ordering, and a reply to the bot ends the update
# ---------------------------------------------------------------------------


def _attach_recording_api(
    bot: Any,
    monkeypatch: Any,
    sink: list[dict[str, Any]],
) -> None:
    """Telegram mock that records EVERY call, ``get_chat_member`` included.

    The helpers above log only the sends. These tests are about an
    outbound call that must NOT happen, so the mock has to log it too.
    """

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        sink.append({"method": name, "text": getattr(method, "text", None)})
        if name == "GetChatMember":
            from aiogram.types import ChatMemberMember
            from aiogram.types import User as TGUser

            return ChatMemberMember(user=TGUser(id=method.user_id, is_bot=False, first_name="M"))
        if name == "SendMessage":
            from aiogram.types import Chat, Message
            from aiogram.types import User as TGUser

            return Message(
                message_id=10,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TGUser(id=777, is_bot=True, first_name="Bot"),
                text=method.text,
            )
        if name == "GetMe":
            from aiogram.types import User as TGUser

            return TGUser(id=777, is_bot=True, first_name="Bot", username="bot")
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def test_rate_limit_covers_the_username_lookup(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """#598: the limiter must sit in FRONT of the outbound get_chat_member.

    ``.выебать @handle`` resolves its target with a live
    ``get_chat_member`` (``_member_of_chat``). While the limiter ran
    *after* that resolution, every inbound message spent one API call
    before being refused — so a single user could drive that call at
    Telegram's own inbound message rate and earn a global 429 for the
    whole bot, not just for their own chat. Legacy gated first
    (bot.py:21786-21788, then :21801).
    """
    import telegram_invite_bot.handlers.rp as rp_module

    frozen = time.monotonic()
    monkeypatch.setattr(rp_module, "_limiter", rp_module.RpRateLimiter(time_fn=lambda: frozen))

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _enable_rp18(registry)
    await _seed_username(registry)
    sink: list[dict[str, Any]] = []
    _attach_recording_api(bot, monkeypatch, sink)

    # Burn the 20-slot window on a verb that needs no API call at all.
    for i in range(20):
        await dp.feed_update(
            bot, _group_msg(".обнять", user_id=10, reply_to_user_id=20, update_id=i + 1)
        )
    assert [m["method"] for m in sink].count("GetChatMember") == 0

    await dp.feed_update(bot, _mention_msg("outsider"))

    assert [m["method"] for m in sink].count("GetChatMember") == 0
    sends = [m for m in sink if m["method"] == "SendMessage"]
    assert len(sends) == 21
    assert "слишком много" in sends[20]["text"].lower()


async def test_rp18_prompt_survives_an_exhausted_rate_limit(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """#598, second half: the 18+ gate runs BEFORE the limiter.

    Legacy refused-or-prompted at bot.py:21768-21783 and only then
    consulted the limiter at :21786. With the order swapped, an admin
    whose window was already spent got ``rel_rp_rate_limit`` instead of
    the one-time enable prompt — and since the prompt is shown at most
    once per group, that affordance is easy to lose in a busy chat.
    """
    import telegram_invite_bot.handlers.rp as rp_module

    frozen = time.monotonic()
    monkeypatch.setattr(rp_module, "_limiter", rp_module.RpRateLimiter(time_fn=lambda: frozen))

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_rp18_api(bot, monkeypatch, sink, admin_ids={10})

    for i in range(20):
        await dp.feed_update(
            bot, _group_msg(".обнять", user_id=10, reply_to_user_id=20, update_id=i + 1)
        )
    assert len([m for m in sink if m["method"] == "SendMessage"]) == 20

    await dp.feed_update(
        bot, _group_msg(".поцеловать", user_id=10, reply_to_user_id=20, update_id=99)
    )

    sends = [m for m in sink if m["method"] == "SendMessage"]
    assert len(sends) == 21
    assert "<b>" in sends[20]["text"]
    assert "18+" in sends[20]["text"].lower()
    assert await _read_rp18_enabled(registry) is False


async def test_reply_to_a_bot_is_silent(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#600: legacy answered a reply-to-a-bot with NOTHING.

    ``bot.py:21815-21816`` returns False and sends no message. The port
    had turned that case into ``rel_rp_reply``, so a group that answers
    the bot's own posts with RP verbs collected one refusal per post.
    """
    bot, dp, _ = await make_wired(schemas=[UsersBase])

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg(".обнять", user_id=10, reply_to_user_id=777, reply_to_is_bot=True),
    )
    assert sent == []


async def test_reply_to_a_bot_does_not_fall_through_to_the_username_branch(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """#600: a bot reply ends the update exactly where legacy ended it.

    Legacy set ``target`` from the reply unconditionally
    (bot.py:21791-21792), which made the ``@username`` branch at :21794
    unreachable once a reply existed; the bot check at :21815 then
    returned silently. The port skipped the bot when *setting* the
    target, which re-opened that branch — so ``.выебать @someone`` sent
    as a reply to one of the bot's own posts resolved the handle over the
    network and published the 18+ line naming a third party.
    """
    from aiogram.types import Update

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    await _enable_rp18(registry)
    await _seed_username(registry)
    sink: list[dict[str, Any]] = []
    _attach_recording_api(bot, monkeypatch, sink)

    verb = ".выебать"
    await dp.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 8,
                "message": {
                    "message_id": 2,
                    "date": 1_700_000_000,
                    "chat": {"id": _CHAT, "type": "supergroup", "title": "T"},
                    "from": {"id": 10, "is_bot": False, "first_name": "Alice"},
                    "text": f"{verb} @outsider",
                    "entities": [{"type": "mention", "offset": len(verb) + 1, "length": 9}],
                    "reply_to_message": {
                        "message_id": 1,
                        "date": 1_700_000_000,
                        "chat": {"id": _CHAT, "type": "supergroup", "title": "T"},
                        "from": {"id": 777, "is_bot": True, "first_name": "Bot"},
                        "text": "hi",
                    },
                },
            }
        ),
    )

    assert [m["method"] for m in sink].count("GetChatMember") == 0
    assert [m for m in sink if m["method"] == "SendMessage"] == []
