"""``LeftBondsCleanupSweeper.sweep_once`` end-to-end (#482).

Legacy ended the bonds of long-departed members from inside bond READ
paths (``bot.py:21586``), so the rule only fired where somebody happened
to look. The port has no such hook, which is why the rule has not
existed since the cutover and why it now lives in a background pass.

These tests pin the four things that pass has to get right, each of
which is a way to do real damage if wrong:

* the seven-day grace is honoured (dissolving early is not recoverable);
* nothing is dissolved without a live membership probe saying so, and a
  probe that fails says nothing (fail-closed, like legacy's bare
  ``except: continue``);
* someone who came back has their departure flag healed instead;
* a rate-limit answer aborts the pass rather than hammering the API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import Marriage, Relationship, UserGroupJoin
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.scheduler.left_bonds_cleanup import (
    DAYS_AFTER_LEFT_TO_END_BONDS,
    LeftBondsCleanupSweeper,
)

_CHAT = -100500
_NOW = datetime(2026, 6, 10, 12, 0, 0)
_LONG_AGO = _NOW - timedelta(days=30)


class _Member:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeBot:
    """Answers membership probes from a table; records what it was asked.

    ``statuses`` maps ``user_id`` to a ChatMemberStatus, to the string
    ``"boom"`` (raise a generic error), to ``"forbidden"`` (raise
    :class:`TelegramForbiddenError`, i.e. the chat itself is unreadable)
    or to ``"flood"`` (raise :class:`TelegramRetryAfter`). An id that is absent defaults to LEFT,
    which keeps the ordinary "yes, they are gone" case terse.
    """

    def __init__(self, statuses: dict[int, str] | None = None) -> None:
        self.statuses = statuses or {}
        self.asked: list[tuple[int, int]] = []

    async def get_chat_member(self, chat_id: int, user_id: int, **_: Any) -> _Member:
        self.asked.append((chat_id, user_id))
        answer = self.statuses.get(user_id, ChatMemberStatus.LEFT)
        if answer == "boom":
            msg = "Bad Request: user not found"
            raise RuntimeError(msg)
        if answer == "forbidden":
            raise TelegramForbiddenError(
                method="getChatMember",  # type: ignore[arg-type]
                message="Forbidden: bot was kicked from the supergroup chat",
            )
        if answer == "flood":
            raise TelegramRetryAfter(
                method="getChatMember",  # type: ignore[arg-type]
                message="Too Many Requests",
                retry_after=7,
            )
        return _Member(answer)


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(UsersBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    reg = EngineRegistry(
        engines={DBName.USERS: engine},
        sessions={DBName.USERS: sessionmaker},
    )
    try:
        yield reg
    finally:
        await engine.dispose()


async def _seed(
    registry: EngineRegistry,
    *,
    user_id: int,
    left_at: datetime | None,
    chat_id: int = _CHAT,
    marriage: bool = False,
    relationship: bool = False,
    partner: int = 999,
) -> None:
    async with session_for(registry, DBName.USERS) as s:
        s.add(
            UserGroupJoin(
                user_id=user_id,
                chat_id=chat_id,
                joined_at=datetime(2024, 1, 1),
                left_at=left_at,
                is_active=0 if left_at is not None else 1,
                source="join_event",
            )
        )
        if marriage:
            s.add(
                Marriage(
                    chat_id=chat_id,
                    user1_id=user_id,
                    user2_id=partner,
                    created_at=datetime(2024, 1, 1),
                    experience=100,
                    status="active",
                )
            )
        if relationship:
            s.add(
                Relationship(
                    chat_id=chat_id,
                    user1_id=user_id,
                    user2_id=partner,
                    created_at=datetime(2024, 1, 1),
                    experience=200,
                    status="active",
                )
            )


def _sweeper(registry: EngineRegistry, bot: _FakeBot | None, **kw: Any) -> LeftBondsCleanupSweeper:
    return LeftBondsCleanupSweeper(
        registry,
        bot=bot,  # type: ignore[arg-type]  # duck-typed fake; only get_chat_member is used
        clock=lambda: _NOW,
        **kw,
    )


async def _statuses(registry: EngineRegistry) -> tuple[str | None, str | None]:
    async with session_for(registry, DBName.USERS) as s:
        marriage = (await s.execute(text("SELECT status FROM marriages"))).scalars().first()
        relationship = (await s.execute(text("SELECT status FROM relationships"))).scalars().first()
    return marriage, relationship


async def test_a_week_gone_ends_both_bonds(registry: EngineRegistry) -> None:
    await _seed(registry, user_id=1, left_at=_LONG_AGO, marriage=True, relationship=True)
    bot = _FakeBot()

    report = await _sweeper(registry, bot).sweep_once()

    assert report.marriages_divorced == 1
    assert report.relationships_ended == 1
    assert report.members_probed == 1
    assert await _statuses(registry) == ("divorced", "ended")


async def test_the_grace_period_is_honoured(registry: EngineRegistry) -> None:
    """Six days gone is not seven. Ending a marriage early is the one
    outcome waiting cannot undo, so the boundary is pinned on the safe
    side and the probe is never even spent."""
    six_days = _NOW - timedelta(days=DAYS_AFTER_LEFT_TO_END_BONDS - 1)
    await _seed(registry, user_id=1, left_at=six_days, marriage=True, relationship=True)
    bot = _FakeBot()

    report = await _sweeper(registry, bot).sweep_once()

    assert report.members_probed == 0
    assert report.marriages_divorced == 0
    assert await _statuses(registry) == ("active", "active")


async def test_a_present_member_is_healed_not_divorced(
    registry: EngineRegistry,
) -> None:
    """The departure flag can be wrong — a leave event we saw, a rejoin
    we missed. The probe is what settles it, and the answer "still here"
    must repair the row rather than merely skip it, or every later pass
    re-spends a probe on the same person forever."""
    await _seed(registry, user_id=1, left_at=_LONG_AGO, marriage=True, relationship=True)
    bot = _FakeBot({1: ChatMemberStatus.MEMBER})

    report = await _sweeper(registry, bot).sweep_once()

    assert report.rejoins_healed == 1
    assert report.marriages_divorced == 0
    assert await _statuses(registry) == ("active", "active")

    async with session_for(registry, DBName.USERS) as s:
        row = (await s.execute(text("SELECT * FROM user_group_joins"))).mappings().one()
    assert row["is_active"] == 1
    assert row["left_at"] is None
    assert str(row["joined_at"]).startswith("2024-01-01")


async def test_a_failed_probe_dissolves_nothing(registry: EngineRegistry) -> None:
    """Fail-closed, exactly like legacy's bare ``except: continue``
    around the same call. A chat we cannot read says nothing, and a
    network hiccup must never be able to end a marriage."""
    await _seed(registry, user_id=1, left_at=_LONG_AGO, marriage=True, relationship=True)
    bot = _FakeBot({1: "boom"})

    report = await _sweeper(registry, bot).sweep_once()

    assert report.members_probed == 1
    assert report.marriages_divorced == 0
    assert report.rejoins_healed == 0
    assert await _statuses(registry) == ("active", "active")


async def test_a_rate_limit_aborts_the_pass(registry: EngineRegistry) -> None:
    """Ban safety. Folding TelegramRetryAfter into "unknown" would leave
    the loop firing the same throttled call at every remaining candidate;
    it propagates so the pass ends and the next one starts fresh."""
    for uid in (1, 2):
        await _seed(registry, user_id=uid, left_at=_LONG_AGO, marriage=True, partner=uid + 50)
    bot = _FakeBot({1: "flood", 2: "flood"})

    with pytest.raises(TelegramRetryAfter):
        await _sweeper(registry, bot).sweep_once()

    assert len(bot.asked) == 1
    assert await _statuses(registry) == ("active", None)


async def test_members_without_bonds_are_never_probed(
    registry: EngineRegistry,
) -> None:
    """The affordability filter. Departures far outnumber bonds, and a
    sweep that probed every departed member would re-ask the same
    hundreds of questions every six hours forever — and, because
    candidates arrive longest-departed first, would spend its whole
    budget on people with nothing left to end."""
    await _seed(registry, user_id=1, left_at=_LONG_AGO)
    await _seed(registry, user_id=2, left_at=_LONG_AGO, marriage=True, partner=52)
    bot = _FakeBot()

    report = await _sweeper(registry, bot).sweep_once()

    assert bot.asked == [(_CHAT, 2)]
    assert report.members_probed == 1
    assert report.marriages_divorced == 1


async def test_a_departed_partner_ends_the_bond_from_either_side(
    registry: EngineRegistry,
) -> None:
    """The bond names two people and the departure flag names one. Being
    the second-listed half of a marriage must not save it, or every pair
    recorded in the other order would survive the sweep."""
    async with session_for(registry, DBName.USERS) as s:
        s.add(
            UserGroupJoin(
                user_id=7,
                chat_id=_CHAT,
                joined_at=datetime(2024, 1, 1),
                left_at=_LONG_AGO,
                is_active=0,
                source="join_event",
            )
        )
        s.add(
            Marriage(
                chat_id=_CHAT,
                user1_id=3,
                user2_id=7,
                created_at=datetime(2024, 1, 1),
                experience=100,
                status="active",
            )
        )

    report = await _sweeper(registry, _FakeBot()).sweep_once()

    assert report.marriages_divorced == 1


async def test_the_probe_budget_caps_one_pass(registry: EngineRegistry) -> None:
    """A bot in many groups must not wake up and fire hundreds of API
    calls in a burst. The work is a week overdue by construction, so
    deferring the tail costs nothing; the report says the pass was
    truncated so the cap is visible rather than silent."""
    for uid in (1, 2, 3):
        await _seed(registry, user_id=uid, left_at=_LONG_AGO, marriage=True, partner=uid + 50)
    bot = _FakeBot()

    report = await _sweeper(registry, bot, probe_budget=2).sweep_once()

    assert report.members_probed == 2
    assert report.marriages_divorced == 2
    assert report.budget_exhausted is True


async def test_without_a_bot_the_pass_is_a_no_op(registry: EngineRegistry) -> None:
    """No bot means no membership probe, and nothing may be dissolved
    unverified. Degraded, not broken — the same posture the economy
    sweeper takes for its bot-dependent steps."""
    await _seed(registry, user_id=1, left_at=_LONG_AGO, marriage=True, relationship=True)

    report = await _sweeper(registry, None).sweep_once()

    assert report == type(report)()
    assert await _statuses(registry) == ("active", "active")


async def test_other_chats_bonds_are_untouched(registry: EngineRegistry) -> None:
    """Someone can leave one group and stay in another. Legacy scoped
    every statement by ``chat_id`` for exactly this reason; dropping the
    scope would end a member's bonds everywhere the bot serves."""
    await _seed(registry, user_id=1, left_at=_LONG_AGO, marriage=True, partner=51)
    async with session_for(registry, DBName.USERS) as s:
        s.add(
            Marriage(
                chat_id=-777,
                user1_id=1,
                user2_id=51,
                created_at=datetime(2024, 1, 1),
                experience=100,
                status="active",
            )
        )

    await _sweeper(registry, _FakeBot()).sweep_once()

    async with session_for(registry, DBName.USERS) as s:
        found = (await s.execute(text("SELECT chat_id, status FROM marriages"))).all()
    rows = {int(chat_id): str(status) for chat_id, status in found}
    assert rows[_CHAT] == "divorced"
    assert rows[-777] == "active"


