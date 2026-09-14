"""``PaymentsService`` integration — idempotency + credit + ledger.

Drives the service against a real SQLite economy DB via the real
EconomyRepo / TransactionsRepo / ProcessedWebhooksRepo. Adapter and
Bot are stubbed because the service doesn't construct them — it
receives a ``ParsedEvent`` from the router.

Covers:

* Happy path → wallet credited, ledger row written, processed_webhooks
  row written. Outcome ``CREDITED``.
* Duplicate (existing processed_webhooks row) → no credit, no extra
  ledger row. Outcome ``IDEMPOTENT``.
* Unknown user (no wallet) → the row is seeded and the credit lands
  anyway (#770). Outcome ``CREDITED``.
* Credit the economy layer refuses (the balance ceiling, which is
  what is left once #770 removed the missing-wallet case) → no
  rows. Outcome ``CREDIT_REFUSED``.
* Invalid amount → outcome ``INVALID_AMOUNT``, no rows.
* Multi-provider isolation: same external_id under different providers
  is NOT a duplicate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    ProcessedWebhook,
    Transaction,
)
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.processed_webhooks_repo import (
    ProcessedWebhooksRepo,
)
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.payments.base import ParsedEvent, Provider
from telegram_invite_bot.services.payments_service import (
    CreditOutcome,
    PaymentsService,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT


@pytest.fixture
async def maker(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessionmaker over a fresh economy DB.

    Exposed separately from ``session`` because the R15 tests need to
    open and *close* a ``session.begin()`` block the way prod does
    (``webhook/payments.py:_credit_event``), then reopen a clean
    session to read what actually survived the commit.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session(
    maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with maker() as s:
        yield s


@pytest.fixture
async def service(session: AsyncSession) -> PaymentsService:
    return PaymentsService(
        economy=EconomyService(EconomyRepo(session), TransactionsRepo(session)),
        idempotency=ProcessedWebhooksRepo(session),
        bot=None,
    )


async def _seed(session: AsyncSession, user_id: int, balance: int = 100) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


def _event(
    *,
    provider: Provider = Provider.CRYPTO,
    external_id: str = "EXT-1",
    user_id: int = 1,
    coins: int = 50,
    fiat_amount: Decimal | None = None,
    fiat_currency: str | None = None,
    fx_rate: Decimal | None = None,
) -> ParsedEvent:
    return ParsedEvent(
        provider=provider,
        external_id=external_id,
        user_id=user_id,
        coins=coins,
        reason="test",
        fiat_amount=fiat_amount,
        fiat_currency=fiat_currency,
        fx_rate=fx_rate,
    )


async def _processed_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(ProcessedWebhook))
    return int(result.scalar_one())


async def _ledger_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Transaction))
    return int(result.scalar_one())


async def test_credit_happy_path(service: PaymentsService, session: AsyncSession) -> None:
    await _seed(session, user_id=1, balance=100)
    outcome = await service.handle_event(_event(coins=50))
    assert outcome is CreditOutcome.CREDITED
    # Wallet
    row = await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == 1))
    assert row.scalar_one() == 150
    assert await _processed_count(session) == 1
    assert await _ledger_count(session) == 1


async def test_duplicate_is_idempotent(service: PaymentsService, session: AsyncSession) -> None:
    await _seed(session, user_id=1, balance=100)
    o1 = await service.handle_event(_event())
    o2 = await service.handle_event(_event())
    assert o1 is CreditOutcome.CREDITED
    assert o2 is CreditOutcome.IDEMPOTENT
    # Wallet moved exactly once.
    row = await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == 1))
    assert row.scalar_one() == 150
    assert await _processed_count(session) == 1
    assert await _ledger_count(session) == 1


async def test_unknown_user_is_seeded_and_credited(
    service: PaymentsService, session: AsyncSession
) -> None:
    """#770: the buyer already paid — a missing wallet must not refuse.

    Legacy self-healed on this exact path (``register_user`` inside
    ``add_coins``, bot.py:9724, commented "иначе UPDATE ничего не
    изменит"), and the port already does it for everyone paid *out of*
    this payment (referral_commission_service.py:267, :365). The payer
    was the last party still refused, and the only one whose money is
    definitely already gone.
    """
    outcome = await service.handle_event(_event(user_id=999, coins=50))
    assert outcome is CreditOutcome.CREDITED

    # 100 is the legacy welcome credit the fresh row carries
    # (``EconomyUser`` docstring, "Legacy default balance for fresh rows
    # is 100"), so the payer lands where any first-time user lands plus
    # what they paid for — not on a bespoke zero-balance row.
    row = await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == 999))
    assert row.scalar_one() == 100 + 50
    assert await _processed_count(session) == 1
    assert await _ledger_count(session) == 1


async def test_a_credit_over_the_ceiling_is_still_refused(
    service: PaymentsService, session: AsyncSession
) -> None:
    """The one refusal #770 leaves standing, and the reason for the rename.

    ``ensure_wallet`` removes "no such wallet" as a cause, so the only
    way ``credit`` still answers ``None`` for a valid amount is the
    ``balance + amount <= _MAX_AMOUNT`` guard in SQL. The outcome must
    stay terminal AND must not write an idempotency row — a tombstone
    here would swallow the retry that fixes it.
    """
    await _seed(session, user_id=1, balance=_MAX_AMOUNT)

    outcome = await service.handle_event(_event(coins=50))

    assert outcome is CreditOutcome.CREDIT_REFUSED
    assert await _processed_count(session) == 0
    assert await _ledger_count(session) == 0


async def test_invalid_amount_returns_invalid_amount(
    service: PaymentsService, session: AsyncSession
) -> None:
    await _seed(session, user_id=1, balance=100)
    outcome = await service.handle_event(_event(coins=0))
    # validate_credit_amount(0) is False — service short-circuits.
    assert outcome is CreditOutcome.INVALID_AMOUNT
    row = await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == 1))
    assert row.scalar_one() == 100
    assert await _processed_count(session) == 0
    assert await _ledger_count(session) == 0


async def test_same_external_id_different_provider_is_not_duplicate(
    service: PaymentsService, session: AsyncSession
) -> None:
    """Composite-PK guarantee: provider scopes external_id uniqueness."""
    await _seed(session, user_id=1, balance=100)
    o1 = await service.handle_event(
        _event(provider=Provider.CRYPTO, external_id="SHARED", coins=10)
    )
    o2 = await service.handle_event(
        _event(provider=Provider.STRIPE, external_id="SHARED", coins=20)
    )
    o3 = await service.handle_event(
        _event(provider=Provider.YOOKASSA, external_id="SHARED", coins=30)
    )
    assert (o1, o2, o3) == (
        CreditOutcome.CREDITED,
        CreditOutcome.CREDITED,
        CreditOutcome.CREDITED,
    )
    row = await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == 1))
    assert row.scalar_one() == 160
    assert await _processed_count(session) == 3
    assert await _ledger_count(session) == 3


class _StaleIdempotencyGate(ProcessedWebhooksRepo):
    """A repo whose pre-check answers from *before* the winner committed.

    The engine promotes a session to ``BEGIN IMMEDIATE`` on its first
    WRITE, not at ``begin()`` (``db/engines.py``), so
    :meth:`ProcessedWebhooksRepo.is_processed` runs before the write
    lock is taken. Two simultaneous redeliveries of one payment can
    therefore both read "not processed" — the loser's SELECT lands
    before the winner's COMMIT. Overriding it to a flat ``False``
    reproduces exactly that interleaving, deterministically, without
    threads.
    """

    async def is_processed(self, *, provider: str, external_id: str) -> bool:
        return False


async def test_race_loser_is_stopped_by_the_primary_key_not_the_precheck(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A duplicate that slips past the pre-check must not credit twice.

    This is the guarantee the ``PRIMARY KEY (provider, external_id)``
    on ``processed_webhooks`` exists for, and it is worth a test
    because nothing in the Python path enforces it: the pre-check can
    race, and if the INSERT were ever softened to an upsert or an
    ``ON CONFLICT DO NOTHING``, every assertion in this file would
    still pass while a redelivered payment paid out twice.

    The loser raises ``IntegrityError`` out of the service, which
    propagates through the caller's ``async with session.begin()`` and
    takes its half-finished credit down with it — so the wallet ends
    up exactly where the winner left it.
    """
    async with maker() as winner_session:
        await _seed(winner_session, user_id=1, balance=100)
        async with winner_session.begin():
            winner = PaymentsService(
                economy=EconomyService(
                    EconomyRepo(winner_session), TransactionsRepo(winner_session)
                ),
                idempotency=ProcessedWebhooksRepo(winner_session),
                bot=None,
            )
            assert await winner.handle_event(_event(coins=50)) is CreditOutcome.CREDITED

    async with maker() as loser_session:
        loser = PaymentsService(
            economy=EconomyService(EconomyRepo(loser_session), TransactionsRepo(loser_session)),
            idempotency=_StaleIdempotencyGate(loser_session),
            bot=None,
        )
        with pytest.raises(IntegrityError):
            async with loser_session.begin():
                await loser.handle_event(_event(coins=50))

    async with maker() as reader:
        row = await reader.execute(select(EconomyUser.balance).where(EconomyUser.user_id == 1))
        assert row.scalar_one() == 150  # credited once, not twice
        assert await _processed_count(reader) == 1
        assert await _ledger_count(reader) == 1


async def test_ledger_row_attribution_is_to_id_user_no_from(
    service: PaymentsService, session: AsyncSession
) -> None:
    """Payment credits set to_id=user_id, from_id=None — no counter-party."""
    await _seed(session, user_id=1, balance=0)
    outcome = await service.handle_event(_event(user_id=1, coins=42))
    assert outcome is CreditOutcome.CREDITED
    row = await session.execute(select(Transaction))
    tx = row.scalars().one()
    assert tx.to_id == 1
    assert tx.from_id is None
    assert tx.amount == 42
    assert tx.type == "purchase_crypto"


async def test_mark_processed_persists_metadata(
    service: PaymentsService, session: AsyncSession
) -> None:
    """The processed_webhooks row records the credited_amount and processed_at."""
    await _seed(session, user_id=1, balance=0)
    before = datetime.now(UTC).replace(tzinfo=None)
    await service.handle_event(_event(coins=123))
    after = datetime.now(UTC).replace(tzinfo=None)
    row = await session.execute(select(ProcessedWebhook))
    pw = row.scalars().one()
    assert pw.provider == "crypto"
    assert pw.external_id == "EXT-1"
    assert pw.user_id == 1
    assert pw.credited_amount == 123
    assert before <= pw.processed_at <= after


async def test_mark_processed_persists_the_fiat_charge(
    service: PaymentsService, session: AsyncSession
) -> None:
    """#239: the money the payer actually handed over is recorded too.

    The three columns are TEXT and the assertions are on the *string*
    form on purpose. A float round-trip would turn ``1000.00`` into
    ``1000.0`` and ``81.2345`` into something ending in ``...44999``;
    either would still compare equal numerically and would still be
    wrong on the day someone reconciles the row against RollyPay's
    dashboard. Exact strings are the whole point of the ticket.
    """
    await _seed(session, user_id=1, balance=0)
    await service.handle_event(
        _event(
            coins=123,
            fiat_amount=Decimal("1000.00"),
            fiat_currency="RUB",
            fx_rate=Decimal("81.2345"),
        )
    )
    row = await session.execute(select(ProcessedWebhook))
    pw = row.scalars().one()
    assert pw.fiat_amount == "1000.00"
    assert pw.fiat_currency == "RUB"
    assert pw.fx_rate == "81.2345"


async def test_mark_processed_leaves_the_fiat_columns_null_when_unknown(
    service: PaymentsService, session: AsyncSession
) -> None:
    """An event with no charge attached writes NULL, not ``"None"``.

    Stars top-ups and any future provider that omits the figure land
    here. ``NULL`` reads as "not recorded"; the string ``"None"`` would
    read as a value and would poison every aggregate over the column.
    """
    await _seed(session, user_id=1, balance=0)
    await service.handle_event(_event(coins=123))
    row = await session.execute(select(ProcessedWebhook))
    pw = row.scalars().one()
    assert pw.fiat_amount is None
    assert pw.fiat_currency is None
    assert pw.fx_rate is None


# ---------------------------------------------------------------------------
# R15 — a failing commission must not take the paid top-up down with it
# ---------------------------------------------------------------------------
#
# ``handle_event`` wraps ``apply_purchase_commissions`` in a blanket
# ``except Exception`` whose comment promises "top-up credit unaffected".
# For a *DB-level* failure that promise is false: the failed flush
# deactivates the enclosing transaction, so when the caller's
# ``async with session.begin():`` block exits it rolls back — silently,
# with no exception — and the credit, the ledger row and the
# processed_webhooks row all vanish. The service still returns
# ``CREDITED``, the router still answers 200, and the provider never
# retries. The customer paid and got nothing.
#
# These tests reproduce the caller's transaction shape exactly, because
# that is where the damage happens: driving ``handle_event`` on a bare
# autobegin session and calling ``commit()`` afterwards does NOT expose
# it the same way.


class _ExplodingCommission:
    """Stands in for a ``ReferralCommissionService`` that hits the DB and fails.

    Duplicating a PK is the cheapest *genuine* DB-level failure: it goes
    through the same flush machinery a real commission bug would (a
    UNIQUE violation on a ledger row, a NOT NULL on a new column, a
    stale FK), rather than faking one with a hand-raised exception that
    never touched the connection.
    """

    def __init__(self, session: AsyncSession, clash_user_id: int) -> None:
        self._session = session
        self._clash_user_id = clash_user_id
        self.calls = 0

    async def apply_purchase_commissions(self, *, buyer_id: int, coins_purchased: int) -> object:
        self.calls += 1
        self._session.add(EconomyUser(user_id=self._clash_user_id, balance=0, language="ru"))
        await self._session.flush()  # IntegrityError: duplicate PK
        raise AssertionError("unreachable — the flush above must raise")


class _RaisingCommission:
    """A commission that fails *without* touching the DB."""

    def __init__(self) -> None:
        self.calls = 0

    async def apply_purchase_commissions(self, *, buyer_id: int, coins_purchased: int) -> object:
        self.calls += 1
        raise RuntimeError("kickback maths blew up")


def _service_with_commission(session: AsyncSession, commission: object) -> PaymentsService:
    return PaymentsService(
        economy=EconomyService(EconomyRepo(session), TransactionsRepo(session)),
        idempotency=ProcessedWebhooksRepo(session),
        bot=None,
        referral_commission=commission,  # type: ignore[arg-type]
        session=session,
    )


async def test_db_level_commission_failure_leaves_the_topup_credited(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """R15: the buyer keeps the coins they paid for, even if the kickback dies.

    A SAVEPOINT around the commission is what makes the blanket
    ``except`` honest — without it the rollback is total and silent.
    """
    async with maker() as s, s.begin():
        s.add(EconomyUser(user_id=1, balance=100, language="ru"))

    async with maker() as s, s.begin():
        commission = _ExplodingCommission(s, clash_user_id=1)
        service = _service_with_commission(s, commission)
        outcome = await service.handle_event(_event(user_id=1, coins=50))

    assert outcome is CreditOutcome.CREDITED
    assert commission.calls == 1

    # Read through a *fresh* session: anything still visible here
    # genuinely committed.
    async with maker() as s:
        balance = (
            await s.execute(select(EconomyUser.balance).where(EconomyUser.user_id == 1))
        ).scalar_one()
        assert balance == 150, (
            "the top-up was rolled back by a failing commission — the customer "
            "paid and received nothing, while the service reported CREDITED"
        )
        # The idempotency row must survive too. If it does not, a retry
        # would re-credit — but the router already answered 200, so no
        # retry is coming and the money is simply gone.
        assert await _processed_count(s) == 1
        assert await _ledger_count(s) == 1


async def test_non_db_commission_failure_leaves_the_topup_credited(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The same guarantee for a plain bug (no DB involvement).

    This half already held before R15; pinning it makes sure the
    SAVEPOINT did not narrow the blanket ``except`` to DB errors only.
    """
    async with maker() as s, s.begin():
        s.add(EconomyUser(user_id=1, balance=100, language="ru"))

    async with maker() as s, s.begin():
        commission = _RaisingCommission()
        service = _service_with_commission(s, commission)
        outcome = await service.handle_event(_event(user_id=1, coins=50))

    assert outcome is CreditOutcome.CREDITED
    assert commission.calls == 1
    async with maker() as s:
        balance = (
            await s.execute(select(EconomyUser.balance).where(EconomyUser.user_id == 1))
        ).scalar_one()
        assert balance == 150
        assert await _processed_count(s) == 1


async def test_commission_without_a_session_is_a_construction_error(
    session: AsyncSession,
) -> None:
    """The SAVEPOINT is not optional where the blanket ``except`` is.

    Wiring a commission without the session it must be scoped to is the
    exact mistake R15 fixes, so it fails loudly at construction instead
    of quietly at the next DB hiccup.
    """
    with pytest.raises(ValueError, match="session"):
        PaymentsService(
            economy=EconomyService(EconomyRepo(session), TransactionsRepo(session)),
            idempotency=ProcessedWebhooksRepo(session),
            bot=None,
            referral_commission=_RaisingCommission(),  # type: ignore[arg-type]
        )
