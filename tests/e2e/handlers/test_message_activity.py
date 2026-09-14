"""E2E: per-message group side effects via the outer middleware (A-03).

The legacy catch-all counted every group message and (subject to an
anti-spam gate) minted one coin for the author. The new pipeline lost
both when the legacy bridge was deleted; ``MessageActivityMiddleware``
(an OUTER middleware on the root router) restores them so plain chatter
that no handler claims still ticks the counter and earns.

These drive a real group text :class:`Update` through the full wired
dispatcher (so the root-router middleware actually runs) and assert on
the ``message_stats.message_counts`` row, the ``economy.users`` balance
and the ``users.user_group_joins`` membership row — plus the exclusions
(private chat, bots, ``sender_chat``, commands) that must NOT count,
earn or record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from aiogram import Router
from aiogram.types import Update
from sqlalchemy import select, update

from telegram_invite_bot.db.models.base import EconomyBase, MessageStatsBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.message_stats import MessageCount
from telegram_invite_bot.db.models.users import UserGroupJoin
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.middlewares.message_activity import MessageActivityMiddleware
from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.vip_bonus import clear_cache as clear_vip_cache
from telegram_invite_bot.utils.economy import _MAX_AMOUNT

from .conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

# get_or_create seeds a wallet at the legacy welcome balance of 100;
# one passive-earning credit lands it at 101.
_WELCOME_BALANCE = 100
_GROUP_TEXT = "Hello there friends, how is everyone doing today?"

# ``make_message_update``'s fixed ``date``, and the naive-UTC instant
# it must land in the table as.
_MESSAGE_DATE = 1_700_000_000
_MESSAGE_DT = datetime.fromtimestamp(_MESSAGE_DATE, tz=UTC).replace(tzinfo=None)


async def _count_rows(registry: EngineRegistry, user_id: int) -> list[MessageCount]:
    sessionmaker = registry.session(DBName.MESSAGE_STATS)
    async with sessionmaker() as session:
        result = await session.execute(select(MessageCount).where(MessageCount.user_id == user_id))
        return list(result.scalars().all())


async def _balance(registry: EngineRegistry, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        result = await session.execute(
            select(EconomyUser.balance).where(EconomyUser.user_id == user_id)
        )
        value = result.scalar_one_or_none()
        return None if value is None else int(value)


async def _ledger_rows(registry: EngineRegistry, user_id: int) -> list[Transaction]:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        result = await session.execute(select(Transaction).where(Transaction.to_id == user_id))
        return list(result.scalars().all())


async def _join_rows(registry: EngineRegistry, user_id: int) -> list[UserGroupJoin]:
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        result = await session.execute(
            select(UserGroupJoin).where(UserGroupJoin.user_id == user_id)
        )
        return list(result.scalars().all())


def _find_activity_middleware(router: Router) -> MessageActivityMiddleware:
    """The live middleware instance, wherever in the router tree it sits.

    ``main_router`` attaches it to the root *router*, not the dispatcher,
    so a flat scan of ``dispatcher.message.outer_middleware`` misses it.
    """
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, MessageActivityMiddleware):
            return middleware
    for sub in router.sub_routers:
        try:
            return _find_activity_middleware(sub)
        except LookupError:
            continue
    raise LookupError("MessageActivityMiddleware is not registered")


@pytest.fixture
def _wired_schemas() -> list[type]:
    return [UsersBase, EconomyBase, MessageStatsBase]


async def test_group_message_counts_and_earns(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7001),
    )

    rows = await _count_rows(registry, 7001)
    assert len(rows) == 1
    assert rows[0].chat_id == -100123
    assert rows[0].count == 1

    # Passive earning: wallet seeded (100) + one coin.
    assert await _balance(registry, 7001) == _WELCOME_BALANCE + 1

    # #225: the mint is booked. Legacy wrote this row through
    # ``add_coins`` (bot.py:43842); without it a week of chatting read
    # as "received: 0" in /balance's cashflow.
    ledger = await _ledger_rows(registry, 7001)
    assert len(ledger) == 1
    assert ledger[0].from_id is None
    assert ledger[0].amount == 1
    assert ledger[0].type == "message_reward"


async def test_second_immediate_message_counts_but_does_not_earn(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    first = make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7002)
    second = make_message_update(
        "A completely different long sentence to dodge the dup gate.",
        chat_type="supergroup",
        chat_id=-100123,
        user_id=7002,
        update_id=2,
        message_id=2,
    )
    await dispatcher.feed_update(bot, first)
    await dispatcher.feed_update(bot, second)

    rows = await _count_rows(registry, 7002)
    assert len(rows) == 1
    # Both messages counted on the same day → count == 2.
    assert rows[0].count == 2
    # But the cooldown blocks the second reward → only one coin.
    assert await _balance(registry, 7002) == _WELCOME_BALANCE + 1


async def test_daily_cap_stops_earning_but_not_counting(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """T-019: an exhausted daily allowance blocks the credit only.

    Proves the config → middleware → wallet wiring end to end. Feeding
    150 real messages is impossible under the 20 s cooldown, so the
    allowance is spent directly on the live tracker instead — the same
    state a day of chatting would leave behind.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    activity = _find_activity_middleware(dispatcher)
    today = datetime.now(activity._tz).date().isoformat()  # noqa: SLF001
    cap = activity._economy.message_reward_daily_cap  # noqa: SLF001
    assert cap > 0, "the shipped default must have a ceiling"
    assert activity._tracker.take(7010, today, cap) == cap  # noqa: SLF001

    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7010),
    )

    # Stats are unconditional — the message still counts for /top.
    rows = await _count_rows(registry, 7010)
    assert len(rows) == 1
    assert rows[0].count == 1
    # Earning is not: no credit, and no wallet conjured to hold one.
    assert await _balance(registry, 7010) is None