async def test_a_kicked_member_counts_as_gone(registry: EngineRegistry) -> None:
    """Legacy read the same two statuses (``bot.py:21600``); a ban is a
    departure the person cannot undo by walking back in."""
    await _seed(registry, user_id=1, left_at=_LONG_AGO, relationship=True)
    bot = _FakeBot({1: ChatMemberStatus.KICKED})

    report = await _sweeper(registry, bot).sweep_once()

    assert report.relationships_ended == 1


async def test_a_second_pass_finds_nothing_left_to_do(
    registry: EngineRegistry,
) -> None:
    """Idempotence, and the reason the affordability filter is worth its
    keep: once the bonds are ended the same departure row stops being a
    candidate, so the pass costs zero probes from then on."""
    await _seed(registry, user_id=1, left_at=_LONG_AGO, marriage=True, relationship=True)
    bot = _FakeBot()
    sweeper = _sweeper(registry, bot)

    await sweeper.sweep_once()
    second = await sweeper.sweep_once()

    assert second.members_probed == 0
    assert second.marriages_divorced == 0
    assert second.relationships_ended == 0
    assert len(bot.asked) == 1


_OTHER_CHAT = _CHAT + 1  # sorts after _CHAT: list_departed_chats orders by chat_id


async def _marriage_status(registry: EngineRegistry, chat_id: int) -> str | None:
    async with session_for(registry, DBName.USERS) as s:
        row = await s.execute(
            text("SELECT status FROM marriages WHERE chat_id = :c"), {"c": chat_id}
        )
        return row.scalars().first()


