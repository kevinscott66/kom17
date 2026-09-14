"""``EconomyService`` integration — wallet writes composed with ledger.

The repo-level tests already cover the SQL guards (insufficient
funds, missing user). These tests focus on the *composition* that
the service adds:

* Validation rejection short-circuits without touching the DB.
* Successful writes produce both a wallet update AND a ledger row.
* Failed writes (insufficient funds, missing user) produce no
  ledger row — corruption would be worse than the missing-log
  alternative.
* Transfer is atomic ``debit + credit + ledger`` end-to-end.
* set_balance writes a delta-amount row only when the delta is
  non-zero, shaped so the read side can tell a top-up from a
  deduction, and moves the lifetime counters with it (#257).

We don't re-test the repo guards (e.g. "concurrent debit returns
None") because that's covered in ``test_economy_repo.py``; the
service layer just propagates the repo signal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.economy_service import EconomyService


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def service(session: AsyncSession) -> EconomyService:
    return EconomyService(EconomyRepo(session), TransactionsRepo(session))


async def _ledger_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Transaction))
    return int(result.scalar_one())


async def _seed_wallet(session: AsyncSession, user_id: int, balance: int = 1_000) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


# ---------------------------------------------------------------------------
# credit
# ---------------------------------------------------------------------------


async def test_credit_writes_wallet_and_ledger(
    session: AsyncSession, service: EconomyService
) -> None:
    await _seed_wallet(session, 42)
    wallet = await service.credit(42, 250, type="daily", reason="daily reward")
    await session.commit()

    assert wallet is not None
    assert wallet.balance == 1_250
    assert wallet.total_earned == 250

    rows = (await session.execute(select(Transaction))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.from_id is None
    assert row.to_id == 42
    assert row.amount == 250
    assert row.type == "daily"
    assert row.reason == "daily reward"


async def test_credit_rejects_zero_amount_no_side_effects(
    session: AsyncSession, service: EconomyService
) -> None:
    """validate_credit_amount(0) is False → service short-circuits.
    The DB must be untouched: no wallet bump, no ledger row."""
    await _seed_wallet(session, 42)
    result = await service.credit(42, 0, type="daily")
    await session.commit()

    assert result is None
    assert await _ledger_count(session) == 0
    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.balance == 1_000  # untouched


async def test_credit_rejects_negative_amount(
    session: AsyncSession, service: EconomyService
) -> None:
    await _seed_wallet(session, 42)
    result = await service.credit(42, -50, type="daily")
    await session.commit()

    assert result is None
    assert await _ledger_count(session) == 0


async def test_credit_missing_wallet_returns_none_no_ledger(
    session: AsyncSession, service: EconomyService
) -> None:
    """No wallet → repo returns None → service must NOT write a
    ledger row for a credit that didn't actually happen."""
    result = await service.credit(99999, 100, type="referral")
    await session.commit()

    assert result is None
    assert await _ledger_count(session) == 0


# ---------------------------------------------------------------------------
# debit
# ---------------------------------------------------------------------------


async def test_debit_writes_wallet_and_ledger(
    session: AsyncSession, service: EconomyService
) -> None:
    await _seed_wallet(session, 42)
    wallet = await service.debit(42, 300, type="shop", reason="bought hat")
    await session.commit()

    assert wallet is not None
    assert wallet.balance == 700
    assert wallet.total_spent == 300

    row = (await session.execute(select(Transaction))).scalar_one()
    assert row.from_id == 42
    assert row.to_id is None
    assert row.amount == 300
    assert row.type == "shop"


async def test_debit_insufficient_funds_no_ledger(
    session: AsyncSession, service: EconomyService
) -> None:
    """The load-bearing one: a failed debit MUST NOT leave a
    ledger row claiming it happened — that would make /cstats lie."""
    await _seed_wallet(session, 42, balance=50)
    result = await service.debit(42, 100, type="shop")
    await session.commit()

    assert result is None
    assert await _ledger_count(session) == 0
    # Wallet untouched too.
    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.balance == 50
    assert wallet.total_spent == 0


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------


