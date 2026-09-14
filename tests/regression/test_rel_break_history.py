"""#2022 — ``/rel_break`` must take the pair's history with it.

``BondsWriteRepo.terminate_relationship`` is a hard DELETE: the
``relationships`` row is gone, and the next relationship between the
same two people is a brand-new row with ``experience=0``. The
``relationship_activity_log`` rows are not deleted with it, and the
history view addresses them by ``(chat_id, user1_id, user2_id)`` alone
(``get_relationship_activity_log``) — so the new couple's card opens on
the OLD couple's dinners and gifts, dated before the relationship it is
attached to existed, each line reading out XP the pair's counter says
they never earned.

Legacy is the source (``bot.py:22064`` deletes the pair row and nothing
else), but the new bot is where it hurts: L-34/L-35 gave the log a
button, and a log nobody could read was harmless.

The rule this restores is the one ``accept_proposal`` states for
marriages — XP and history travel WITH the bond row. A bond that
survives keeps both (the marriage revive keeps its XP, and so does
``_create_relationship`` when it reactivates a soft-ended pair, so both
keep their log). A bond that is deleted keeps neither. The soft end
from the absence sweep is deliberately on the first side of that line
and is pinned here as such.

Not addressed here: the orphans already sitting in prod, left by every
``/rel_break`` before this fix. Cleaning them means an irreversible
anti-join DELETE against live user data to tidy rows that are invisible
unless a pair re-forms in the same chat — a call for the owner to make,
not a side effect of a regression fix.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.bond_activity import (
    MarriageActivityLog,
    RelationshipActivityLog,
)
from telegram_invite_bot.db.models.users import Relationship
from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
from tests.integration.repositories._session import build_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_CHAT = 5150
_OTHER_CHAT = 5151
_NOW = datetime(2026, 9, 11, 12, 0, 0)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, UsersBase, "users.db") as s:
        yield s


async def _paired(session: AsyncSession, a: int, b: int, *, chat_id: int = _CHAT) -> None:
    """One active relationship plus one logged activity for it."""
    session.add(
        Relationship(
            chat_id=chat_id,
            user1_id=min(a, b),
            user2_id=max(a, b),
            created_at=_NOW,
            experience=40,
            status="active",
        )
    )
    await BondsWriteRepo(session).log_relationship_activity(
        chat_id, a, b, "dinner", 20, paid_by_user_id=a
    )
    await session.flush()


async def _log_rows(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(RelationshipActivityLog)) or 0


async def test_rel_break_deletes_the_pairs_activity_log(session: AsyncSession) -> None:
    """The hard DELETE takes the log rows it orphans."""
    await _paired(session, 1, 2)
    assert await _log_rows(session) == 1

    assert await BondsWriteRepo(session).terminate_relationship(_CHAT, 1, 2) is True

    assert await _log_rows(session) == 0, (
        "the relationship row is gone and its activity log outlived it"
    )


async def test_a_new_relationship_does_not_inherit_the_old_ones_history(
    session: AsyncSession,
) -> None:
    """The user-visible half: a fresh pair opens on an empty history.

    Their XP counter starts at 0 — a card that lists twenty XP of
    dinners next to a total of zero is the contradiction this is about.
    """
    repo = BondsWriteRepo(session)
    await _paired(session, 1, 2)
    await repo.terminate_relationship(_CHAT, 1, 2)

    assert await repo._create_relationship(_CHAT, 1, 2) is True
    fresh = await repo.get_relationship(_CHAT, 1, 2)
    assert fresh is not None
    assert fresh.experience == 0

    assert await repo.get_relationship_activity_log(_CHAT, 1, 2) == [], (
        "the new couple's history card opened on the previous couple's log"
    )


async def test_the_delete_is_scoped_to_the_pair_and_the_chat(session: AsyncSession) -> None:
    """Nobody else's history is collateral.

    Three logs exist: the pair being broken up, a different pair in the
    same chat, and the SAME two people in another group — relationships
    are per-chat, and breaking up in one says nothing about the other.
    """
    repo = BondsWriteRepo(session)
    await _paired(session, 1, 2)
    await _paired(session, 3, 4)
    await _paired(session, 1, 2, chat_id=_OTHER_CHAT)

    await repo.terminate_relationship(_CHAT, 1, 2)

    assert len(await repo.get_relationship_activity_log(_CHAT, 3, 4)) == 1
    assert len(await repo.get_relationship_activity_log(_OTHER_CHAT, 1, 2)) == 1
    assert await _log_rows(session) == 2


async def test_the_marriage_log_is_a_different_table_and_survives(
    session: AsyncSession,
) -> None:
    """``/rel_break`` ends a relationship, not a marriage.

    The two logs are keyed identically — ``(chat_id, user1_id,
    user2_id)`` — which is exactly why a delete written against the
    wrong one would be invisible in every other test here.
    """
    repo = BondsWriteRepo(session)
    await _paired(session, 1, 2)
    await repo.log_marriage_activity(_CHAT, 1, 2, "big_gift", 50, paid_by_user_id=2)
    await session.flush()

    await repo.terminate_relationship(_CHAT, 1, 2)

    survived = await session.scalar(select(func.count()).select_from(MarriageActivityLog))
    assert survived == 1, "the relationship break-up wiped the couple's marriage history"


async def test_a_soft_ended_relationship_keeps_its_history(session: AsyncSession) -> None:
    """The absence sweep is the other side of the line.

    ``end_relationships_for`` leaves the row standing so
    ``_create_relationship`` can reactivate it with its XP intact; the
    log has to stay with the XP it explains.
    """
    repo = BondsWriteRepo(session)
    await _paired(session, 1, 2)

    assert await repo.end_relationships_for(_CHAT, 1, now=_NOW) == 1

    assert len(await repo.get_relationship_activity_log(_CHAT, 1, 2)) == 1, (
        "a soft end took the history the reactivated pair still owns"
    )
