"""#1567 — ``/voice`` billing must not move the lifetime counters.

``TtsService.synthesize`` pre-authorises the coin charge before the
upstream OpenAI call and hands it back on every failure path
(upstream exception, non-bytes response, oversize audio). That leg
used to be ``EconomyService.debit`` with a matching ``credit`` for the
refund, and those two primitives move ``total_spent`` and
``total_earned`` on top of the balance column. A synthesis that failed
therefore inflated BOTH lifetime counters for free — no counterparty,
no cost, repeatable as fast as the AI rate limiter allows, and cheapest
of all when the upstream is the thing that is broken.

The pre-authorisation is now ``hold`` and the refund ``release``
(balance column only); a synthesis that actually succeeds books the
kept coins as a real spend via ``settle_hold``. This module pins both
halves: counters frozen across every refund path, and ``total_spent``
moved exactly once — at settlement — on the success path.

#1895 moved settlement OUT of the service. ``synthesize`` returns
SUCCESS with the hold still open, and the caller closes it: settle
once the audio has actually been delivered, release if it could not
be. Settling inside the service left the delivery-failure path with
``credit`` as its only way back — the one primitive that can put
coins on top of a completed spend — so an undeliverable upload ended
the round with the balance whole and BOTH counters inflated. The
success tests below therefore pin two separate facts: what the
service leaves behind, and what each of the caller's two closing
legs does to it.

``tests/regression/test_money_call_sites.py`` cannot catch this class:
it has no opinion about WHICH primitive a call site picked, only that
its result is checked and ledgered.
"""

from __future__ import annotations

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
from telegram_invite_bot.services.tts_service import TtsOutcome, TtsService

pytestmark = pytest.mark.asyncio

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


async def _totals(session: AsyncSession, user_id: int = 1) -> tuple[int, int]:
    """Return ``(total_spent, total_earned)`` straight off the wallet row."""
    row = (
        await session.execute(
            select(EconomyUser.total_spent, EconomyUser.total_earned).where(
                EconomyUser.user_id == user_id
            )
        )
    ).one()
    return int(row[0]), int(row[1])


async def _balance(session: AsyncSession, user_id: int = 1) -> int:
    return int(
        (
            await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == user_id))
        ).scalar_one()
    )


async def _ledger_types(session: AsyncSession) -> list[str]:
    rows = (await session.execute(select(Transaction))).scalars().all()
    return sorted(r.type for r in rows)


def _service(
    session: AsyncSession,
    *,
    response: object | None = None,
    raises: Exception | None = None,
    config: TtsConfig | None = None,
) -> TtsService:
    create_mock = AsyncMock()
    if raises is not None:
        create_mock.side_effect = raises
    else:
        create_mock.return_value = response
    client: Any = SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create_mock)))
    economy = EconomyService(EconomyRepo(session), TransactionsRepo(session))
    return TtsService(config or TtsConfig(), economy, client=client)


# ---------------------------------------------------------------------------
# Refund paths: balance round-trips, counters never move
# ---------------------------------------------------------------------------


async def test_upstream_error_refund_leaves_counters_frozen(session: AsyncSession) -> None:
    await _seed(session)
    svc = _service(session, raises=RuntimeError("boom"))

    result = await svc.synthesize(user_id=1, text="hello", balance=START)

    assert result.outcome is TtsOutcome.UPSTREAM_ERROR
    assert await _balance(session) == START
    assert await _totals(session) == (0, 0)
    # The movement is still auditable — hold and release each ledger.
    assert await _ledger_types(session) == ["tts", "tts_refund"]


async def test_non_bytes_response_refund_leaves_counters_frozen(session: AsyncSession) -> None:
    await _seed(session)
    svc = _service(session, response=SimpleNamespace(content="not-bytes"))

    result = await svc.synthesize(user_id=1, text="hello", balance=START)

    assert result.outcome is TtsOutcome.UPSTREAM_ERROR
    assert await _balance(session) == START
    assert await _totals(session) == (0, 0)


async def test_oversize_audio_refund_leaves_counters_frozen(session: AsyncSession) -> None:
    await _seed(session)
    svc = _service(
        session,
        response=SimpleNamespace(content=b"x" * 4096),
        config=TtsConfig(OPENAI_TTS_MAX_AUDIO_BYTES=1024),
    )

    result = await svc.synthesize(user_id=1, text="hello", balance=START)

    assert result.outcome is TtsOutcome.AUDIO_TOO_LARGE
    assert await _balance(session) == START
    assert await _totals(session) == (0, 0)