async def test_transfer_moves_balance_and_logs_one_row(
    session: AsyncSession, service: EconomyService
) -> None:
    """Happy-path transfer: sender debited, recipient credited, one
    ledger row records the move with both endpoints. NOT two rows
    (that would double-count in SUM queries)."""
    await _seed_wallet(session, 1, balance=500)
    await _seed_wallet(session, 2, balance=100)

    result = await service.transfer(from_user_id=1, to_user_id=2, amount=200, reason="gift")
    await session.commit()

    assert result is not None
    sender, recipient = result
    assert sender.balance == 300
    assert recipient.balance == 300

    row = (await session.execute(select(Transaction))).scalar_one()
    assert row.from_id == 1
    assert row.to_id == 2
    assert row.amount == 200
    assert row.type == "transfer"


async def test_transfer_self_returns_none(session: AsyncSession, service: EconomyService) -> None:
    """Self-transfers are pointless (legacy allowed them as wasted
    rows). The service rejects without touching the DB."""
    await _seed_wallet(session, 1)
    result = await service.transfer(from_user_id=1, to_user_id=1, amount=100)
    await session.commit()

    assert result is None
    assert await _ledger_count(session) == 0


async def test_transfer_sender_insufficient_no_partial_state(
    session: AsyncSession, service: EconomyService
) -> None:
    """The first leg (sender debit) fails → no recipient credit, no
    ledger row. The session-level transaction wrapper isn't even
    needed here because we never started writing — but the test
    pins the "no side effects on failure" promise."""
    await _seed_wallet(session, 1, balance=10)
    await _seed_wallet(session, 2, balance=0)

    result = await service.transfer(from_user_id=1, to_user_id=2, amount=100)
    await session.commit()

    assert result is None
    assert await _ledger_count(session) == 0
    # Both wallets untouched.
    repo = EconomyRepo(session)
    assert (await repo.get(1)).balance == 10  # type: ignore[union-attr]
    assert (await repo.get(2)).balance == 0  # type: ignore[union-attr]


async def test_transfer_reverts_the_sender_when_the_recipient_is_missing(
    session: AsyncSession, service: EconomyService
) -> None:
    """A credit that fails after the debit landed takes the debit
    with it, with no help from the caller (#1520).

    This test used to assert the opposite — ``balance == 400``, a
    sender who paid for a transfer that never happened — and called
    it the documented contract, on the grounds that the caller was
    supposed to raise so the middleware would roll back.
    ``BaseSessionMiddleware`` rolls back on a raised exception
    only, and ``transfer`` returns ``None`` instead of raising, so
    "the caller will raise" was a rule no caller was told about and
    none could infer from the return value. The primitive now owns
    its own SAVEPOINT, so the assertion flips: nothing is left
    behind.
    """
    await _seed_wallet(session, 1, balance=500)
    result = await service.transfer(from_user_id=1, to_user_id=2, amount=100)
    await session.commit()

    assert result is None
    wallet = await EconomyRepo(session).get(1)
    assert wallet is not None
    assert wallet.balance == 500
    assert await _ledger_count(session) == 0


