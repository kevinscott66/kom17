"""#2021 — ``/marry_top_off`` must actually take the pair off the board.

``BondsWriteRepo.set_marriage_in_top`` flips ``marriages.in_top`` and
answers «☆ Твой брак исключён из рейтинга» / «☆ Your marriage is now
excluded from the rating». Nothing has ever read the column back:
``MarriagesRepo.list_active`` and ``count_active`` filter on ``status``
alone, so both commands are pure theatre and the couple who asked to be
left out of the board stays on it.

Legacy is where it comes from — ``bot.py:22740``/``:22758`` write the
flag and ``bot.py:22991`` ignores it — but a faithfully ported promise
the bot cannot keep is still a promise the bot cannot keep, and this one
is stated in two languages.

The reading is «1 is on the board»: the flag's own commands say so, the
column defaults to 0, and the two are only reconcilable one way — every
marriage written before this must be backfilled to 1, because every one
of them is on the board today. That backfill cannot tell an untouched 0
from a deliberate ``/marry_top_off`` (both write the same zero, and the
opt-out never had an effect to preserve), so a couple who opted out
before this lands is opted back in and has to say it once more. Said
here so the next reader does not have to rediscover it from the
migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import Marriage, User
from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo, MarriagesRepo
from tests.integration.repositories._session import build_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_CHAT = 4242


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, UsersBase, "users.db") as s:
        yield s


async def _married(session: AsyncSession, a: int, b: int, *, xp: int = 100) -> None:
    """One active pair, written the way every other row in the table is.

    Deliberately WITHOUT naming ``in_top``: what the board shows for a
    marriage nobody has touched is the whole question, and a test that
    passed the flag explicitly would answer it for the code.
    """
    session.add_all(
        [
            User(user_id=a, first_name=f"U{a}"),
            User(user_id=b, first_name=f"U{b}"),
            Marriage(
                chat_id=_CHAT,
                user1_id=a,
                user2_id=b,
                created_at=datetime(2026, 1, 1),  # noqa: DTZ001 — naive, as the column is
                experience=xp,
                status="active",
            ),
        ]
    )
    await session.commit()


async def test_a_new_marriage_is_on_the_board_without_being_asked(
    session: AsyncSession,
) -> None:
    """The opt-out is an opt-OUT; joining costs nothing."""
    await _married(session, 1, 2)

    assert len(await MarriagesRepo(session).list_active(_CHAT, limit=10)) == 1
    assert await MarriagesRepo(session).count_active(_CHAT) == 1


async def test_marry_top_off_removes_the_pair_from_the_board(
    session: AsyncSession,
) -> None:
    """The defect: the flag flips, the board does not notice."""
    await _married(session, 1, 2)
    assert await BondsWriteRepo(session).set_marriage_in_top(_CHAT, 1, in_top=False)
    await session.commit()

    pairs = await MarriagesRepo(session).list_active(_CHAT, limit=10)
    assert pairs == [], (
        "the couple was told they are off the rating and is still on it: "
        f"{[(p.user1_id, p.user2_id) for p in pairs]}"
    )


async def test_the_more_line_does_not_count_the_pairs_it_hides(
    session: AsyncSession,
) -> None:
    """``count_active`` feeds «…and N more», so it must agree with the list.

    ``handlers/relations._hidden_count`` subtracts the rows rendered from
    this count. A count that still includes the opted-out pairs turns
    the opt-out into a different lie — the board would promise more rows
    than it is willing to show, forever.
    """
    await _married(session, 1, 2)
    await _married(session, 3, 4)
    assert await BondsWriteRepo(session).set_marriage_in_top(_CHAT, 3, in_top=False)
    await session.commit()

    assert await MarriagesRepo(session).count_active(_CHAT) == 1


async def test_turning_it_back_on_puts_the_pair_back(session: AsyncSession) -> None:
    """The toggle has to work in both directions, on the same row."""
    write = BondsWriteRepo(session)
    await _married(session, 1, 2)
    assert await write.set_marriage_in_top(_CHAT, 2, in_top=False)
    assert await write.set_marriage_in_top(_CHAT, 2, in_top=True)
    await session.commit()

    assert len(await MarriagesRepo(session).list_active(_CHAT, limit=10)) == 1
    assert await MarriagesRepo(session).count_active(_CHAT) == 1


async def test_a_pair_that_opted_out_still_sees_its_own_marriage(
    session: AsyncSession,
) -> None:
    """The flag hides them from the BOARD, not from themselves.

    ``list_for_user`` renders the ``/marriage`` card — the couple's own
    row. Filtering it too would make the opt-out read as a divorce.
    """
    await _married(session, 1, 2)
    assert await BondsWriteRepo(session).set_marriage_in_top(_CHAT, 1, in_top=False)
    await session.commit()

    assert len(await MarriagesRepo(session).list_for_user(1, limit=10)) == 1