async def test_repeated_failures_never_inflate_counters(session: AsyncSession) -> None:
    """The abuse shape the ticket describes: fail in a loop, gain history.

    Under debit/credit each round trip added ``cost`` to BOTH counters
    while the balance ended where it started, so a user with a broken
    upstream could farm a lifetime spend/earn record at zero cost.
    """
    await _seed(session)
    svc = _service(session, raises=RuntimeError("boom"))

    for _ in range(5):
        result = await svc.synthesize(user_id=1, text="hello", balance=START)
        assert result.outcome is TtsOutcome.UPSTREAM_ERROR

    assert await _balance(session) == START
    assert await _totals(session) == (0, 0)


# ---------------------------------------------------------------------------
# Success path: the service hands the caller an OPEN hold to close
# ---------------------------------------------------------------------------


async def test_success_leaves_the_hold_open_for_the_caller(session: AsyncSession) -> None:
    """#1895: SUCCESS means "the bytes exist", not "the user paid".

    The coins are out of the wallet — the hold is committed and the
    ledger row written — but nothing has reached ``total_spent``.
    That last step is the caller's, because only the caller finds
    out whether the audio ever reached Telegram.
    """
    await _seed(session)
    svc = _service(session, response=SimpleNamespace(content=b"mp3"))

    result = await svc.synthesize(user_id=1, text="helloworld", balance=START)

    assert result.outcome is TtsOutcome.SUCCESS
    cost = result.coins_charged
    assert cost == 1
    assert await _balance(session) == START - cost
    # Escrowed, not spent: the hold moved the balance and nothing else.
    assert await _totals(session) == (0, 0)
    assert await _ledger_types(session) == ["tts"]


async def test_settling_the_open_hold_books_the_spend_exactly_once(
    session: AsyncSession,
) -> None:
    """The caller's success leg: settle after the audio has landed."""
    await _seed(session)
    svc = _service(session, response=SimpleNamespace(content=b"mp3"))
    economy = EconomyService(EconomyRepo(session), TransactionsRepo(session))

    result = await svc.synthesize(user_id=1, text="helloworld", balance=START)
    assert result.outcome is TtsOutcome.SUCCESS
    cost = result.coins_charged

    settled = await economy.settle_hold(1, cost)

    assert settled is not None
    assert await _balance(session) == START - cost
    # Spend booked once; nothing was ever earned.
    assert await _totals(session) == (cost, 0)
    # settle_hold moves no coins and writes no row — the hold already did.
    assert await _ledger_types(session) == ["tts"]


async def test_releasing_after_a_successful_synthesis_freezes_both_counters(
    session: AsyncSession,
) -> None:
    """The caller's failure leg: the upload died, so undo the hold.

    This is the #1895 round in full. Because the hold is still open,
    ``release`` is available and it touches neither counter. Had the
    service settled on the way out, the only way back would have been
    ``credit`` — balance restored, ``total_spent`` AND ``total_earned``
    up by the cost, and ``EconomyRepo.bump_totals`` refusing the
    negative arguments that would undo either one.
    """
    await _seed(session)
    svc = _service(session, response=SimpleNamespace(content=b"mp3"))
    economy = EconomyService(EconomyRepo(session), TransactionsRepo(session))

    result = await svc.synthesize(user_id=1, text="helloworld", balance=START)
    assert result.outcome is TtsOutcome.SUCCESS
    cost = result.coins_charged

    released = await economy.release(1, cost, type="tts_refund", reason="delivery_failed")

    assert released is not None
    assert await _balance(session) == START
    assert await _totals(session) == (0, 0)
    assert await _ledger_types(session) == ["tts", "tts_refund"]


async def test_skip_billing_moves_neither_balance_nor_counters(session: AsyncSession) -> None:
    await _seed(session, balance=0)
    svc = _service(session, response=SimpleNamespace(content=b"gift"))

    result = await svc.synthesize(user_id=1, text="hi", balance=0, skip_billing=True)

    assert result.outcome is TtsOutcome.SUCCESS
    assert result.coins_charged == 0
    assert await _balance(session) == 0
    assert await _totals(session) == (0, 0)
    assert await _ledger_types(session) == []


async def test_pre_check_refusal_writes_nothing(session: AsyncSession) -> None:
    """Insufficient balance refuses before the hold, so nothing moves."""
    await _seed(session, balance=0)
    svc = _service(session, response=SimpleNamespace(content=b"mp3"))

    result = await svc.synthesize(user_id=1, text="hello", balance=0)

    assert result.outcome is TtsOutcome.INSUFFICIENT_BALANCE
    assert await _balance(session) == 0
    assert await _totals(session) == (0, 0)
    assert await _ledger_types(session) == []