async def test_transfer_uses_a_savepoint(
    session: AsyncSession,
    service: EconomyService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the SAVEPOINT call itself, not just its effect.

    The revert above is only observable when the recipient wallet is
    missing. A refactor that dropped the ``begin_nested`` wrapper on
    the happy path would keep every other test in this module green
    while reopening the window, so the call is made observable here
    — the same posture as
    ``test_transfer_uses_savepoint_for_atomicity`` in
    ``tests/integration/services/test_transfer_service.py``.
    """
    await _seed_wallet(session, 1, balance=500)
    await _seed_wallet(session, 2, balance=0)
    real_begin_nested = session.begin_nested
    calls: list[None] = []

    def spy_begin_nested():  # type: ignore[no-untyped-def]
        calls.append(None)
        return real_begin_nested()

    monkeypatch.setattr(session, "begin_nested", spy_begin_nested)

    result = await service.transfer(from_user_id=1, to_user_id=2, amount=100)
    assert result is not None
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# set_balance
# ---------------------------------------------------------------------------


async def test_set_balance_writes_delta_ledger_row(
    session: AsyncSession, service: EconomyService
) -> None:
    await _seed_wallet(session, 42, balance=1_000)
    wallet = await service.set_balance(42, 5_000, admin_id=99, reason="event payout")
    await session.commit()

    assert wallet is not None
    assert wallet.balance == 5_000

    row = (await session.execute(select(Transaction))).scalar_one()
    assert row.from_id == 99
    assert row.to_id == 42
    assert row.amount == 4_000  # delta = 5000 - 1000
    assert row.type == "admin_set"

    # #257: legacy bumps the lifetime counters from the same delta
    # (bot.py:9676-9679). An admin top-up is earned, never spent.
    assert wallet.total_earned == 4_000
    assert wallet.total_spent == 0


async def test_set_balance_decrease_is_recorded_as_a_debit_not_a_credit(
    session: AsyncSession, service: EconomyService
) -> None:
    """#257: an admin deduction used to read back as income.

    The row was written ``from_id=admin, to_id=user`` regardless of
    direction, and the read side derives the sign purely from
    ``to_id == user_id`` (transactions_repo.py:224) — so taking 600
    coins away rendered as "+600". The magnitude is still stored
    positive (transactions_repo.py:19-28); only the from/to pair
    flips.
    """
    await _seed_wallet(session, 42, balance=1_000)
    wallet = await service.set_balance(42, 400, admin_id=99, reason="rollback")
    await session.commit()

    assert wallet is not None
    assert wallet.balance == 400

    row = (await session.execute(select(Transaction))).scalar_one()
    assert row.from_id == 42, "the user is the one losing the coins"
    assert row.to_id == 99
    assert row.amount == 600, "magnitude stays positive; direction is in from/to"
    assert row.type == "admin_set"

    # The observable consequence: the finances panel now shows it as
    # money leaving. This is what the old shape got backwards.
    recent = await TransactionsRepo(session).recent(42)
    assert [tx.signed_amount for tx in recent] == [-600]


async def test_set_balance_decrease_bumps_total_spent_not_earned(
    session: AsyncSession, service: EconomyService
) -> None:
    """Legacy bot.py:9678-9679 routes a negative delta to
    ``update_total_spent(user_id, -amount_diff)`` — a magnitude, on
    the *spent* counter."""
    await _seed_wallet(session, 42, balance=1_000)
    wallet = await service.set_balance(42, 250, admin_id=99)
    await session.commit()

    assert wallet is not None
    assert wallet.total_spent == 750
    assert wallet.total_earned == 0


async def test_set_balance_zero_delta_writes_no_ledger(
    session: AsyncSession, service: EconomyService
) -> None:
    """``set_balance(target=current)`` is a no-op on the wallet and
    must not emit a ledger row. Legacy bot.py:9665 has the same
    ``if amount_diff != 0`` guard."""
    await _seed_wallet(session, 42, balance=1_000)
    wallet = await service.set_balance(42, 1_000, admin_id=99)
    await session.commit()

    assert wallet is not None
    assert wallet.balance == 1_000
    assert await _ledger_count(session) == 0
    # No delta, no history to rewrite either.
    assert wallet.total_earned == 0
    assert wallet.total_spent == 0


async def test_set_balance_negative_target_rejected(
    session: AsyncSession, service: EconomyService
) -> None:
    """``validate_balance_target(-100) is False`` → short-circuit, no
    side effects, no ledger row, wallet untouched."""
    await _seed_wallet(session, 42, balance=1_000)
    result = await service.set_balance(42, -100, admin_id=99)
    await session.commit()

    assert result is None
    assert await _ledger_count(session) == 0
    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.balance == 1_000  # untouched


async def test_hold_books_a_ledger_row_but_no_lifetime_spend(
    session: AsyncSession, service: EconomyService
) -> None:
    """#238: parking coins is a movement to log, not a spend to count.

    Those are two different facts and they live in two different
    columns — the ledger row still has to be there for an audit to
    follow the coins, while ``total_spent`` must stay put because the
    caller may hand every one of them straight back.
    """
    await _seed_wallet(session, 42)
    wallet = await service.hold(42, 300, type="withdraw_escrow", reason="escrow hold")
    await session.commit()

    assert wallet is not None
    assert wallet.balance == 700
    assert wallet.total_spent == 0
    assert wallet.total_earned == 0

    row = (await session.execute(select(Transaction))).scalar_one()
    assert row.from_id == 42
    assert row.to_id is None
    assert row.amount == 300
    assert row.type == "withdraw_escrow"
    assert row.reason == "escrow hold"


async def test_release_books_a_ledger_row_but_no_lifetime_income(
    session: AsyncSession, service: EconomyService
) -> None:
    await _seed_wallet(session, 42)
    wallet = await service.release(42, 300, type="withdraw_refund", reason="rejected refund")
    await session.commit()

    assert wallet is not None
    assert wallet.balance == 1_300
    assert wallet.total_earned == 0
    assert wallet.total_spent == 0

    row = (await session.execute(select(Transaction))).scalar_one()
    assert row.from_id is None
    assert row.to_id == 42
    assert row.amount == 300
    assert row.type == "withdraw_refund"


async def test_hold_release_cycle_leaves_the_balance_card_untouched(
    session: AsyncSession, service: EconomyService
) -> None:
    """The headline: the numbers on ``/balance`` survive the round trip.

    ``debit``/``credit`` added 300 to both lifetime totals per cycle
    while the balance came back to exactly where it started.
    """
    await _seed_wallet(session, 42)
    for _ in range(3):
        assert await service.hold(42, 300, type="withdraw_escrow") is not None
        assert await service.release(42, 300, type="withdraw_refund") is not None
    await session.commit()

    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.balance == 1_000
    assert wallet.total_spent == 0
    assert wallet.total_earned == 0

    # The movements are still fully auditable — six rows, not zero.
    assert await _ledger_count(session) == 6


async def test_hold_rejects_invalid_amount_without_touching_the_db(
    session: AsyncSession, service: EconomyService
) -> None:
    await _seed_wallet(session, 42)
    assert await service.hold(42, 0, type="withdraw_escrow") is None
    assert await service.hold(42, -5, type="withdraw_escrow") is None
    await session.commit()

    assert await _ledger_count(session) == 0
    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.balance == 1_000


async def test_hold_shortfall_writes_no_ledger_row(
    session: AsyncSession, service: EconomyService
) -> None:
    """A refused hold must not leave a phantom escrow row behind."""
    await _seed_wallet(session, 42, balance=100)
    assert await service.hold(42, 101, type="withdraw_escrow") is None
    await session.commit()

    assert await _ledger_count(session) == 0


async def test_release_to_missing_wallet_writes_no_ledger_row(
    session: AsyncSession, service: EconomyService
) -> None:
    assert await service.release(99999, 100, type="withdraw_refund") is None
    await session.commit()

    assert await _ledger_count(session) == 0


async def test_settle_hold_books_the_spend_without_moving_coins(
    session: AsyncSession, service: EconomyService
) -> None:
    """#1546: the third leg — an escrow the caller decided to keep.

    ``hold`` deliberately leaves ``total_spent`` alone; a purchase that
    actually completed still has to reach that column or ``/balance``
    under-reports what the user paid.
    """
    await _seed_wallet(session, 42)
    assert await service.hold(42, 300, type="couple_activity") is not None
    wallet = await service.settle_hold(42, 300)
    await session.commit()

    assert wallet is not None
    assert wallet.balance == 700  # unchanged by the settlement itself
    assert wallet.total_spent == 300
    assert wallet.total_earned == 0

    # One row, written by the hold. The settlement books no second one:
    # the coins already moved once and ``/cstats`` would double-count.
    assert await _ledger_count(session) == 1


async def test_hold_then_settle_matches_debit_exactly(
    session: AsyncSession, service: EconomyService
) -> None:
    """The migrated call sites must end where ``debit`` used to."""
    await _seed_wallet(session, 42)
    await _seed_wallet(session, 43)

    assert await service.debit(42, 250, type="couple_activity") is not None
    assert await service.hold(43, 250, type="couple_activity") is not None
    assert await service.settle_hold(43, 250) is not None
    await session.commit()

    repo = EconomyRepo(session)
    old, new = await repo.get(42), await repo.get(43)
    assert old is not None
    assert new is not None
    assert (new.balance, new.total_spent, new.total_earned) == (
        old.balance,
        old.total_spent,
        old.total_earned,
    )


async def test_settle_hold_rejects_a_negative_amount(
    session: AsyncSession, service: EconomyService
) -> None:
    """A negative settlement would *reduce* a lifetime total — rewriting history."""
    await _seed_wallet(session, 42)
    assert await service.settle_hold(42, -1) is None
    await session.commit()

    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.total_spent == 0
    assert wallet.balance == 1_000


async def test_settle_hold_on_a_missing_wallet_returns_none(
    session: AsyncSession, service: EconomyService
) -> None:
    assert await service.settle_hold(99999, 100) is None
    await session.commit()

    assert await _ledger_count(session) == 0
