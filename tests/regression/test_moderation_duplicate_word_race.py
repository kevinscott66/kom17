"""Two admins adding the same word at once must not get a crash card.

``WordFilterRepo.add`` and ``GroupAliasRepo.upsert`` both decide what to
do by reading first: *is this word already here?* That read is a
``SELECT``, and this project opens the SQLite transaction lazily and only
for a write-headed statement::

    db/engines.py:204-210   head[0].lower() in _NON_WRITE_HEADS  ->  return

so it runs outside any transaction. Two concurrent ``/filter_add дурак``
(or ``/alias add привет start``) therefore both read "not here", both
INSERT, and the second one arrives after the first has committed:
``unique(group_id, word)`` fires and the loser gets an
``IntegrityError``.

What made that worth fixing is not the race itself but where the
exception went. ``/groupadmin``'s copy of the word-add flow wraps its
session in ``try/except Exception``
(``handlers/groupadmin.py:1899-1913``) and answers "не удалось
сохранить". The two slash commands — ``handlers/wordfilter.py``
``handle_filter_add`` and ``handlers/group_aliases.py`` ``handle_alias``
— do not: they take the session the middleware injected, so the
exception unwound into the errors router, the whole update rolled back,
and the admin was shown an error card for a request that had in fact
succeeded. The word *was* filtered; only the confirmation was a crash.

Both repos now put the INSERT under a ``SAVEPOINT`` and read the UNIQUE
violation as what it is, in the shape ``BondsRepo`` established at
``repositories/bonds_repo.py:879``:

* the filter reports "already in the list" — the same answer its own
  pre-check would have given a millisecond later;
* the alias repo does what ADD has always meant (overwrite, legacy
  ``bot.py:42138``) by re-issuing the write as an ``UPDATE``, which
  cannot lose a second time because the failed INSERT has already taken
  the writer lock.

Why this file and not the integration suites: those build their sessions
from a bare ``create_async_engine``, which never installs the
``db/engines.py`` listener — no ``BEGIN IMMEDIATE``, no writer lock, no
race to lose. Here the engines come from :func:`build_registry`, exactly
as production builds them.

The handlers' *other* read-derived decisions are deliberately left
alone. The 500-word and 100-alias ceilings can each be exceeded by one
under the same interleave, and the alias chain rule can be slipped the
same way; neither costs anything. A 501st banned word is a banned word,
and a chained alias is inert — ``GroupAliasMiddleware`` rewrites once
and the rewritten text starts with ``/``, so it never resolves again
(``handlers/group_aliases.py:392``). Taking a database-wide writer
lock on every admin command to buy those two would be the expensive
half of a trade with nothing on the other side.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from telegram_invite_bot.config.settings import AppEnv
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.models.group_aliases import GroupAlias
from telegram_invite_bot.db.models.word_filters import WordFilter
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.repositories.group_aliases_repo import GroupAliasRepo
from telegram_invite_bot.repositories.word_filter_repo import WordFilterRepo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

_GROUP = -1001234567
_WORD = "дурак"
_ALIAS = "привет"


@pytest.fixture
async def registry(make_settings: Callable[..., Settings]) -> AsyncIterator[EngineRegistry]:
    """The production engine set — listeners and pragmas included.

    ``AppEnv.DEV`` because ``install_safety`` refuses an unbounded
    ``UPDATE`` under ``prod``, and the alias repo's overwrite branch is
    an ``UPDATE``; it carries a ``group_id``/``word`` predicate and so is
    bounded either way, but this file should not depend on that reading.
    """
    reg = build_registry(make_settings(AppEnv.DEV))
    try:
        async with reg.engine(DBName.MODERATION).begin() as conn:
            await conn.run_sync(ModerationBase.metadata.create_all)
        yield reg
    finally:
        await reg.dispose()


async def _add_word(registry: EngineRegistry, barrier: asyncio.Barrier, admin: int) -> bool:
    """One ``/filter_add`` on its own session — i.e. its own connection.

    The warm-up ``SELECT`` before the barrier is what makes the race
    actually race: opening the *second* connection costs a pool
    checkout, an aiosqlite worker thread and the connect-time pragmas,
    and that asymmetry alone let the first caller finish before the
    second had connected. A ``SELECT`` is the right warm-up precisely
    because it does not take the writer lock, so it cannot itself
    serialise the two callers.
    """
    async with session_for(registry, DBName.MODERATION) as session:
        await session.execute(select(func.count()).select_from(WordFilter))
        await barrier.wait()
        return await WordFilterRepo(session).add(group_id=_GROUP, word=_WORD, added_by=admin)


async def _add_alias(
    registry: EngineRegistry, barrier: asyncio.Barrier, target: str
) -> tuple[bool, str]:
    """One ``/alias add`` on its own session; returns ``(created, target)``."""
    async with session_for(registry, DBName.MODERATION) as session:
        await session.execute(select(func.count()).select_from(GroupAlias))
        await barrier.wait()
        created = await GroupAliasRepo(session).upsert(
            group_id=_GROUP, word=_ALIAS, target_command=target, added_by=None
        )
        return created, target


async def _count(registry: EngineRegistry, model: type[WordFilter] | type[GroupAlias]) -> int:
    async with session_for(registry, DBName.MODERATION) as session:
        rows = await session.execute(select(func.count()).select_from(model))
        return int(rows.scalar_one())


async def test_two_concurrent_filter_adds_of_one_word_do_not_raise(
    registry: EngineRegistry,
) -> None:
    """The loser reports the duplicate instead of unwinding the update.

    Before the savepoint this ``gather`` raised ``IntegrityError`` —
    which in production meant the errors router, a rolled-back update
    and an error card for an admin whose word had just been saved.
    """
    barrier = asyncio.Barrier(2)
    first, second = await asyncio.gather(
        _add_word(registry, barrier, admin=1), _add_word(registry, barrier, admin=2)
    )

    assert sorted([first, second]) == [False, True], (
        f"both callers claimed to have inserted the row: {first}, {second}"
    )
    assert await _count(registry, WordFilter) == 1


async def test_two_concurrent_alias_adds_of_one_word_keep_the_later_target(
    registry: EngineRegistry,
) -> None:
    """ADD means overwrite, and losing the race must not change that.

    The caller that reports ``False`` is the one whose INSERT hit the
    UNIQUE and fell through to the ``UPDATE``; its target is therefore
    the one that must be stored. Pinning *that* rather than "either of
    the two" is what would catch an overwrite branch that silently
    dropped the write and reported success anyway.
    """
    barrier = asyncio.Barrier(2)
    left, right = await asyncio.gather(
        _add_alias(registry, barrier, "start"), _add_alias(registry, barrier, "help")
    )

    created = [target for made, target in (left, right) if made]
    overwrote = [target for made, target in (left, right) if not made]
    assert len(created) == 1 and len(overwrote) == 1, (
        f"expected one insert and one overwrite, got {left} / {right}"
    )
    assert await _count(registry, GroupAlias) == 1

    async with session_for(registry, DBName.MODERATION) as session:
        stored = await session.execute(
            select(GroupAlias.target_command).where(
                GroupAlias.group_id == _GROUP, GroupAlias.word == _ALIAS
            )
        )
        assert stored.scalar_one() == overwrote[0], (
            "the losing racer reported an overwrite it never performed"
        )