async def test_a_chat_the_bot_was_thrown_out_of_costs_exactly_one_probe(
    registry: EngineRegistry,
) -> None:
    """#1927: the budget is a rate limit, not a starvation vector.

    A chat that answers ``Forbidden`` answers it for every member, and
    because an unknown verdict writes nothing, the same rows come back
    in the same order on the next pass. Before the fix, the lowest
    ``chat_id`` among departed chats could spend the whole budget on
    itself forever and no bond in any other chat was ever dissolved.
    """
    for user_id in (1, 2, 3, 4, 5):
        await _seed(registry, user_id=user_id, left_at=_LONG_AGO, marriage=True)
    await _seed(registry, user_id=10, left_at=_LONG_AGO, chat_id=_OTHER_CHAT, marriage=True)
    bot = _FakeBot(dict.fromkeys((1, 2, 3, 4, 5), "forbidden"))

    report = await _sweeper(registry, bot, probe_budget=4).sweep_once()

    assert bot.asked == [(_CHAT, 1), (_OTHER_CHAT, 10)]
    assert report.chats_abandoned == 1
    assert report.marriages_divorced == 1
    assert report.budget_exhausted is False
    assert await _marriage_status(registry, _CHAT) == "active"
    assert await _marriage_status(registry, _OTHER_CHAT) == "divorced"