async def _seed_wallet_at_the_cap(registry: EngineRegistry, user_id: int) -> None:
    """A wallet with no room left, so ``credit`` refuses the reward."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=_MAX_AMOUNT))
        await session.commit()


async def test_a_refused_credit_hands_the_daily_allowance_back(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """#757: ``take`` books the allowance before the credit is attempted.

    The cap bounds how many coins a day may mint, and a refused credit
    mints none — so a refusal must leave it untouched. Left spent, a
    user sitting at the balance ceiling burned the whole day's cap on
    refusals and stayed unable to earn even after spending back down.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await _seed_wallet_at_the_cap(registry, 7110)
    activity = _find_activity_middleware(dispatcher)

    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7110),
    )

    # Nothing minted and nothing booked: the wallet had no room.
    assert await _balance(registry, 7110) == _MAX_AMOUNT
    assert await _ledger_rows(registry, 7110) == []
    # The message still counts for /top — only the credit was refused.
    rows = await _count_rows(registry, 7110)
    assert len(rows) == 1
    assert rows[0].count == 1
    # And the day's allowance is intact, not spent on the refusal.
    assert activity._tracker._earned.get(7110, 0) == 0  # noqa: SLF001


async def test_a_failing_ledger_write_hands_the_daily_allowance_back(
    make_wired: WiredFactory,
    _wired_schemas: list[type],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1033: the refund covers every exit that mints nothing.

    ``take`` books the allowance before any of the three writes below it
    is attempted, and only the balance-cap refusal ever handed it back.
    A raising ledger insert — a locked table, a dropped connection, a
    failing commit — left the booking standing, so the user forfeited
    that slice of the day for a failure that was never theirs. Same
    complaint as #757, reached by a different route.

    The ledger row is the write chosen here because it is the one that
    can fail on its own while the credit has already succeeded in the
    same transaction: it proves the refund is not merely the old
    ``credited is None`` branch under a new name.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    activity = _find_activity_middleware(dispatcher)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(TransactionsRepo, "record", _boom)

    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7120),
    )

    # The credit and the ledger row share one transaction, so the session
    # rolled back and neither landed — not even the wallet ``get_or_create``
    # opened the pass with.
    assert await _balance(registry, 7120) is None
    assert await _ledger_rows(registry, 7120) == []
    # Stats are written in their own session, before the reward, so the
    # message still counts for /top. Nothing about the failure is hidden:
    # ``__call__`` logs it and propagation continues.
    rows = await _count_rows(registry, 7120)
    assert len(rows) == 1
    # And the allowance is intact, which is the whole point.
    assert activity._tracker._earned.get(7120, 0) == 0  # noqa: SLF001


