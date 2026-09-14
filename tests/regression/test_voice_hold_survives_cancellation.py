"""#1954: a cancelled ``/voice`` must not keep the pre-authorised coins.

``TtsService.synthesize`` commits the hold BEFORE the upstream call —
deliberately, so a minute of synthesis cannot sit on the single SQLite
writer slot (see the ``checkpoint`` paragraph in its docstring). The
price of that is that every path which ends without audio has to hand
the coins back in-band, because there is no longer a transaction to
roll back.

Every such path was covered except one: ``asyncio.CancelledError`` is a
``BaseException``, so the broad ``except Exception`` around the upstream
call — and the one around the upload in the handler — walked straight
past it. The window is the whole upstream call, up to
:attr:`TtsConfig.timeout_seconds` (sixty by default), and what closes it
is an ordinary deploy: uvicorn's bounded graceful shutdown cancels
whatever is still in flight, the handler unwinds, and the user is out
the coins with no audio and no ledger row explaining why.

The compensation is best-effort by nature — a task gets exactly one
``CancelledError`` from ``cancel()``, so the awaits in the release do
run, but a loop already tearing down may not get that far. What must
never happen is the release failure replacing the cancellation the
caller is waiting on, which the last test below pins.

The handler's half (a cancel between synthesis and upload) lives with
its siblings in ``tests/e2e/handlers/test_vip_emoji_voice.py``, next to
the delivery-failure tests it mirrors.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.config.settings import TtsConfig
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.tts_service import TtsService

START = 100


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed(session: AsyncSession, user_id: int = 1, balance: int = START) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


async def _balance(session: AsyncSession, user_id: int = 1) -> int:
    return int(
        (
            await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == user_id))
        ).scalar_one()
    )


async def _totals(session: AsyncSession, user_id: int = 1) -> tuple[int, int]:
    row = (
        await session.execute(
            select(EconomyUser.total_spent, EconomyUser.total_earned).where(
                EconomyUser.user_id == user_id
            )
        )
    ).one()
    return int(row[0]), int(row[1])


async def _ledger_types(session: AsyncSession) -> list[str]:
    rows = (await session.execute(select(Transaction))).scalars().all()
    return sorted(r.type for r in rows)


def _cancelling_service(session: AsyncSession) -> tuple[TtsService, EconomyService]:
    """A service whose upstream call is cancelled instead of answering.

    ``side_effect`` carries a ``BaseException`` here, which is the whole
    point: an ``Exception`` would take the already-covered
    UPSTREAM_ERROR branch and prove nothing.
    """
    create_mock = AsyncMock(side_effect=asyncio.CancelledError())
    client: Any = SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create_mock)))
    economy = EconomyService(EconomyRepo(session), TransactionsRepo(session))
    return TtsService(TtsConfig(), economy, client=client), economy


async def test_a_cancelled_synthesis_hands_the_coins_back(session: AsyncSession) -> None:
    """The ticket itself: shutdown mid-call, wallet whole afterwards."""
    await _seed(session)
    service, _ = _cancelling_service(session)

    with pytest.raises(asyncio.CancelledError):
        await service.synthesize(user_id=1, text="helloworld", balance=START)

    assert await _balance(session) == START
    # Hold and release both ledger, so the round trip stays auditable —
    # and neither lifetime counter moved, same as every other refund
    # path (#1567).
    assert await _ledger_types(session) == ["tts", "tts_refund"]
    assert await _totals(session) == (0, 0)


async def test_the_cancellation_still_reaches_the_caller(session: AsyncSession) -> None:
    """Compensating must not turn a cancel into a normal return.

    Swallowing it — returning UPSTREAM_ERROR, say — would leave the
    handler happily sending a refusal card into a loop that is being
    torn down, and would break the one guarantee every ``await`` in the
    call chain relies on.
    """
    await _seed(session)
    service, _ = _cancelling_service(session)

    with pytest.raises(asyncio.CancelledError):
        await service.synthesize(user_id=1, text="helloworld", balance=START)


async def test_a_gifted_round_has_no_hold_to_release(session: AsyncSession) -> None:
    """``skip_billing`` never took the coins, so nothing is handed back.

    A compensation that fired unconditionally would credit a user who
    never paid — the mirror-image bug, and a free one to farm at the
    voice limiter's rate.
    """
    await _seed(session)
    service, _ = _cancelling_service(session)

    with pytest.raises(asyncio.CancelledError):
        await service.synthesize(user_id=1, text="helloworld", balance=START, skip_billing=True)

    assert await _balance(session) == START
    assert await _ledger_types(session) == []


async def test_a_failing_release_does_not_mask_the_cancellation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort means best-effort: the cancel wins either way.

    On a loop that is already closing the release can itself blow up.
    Letting that exception out would replace ``CancelledError`` with a
    ``RuntimeError`` at every frame above — the shutdown path would log
    a crash instead of a clean cancel, and ``Application.close`` would
    treat the task as having died rather than having been stopped.
    """
    await _seed(session)
    service, economy = _cancelling_service(session)

    async def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("economy.db is gone")

    monkeypatch.setattr(economy, "release", _explode)

    with pytest.raises(asyncio.CancelledError):
        await service.synthesize(user_id=1, text="helloworld", balance=START)