async def test_probes_that_keep_answering_nothing_give_the_chat_up(
    registry: EngineRegistry,
) -> None:
    """The same bound for errors that do not name the chat.

    ``TelegramBadRequest`` covers both "chat not found" and "user not
    found", so it cannot be classified by type — the cap on unknown
    answers is what stops it from draining the budget.
    """
    for user_id in (1, 2, 3, 4, 5):
        await _seed(registry, user_id=user_id, left_at=_LONG_AGO, marriage=True)
    await _seed(registry, user_id=10, left_at=_LONG_AGO, chat_id=_OTHER_CHAT, marriage=True)
    bot = _FakeBot(dict.fromkeys((1, 2, 3, 4, 5), "boom"))

    report = await _sweeper(registry, bot, probe_budget=6).sweep_once()

    assert bot.asked == [(_CHAT, 1), (_CHAT, 2), (_CHAT, 3), (_OTHER_CHAT, 10)]
    assert report.chats_abandoned == 1
    assert report.members_probed == 4
    assert report.marriages_divorced == 1


async def test_settled_departures_cannot_crowd_out_a_bonded_one(
    registry: EngineRegistry,
) -> None:
    """#2013: the scan limit bounds work to do, not rows to look at.

    Somebody who left long ago and had nothing to dissolve keeps their
    departure row forever — the flag is cleared only for a member the
    probe finds still present — so those rows accumulate and, ordered
    ``left_at ASC``, they sort ahead of everyone. While the bond filter
    ran after the scan limit, any chat past ``_CANDIDATE_SCAN_LIMIT``
    lifetime departures filled its whole window with them, narrowed to
    nothing, and swept nothing again, ever.

    The silence is the worst part: ``chats_scanned`` is incremented only
    past the narrowing, so the pass logged nothing at all and the
    journal showed a healthy sweeper. Production reproduces at 200; here
    three settled departures and a limit of three say the same thing.
    """
    for user_id in (1, 2, 3):
        await _seed(registry, user_id=user_id, left_at=_LONG_AGO - timedelta(days=user_id))
    await _seed(registry, user_id=9, left_at=_LONG_AGO, marriage=True)
    bot = _FakeBot()

    report = await _sweeper(registry, bot, scan_limit=3).sweep_once()

    assert bot.asked == [(_CHAT, 9)], "the probe budget went to settled departures"
    assert report.chats_scanned == 1
    assert report.marriages_divorced == 1
    assert await _marriage_status(registry, _CHAT) == "divorced"


async def test_a_single_failed_probe_does_not_condemn_the_chat(
    registry: EngineRegistry,
) -> None:
    """The cap must not turn one deleted account into a dropped chat."""
    await _seed(registry, user_id=1, left_at=_LONG_AGO, marriage=True)
    await _seed(registry, user_id=2, left_at=_LONG_AGO, marriage=True, partner=998)
    bot = _FakeBot({1: "boom"})

    report = await _sweeper(registry, bot).sweep_once()

    assert bot.asked == [(_CHAT, 1), (_CHAT, 2)]
    assert report.chats_abandoned == 0
    assert report.marriages_divorced == 1