async def test_a_failing_stats_write_does_not_cost_the_author_the_coin(
    make_wired: WiredFactory,
    _wired_schemas: list[type],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1991: the protection the file already promises, given to step 1.

    ``_record`` runs three side effects in order — stats, membership,
    reward — and step 2 carries its own ``try``/``except`` with the
    reason spelled out beside it: a bookkeeping failure "must not cost
    the author the coin step 3 is about to credit". Step 1 is the same
    kind of bookkeeping, sits ahead of both, and had no such guard, so
    a locked ``message_stats.db`` unwound all of ``_record`` and the
    author silently lost the coin — for a failure in a table that has
    nothing to do with money.

    Nothing here is a money invariant in the risky direction: the
    reward is a *mint*, and its own ceiling is re-derived from the
    economy ledger (#1789), never from the counter this test breaks.
    So continuing past a dead counter cannot over-pay.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("message_stats unavailable")

    monkeypatch.setattr(MessageStatsRepo, "increment", _boom)

    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7130),
    )

    # The counter is genuinely gone — this is not a test that quietly
    # stopped exercising the failure.
    assert await _count_rows(registry, 7130) == []
    # The two effects behind it still ran.
    assert await _balance(registry, 7130) == _WELCOME_BALANCE + 1
    assert len(await _ledger_rows(registry, 7130)) == 1
    assert len(await _join_rows(registry, 7130)) == 1


async def test_private_message_is_ignored(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await dispatcher.feed_update(
        bot, make_message_update(_GROUP_TEXT, chat_type="private", user_id=7003)
    )
    assert await _count_rows(registry, 7003) == []
    assert await _balance(registry, 7003) is None


async def test_command_message_neither_counts_nor_earns(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await dispatcher.feed_update(
        bot,
        make_message_update("/balance", chat_type="supergroup", chat_id=-100123, user_id=7004),
    )
    assert await _count_rows(registry, 7004) == []
    # #1862: the ``/balance`` handler seeds its own wallet at the welcome
    # balance and now commits that seed before it replies, so a row here
    # is expected and correct. What must be absent is the middleware's
    # passive coin — the balance stays exactly at the seed and no
    # ``message_reward`` is booked. (Before #1862 this read returned
    # ``None`` for the wrong reason: the reply failed on the fake token
    # and the rollback took the seed with it.)
    assert await _balance(registry, 7004) == _WELCOME_BALANCE
    assert await _ledger_rows(registry, 7004) == []


async def test_group_message_records_membership(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """#277: this is the path that actually fills ``user_group_joins``.

    Legacy wrote the row from its catch-all (bot.py:43833) and that
    source accounts for 17 of the 24 rows in production; the
    ``new_chat_members`` writer the port kept has produced none in
    either bot. Without a row here ``/profile`` prints "—" for "messages
    since joining" to every member first seen after the cutover.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7020),
    )

    rows = await _join_rows(registry, 7020)
    assert len(rows) == 1
    row = rows[0]
    assert row.chat_id == -100123
    assert row.source == "observed_message"
    assert row.group_title == "T"
    assert row.is_active == 1
    assert row.left_at is None
    # The message's own timestamp, in the naive-UTC frame the read side
    # assumes (the join-date block in ``profile._group_caption``) — not
    # "now".
    assert row.joined_at == _MESSAGE_DT


async def test_later_message_refreshes_liveness_but_not_the_join_date(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """First sighting wins: re-observing a member must not move the date.

    ``joined_at`` anchors the since-join counter, so pushing it forward
    on every message would shrink that counter to zero. ``last_seen``
    moving proves the second write really ran rather than being skipped.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    later = _MESSAGE_DATE + 86_400
    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7021),
    )
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "A completely different long sentence a whole day later.",
            chat_type="supergroup",
            chat_id=-100123,
            user_id=7021,
            update_id=2,
            message_id=2,
            date=later,
        ),
    )

    rows = await _join_rows(registry, 7021)
    assert len(rows) == 1
    assert rows[0].joined_at == _MESSAGE_DT
    assert rows[0].last_seen == datetime.fromtimestamp(later, tz=UTC).replace(tzinfo=None)


