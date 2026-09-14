"""#1943: a duplicate mint must report DUPLICATE, not crash.

:meth:`PromoService.create_code` checks for a collision with
``get_by_code`` and then inserts. That probe is a ``SELECT``, so it
promotes nothing and takes no writer lock (``db/engines.py``'s
``_promote_to_write_txn`` leaves reads in autocommit deliberately) —
two developers minting the same code can both pass it, and only the
``UNIQUE(code)`` constraint stops the second.

Which it did, by raising. Nothing caught it: both docstrings said the
service "catches that to report DUPLICATE" and neither the service nor
the repository contained an ``except``. So the loser's
:class:`~sqlalchemy.exc.IntegrityError` travelled up to
``handlers/errors.py`` and became the generic error card — a developer
told their mint had crashed, for a code that simply already existed,
with ``h_promo_create_duplicate`` sitting right there unused.

No coins were ever at risk: the constraint held, so the row was never
written twice. The defect is the answer, and the session state behind
it — a failed statement left in flight is one ``EconomyMiddleware``
cannot commit.

The race is imposed rather than waited for, the same way #1941 does it:
the probe is made to miss on a code that is already in the table, which
is exactly the state the losing coroutine sees. Whether that miss comes
from a real interleaving or from this monkeypatch, the INSERT below it
is identical.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

from telegram_invite_bot.db.models.base import EconomyBase

# Registers the promo tables on ``EconomyBase`` for ``create_all``.
from telegram_invite_bot.db.models.promo import PromoCode  # noqa: F401
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.promo_repo import PromoRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.promo_service import (
    CreateOutcome,
    PromoService,
)
from tests.integration.repositories._session import build_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 9, 12, 0, 0)
_CODE = "WELCOME2026"
_DEV = 999_000


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


def _service(session: AsyncSession) -> PromoService:
    return PromoService(
        PromoRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
        session,
    )


async def _mint(session: AsyncSession, code: str = _CODE) -> CreateOutcome:
    result = await _service(session).create_code(
        code=code,
        reward_coins=100,
        max_uses=10,
        per_user_once=True,
        created_by=_DEV,
        now=_NOW,
    )
    return result.outcome


def _blind_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``get_by_code`` miss — what the losing mint actually sees."""

    async def blind(_self: PromoRepo, _code: str) -> Any:
        return None

    monkeypatch.setattr(PromoRepo, "get_by_code", blind)


async def test_a_mint_that_loses_the_race_reports_duplicate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: the constraint fires and the answer stays honest."""
    assert await _mint(session) is CreateOutcome.OK
    await session.commit()

    _blind_the_probe(monkeypatch)

    assert await _mint(session) is CreateOutcome.DUPLICATE


async def test_the_loser_leaves_a_usable_session(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: the middleware must still be able to commit.

    A failed statement left in flight poisons the transaction, and
    ``EconomyMiddleware`` rolls back only on a RAISED exception — so
    returning DUPLICATE without the rollback would hand it a session
    that refuses to commit, turning a tidy refusal into the crash it
    was supposed to replace.
    """
    assert await _mint(session) is CreateOutcome.OK
    await session.commit()

    _blind_the_probe(monkeypatch)
    assert await _mint(session) is CreateOutcome.DUPLICATE

    monkeypatch.undo()
    assert await _mint(session, "SECOND2026") is CreateOutcome.OK
    await session.commit()

    assert await PromoRepo(session).get_by_code("SECOND2026") is not None


async def test_the_row_is_not_written_twice(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What was never in danger, pinned so it stays that way.

    The constraint is the guard; the fix only changes how its refusal
    is reported. If a later edit "fixed" this by dropping the
    constraint, everything above would still pass and this would not.
    """
    assert await _mint(session) is CreateOutcome.OK
    await session.commit()

    _blind_the_probe(monkeypatch)
    await _mint(session)
    monkeypatch.undo()

    stored = await PromoRepo(session).get_by_code(_CODE)
    assert stored is not None
    assert stored.reward_coins == 100


async def test_the_probe_still_answers_first(session: AsyncSession) -> None:
    """The control: the uncontended duplicate never reaches the INSERT.

    The probe exists so the common case costs a read rather than a
    failed write plus a rollback, and it must keep doing that work.
    """
    assert await _mint(session) is CreateOutcome.OK
    await session.commit()

    assert await _mint(session) is CreateOutcome.DUPLICATE
    # No rollback happened, so the committed row is still visible and
    # the session was never disturbed.
    assert await PromoRepo(session).get_by_code(_CODE) is not None