async def test_private_and_command_messages_record_no_membership(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """The join write inherits the counter's exclusions, as legacy did.

    Legacy's catch-all returned on non-group chats before reaching
    :func:`ensure_user_group_joined` (bot.py:43804), and commands never
    arrived there at all — telebot dispatched them to their own handlers
    first.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await dispatcher.feed_update(
        bot, make_message_update(_GROUP_TEXT, chat_type="private", user_id=7022)
    )
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/balance",
            chat_type="supergroup",
            chat_id=-100123,
            user_id=7023,
            update_id=2,
        ),
    )

    assert await _join_rows(registry, 7022) == []
    assert await _join_rows(registry, 7023) == []


def _service_message_update(
    payload: dict[str, object],
    *,
    chat_id: int = -100123,
    user_id: int = 7030,
    update_id: int = 1,
    message_id: int = 1,
) -> Update:
    """A group service message (join / leave / pin) with no ``text``.

    Telegram delivers these as ordinary ``message`` updates carrying a
    service field instead of a body, which is why they reach the outer
    middleware at all. Legacy's catch-all never saw them: it subscribed
    to ``['text', 'photo', 'video', 'document', 'sticker']``
    (bot.py:43773) and the dedicated ``new_chat_members`` /
    ``left_chat_member`` handlers (bot.py:43914/44157) never called
    ``increment_message_count`` (its one legacy call site is
    bot.py:43850, inside the catch-all).
    """
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": _MESSAGE_DATE,
                "chat": {"id": chat_id, "type": "supergroup", "title": "T"},
                "from": {"id": user_id, "is_bot": False, "first_name": "T"},
                **payload,
            },
        }
    )


def _member_stub(user_id: int) -> dict[str, object]:
    return {"id": user_id, "is_bot": False, "first_name": "T"}


async def test_service_messages_neither_count_nor_record_membership(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """Joins, leaves and pins are not chatter — legacy never saw them.

    Both the counter and the membership stamp hang off the same
    content-type allowlist in legacy, so a service message must move
    neither *from this middleware*. The dedicated ``new_chat_members``
    handler does legitimately write a membership row of its own
    (``source="join_event"``), which is why the assertion below is
    scoped to ``observed_message`` rather than to the row count.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    payloads: list[dict[str, object]] = [
        {"new_chat_members": [_member_stub(7031)]},
        {"left_chat_member": _member_stub(7031)},
        {
            "pinned_message": {
                "message_id": 5,
                "date": _MESSAGE_DATE,
                "chat": {"id": -100123, "type": "supergroup", "title": "T"},
                "text": "pinned",
            }
        },
    ]
    for index, payload in enumerate(payloads, start=1):
        await dispatcher.feed_update(
            bot,
            _service_message_update(payload, user_id=7031, update_id=index, message_id=index),
        )

    assert await _count_rows(registry, 7031) == []
    rows = await _join_rows(registry, 7031)
    observed = [row for row in rows if row.source == "observed_message"]
    assert observed == []
    assert await _balance(registry, 7031) is None


async def test_leave_service_message_does_not_resurrect_membership(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """The hazard the allowlist exists to close.

    ``record_join`` heals ``is_active`` on every sighting so a rejoin
    restores it. If the leave announcement itself were treated as a
    sighting, the very message saying the member left would flip them
    back to active — and the leave handler's own write would be undone
    by an outer middleware that runs before it.

    Since #244 that handler exists and restamps ``left_at`` on the same
    update, so the stamp is asserted to move *forward*, never to be
    cleared: a resurrection writes ``left_at = NULL``, which fails both
    halves of the check below.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7032),
    )
    left_at = datetime.fromtimestamp(_MESSAGE_DATE, tz=UTC).replace(tzinfo=None)
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        await session.execute(
            update(UserGroupJoin)
            .where(UserGroupJoin.user_id == 7032)
            .values(is_active=0, left_at=left_at)
        )
        await session.commit()

    await dispatcher.feed_update(
        bot,
        _service_message_update(
            {"left_chat_member": _member_stub(7032)},
            user_id=7032,
            update_id=2,
            message_id=2,
        ),
    )

    rows = await _join_rows(registry, 7032)
    assert len(rows) == 1
    assert rows[0].is_active == 0
    assert rows[0].left_at is not None
    assert rows[0].left_at >= left_at


async def test_content_types_outside_the_legacy_allowlist_do_not_count(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """Voice, video notes, polls and dice were never legacy chatter.

    Legacy routed voice to its own transcription handler
    (bot.py:39337/39434) and dropped the rest entirely, so none of them
    ticked the counter or stamped membership.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    payloads: list[dict[str, object]] = [
        {"voice": {"file_id": "v1", "file_unique_id": "v1u", "duration": 3}},
        {
            "video_note": {
                "file_id": "vn1",
                "file_unique_id": "vn1u",
                "length": 240,
                "duration": 3,
            }
        },
        {"dice": {"emoji": "\U0001f3b2", "value": 4}},
    ]
    for index, payload in enumerate(payloads, start=1):
        await dispatcher.feed_update(
            bot,
            _service_message_update(payload, user_id=7033, update_id=index, message_id=index),
        )

    assert await _count_rows(registry, 7033) == []
    assert await _join_rows(registry, 7033) == []


async def test_allowlisted_media_still_counts_and_records(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """Positive control: the allowlist must not swallow real chatter.

    A photo and a sticker are both on legacy's list (bot.py:43773), so
    they keep ticking the counter and stamping membership even though
    neither carries ``text`` and so neither earns.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    photo: dict[str, object] = {
        "photo": [{"file_id": "p1", "file_unique_id": "p1u", "width": 90, "height": 90}]
    }
    sticker: dict[str, object] = {
        "sticker": {
            "file_id": "s1",
            "file_unique_id": "s1u",
            "width": 512,
            "height": 512,
            "type": "regular",
            "is_animated": False,
            "is_video": False,
        }
    }
    for index, payload in enumerate((photo, sticker), start=1):
        await dispatcher.feed_update(
            bot,
            _service_message_update(payload, user_id=7034, update_id=index, message_id=index),
        )

    counts = await _count_rows(registry, 7034)
    assert len(counts) == 1
    assert counts[0].count == 2
    assert len(await _join_rows(registry, 7034)) == 1
    # No ``text`` means no passive earning on either — the reward gate
    # reads ``message.text`` and legacy's ``should_reward_message`` did
    # the same (bot.py:43837).
    assert await _balance(registry, 7034) is None


# ---------------------------------------------------------------------------
# #490: the VIP ``message_bonus`` perk
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_vip_bonus_cache() -> None:
    """The resolver's TTL cache is process-global; a stale entry from a
    neighbouring test would decide this file's payouts."""
    clear_vip_cache()


async def _grant_global_vip(registry: EngineRegistry, user_id: int) -> None:
    """Seed an active global VIP grant, the way ``/vip`` would."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            EconomyUser(
                user_id=user_id,
                balance=_WELCOME_BALANCE,
                vip_till=datetime.now(tz=UTC).timestamp() + 86_400,
            )
        )
        await session.commit()


async def test_vip_message_bonus_is_credited(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """Legacy added the perk on the line after the boost multiplication
    (bot.py:43841). The port carried :43840 over and dropped :43841, so a
    paid, advertised perk was credited to nobody (#490)."""
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await _grant_global_vip(registry, 7101)

    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7101),
    )

    # Base reward 1 + VIP message_bonus 1.
    assert await _balance(registry, 7101) == _WELCOME_BALANCE + 2
    ledger = await _ledger_rows(registry, 7101)
    assert len(ledger) == 1
    assert ledger[0].amount == 2


async def test_non_vip_reward_is_unchanged_by_the_bonus_path(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """The resolver's ``0`` answer must leave the ordinary chatter's
    payout exactly where it was."""
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7102),
    )

    assert await _balance(registry, 7102) == _WELCOME_BALANCE + 1


async def _seed_message_rewards(registry: EngineRegistry, user_id: int, total: int) -> None:
    """Book ``total`` coins of message reward on today's ledger.

    ``date`` is naive UTC, matching the writer. "Now" always falls
    inside today's *local* day whatever the host zone, which is the
    only property these tests need.
    """
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            Transaction(
                from_id=None,
                to_id=user_id,
                amount=total,
                type="message_reward",
                reason="message reward",
                date=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await session.commit()


async def test_a_restart_does_not_hand_back_the_allowance_the_ledger_already_spent(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """#1789: the fresh process is the exploit, and the ledger closes it.

    A brand-new middleware is exactly what a deploy produces: empty
    tracker, empty cooldowns, full allowance. Production lands dozens of
    those a day, so the "per calendar day" cap used to hold for minutes
    at a time and the real ceiling was the 3/min rate cap (~4 320
    COM/day, ~29x the intended 150).

    Here the ledger says the user already took the whole cap today, and
    the fresh process honours it: the message still counts for /top, and
    no wallet is conjured to hold a coin it must not mint.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    activity = _find_activity_middleware(dispatcher)
    cap = activity._economy.message_reward_daily_cap  # noqa: SLF001
    assert cap > 0, "the shipped default must have a ceiling"
    await _seed_message_rewards(registry, 7130, cap)

    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7130),
    )

    rows = await _count_rows(registry, 7130)
    assert len(rows) == 1
    assert rows[0].count == 1
    assert await _balance(registry, 7130) is None
    # Nothing new was written: the seeded row is still the only one.
    ledger = await _ledger_rows(registry, 7130)
    assert [r.amount for r in ledger] == [cap]


async def test_a_partly_spent_ledger_allowance_pays_only_the_remainder(
    make_wired: WiredFactory, _wired_schemas: list[type]
) -> None:
    """The seed is a clamp, not an on/off switch.

    With one coin of the day left, the message earns that coin and the
    allowance closes — the same arithmetic ``take`` has always done,
    now fed a number that survived the restart.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    activity = _find_activity_middleware(dispatcher)
    cap = activity._economy.message_reward_daily_cap  # noqa: SLF001
    await _seed_message_rewards(registry, 7131, cap - 1)

    await dispatcher.feed_update(
        bot,
        make_message_update(_GROUP_TEXT, chat_type="supergroup", chat_id=-100123, user_id=7131),
    )

    assert await _balance(registry, 7131) == _WELCOME_BALANCE + 1
    ledger = await _ledger_rows(registry, 7131)
    assert sorted(r.amount for r in ledger) == [1, cap - 1]
    today = datetime.now(activity._tz).date().isoformat()  # noqa: SLF001
    assert activity._tracker.take(7131, today, 1) == 0  # noqa: SLF001


async def test_the_ledger_is_read_once_per_user_not_once_per_message(
    make_wired: WiredFactory,
    _wired_schemas: list[type],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hot-path budget of #1789, pinned.

    Passive earning runs on every group message in the bot; paying an
    extra SELECT for each of them to bound a per-day number would be a
    worse bug than the one being fixed. The read happens on the first
    qualifying message a user sends in a process and never again, so a
    second (different, cooldown-blocked) message adds no query — and a
    second *user* gets their own single read.
    """
    bot, dispatcher, registry = await make_wired(schemas=_wired_schemas, session_middleware=True)
    calls: list[int] = []
    original = TransactionsRepo.message_reward_day_total

    async def _counting(self: TransactionsRepo, user_id: int, **kwargs: object) -> int:
        calls.append(user_id)
        return await original(self, user_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(TransactionsRepo, "message_reward_day_total", _counting)

    for update_id, (uid, text) in enumerate(
        [
            (7140, _GROUP_TEXT),
            (7140, "A completely different long sentence to dodge the dup gate."),
            (7141, _GROUP_TEXT),
        ],
        start=1,
    ):
        await dispatcher.feed_update(
            bot,
            make_message_update(
                text,
                chat_type="supergroup",
                chat_id=-100123,
                user_id=uid,
                update_id=update_id,
                message_id=update_id,
            ),
        )

    assert calls == [7140, 7141]
