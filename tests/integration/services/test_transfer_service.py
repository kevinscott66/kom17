"""``TransferService.send`` — tax-aware /send composed end-to-end.

The pure tax math is pinned in ``tests/unit/utils/test_transfer.py``;
the EconomyService / EconomyRepo primitives are pinned in their
own tests. These tests focus on the *flow*: validation order,
multi-step atomicity, ledger row shape, admin-destination routing,
race-safe SQL guards.

Outcome taxonomy is exhaustive — every :class:`TransferOutcome`
member has at least one test pinning the trigger condition. Pins
prevent a future "simpler" refactor that collapses two outcomes
into one from quietly changing the UX.
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
from telegram_invite_bot.services.effects_service import TransferEffects
from telegram_invite_bot.services.transfer_service import (
    TransferConfig,
    TransferOutcome,
    TransferService,
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _build_service(
    session: AsyncSession,
    *,
    base_rate: float = 0.05,
    admin_user_id: int | None = None,
) -> TransferService:
    return TransferService(
        EconomyRepo(session),
        TransactionsRepo(session),
        config=TransferConfig(base_tax_rate=base_rate, admin_user_id=admin_user_id),
    )


async def _seed_wallet(session: AsyncSession, user_id: int, balance: int = 1_000) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


async def _balance(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        select(EconomyUser.balance).where(EconomyUser.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    return int(row) if row is not None else -1


async def _ledger_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Transaction))
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Validation short-circuits (no DB I/O)
# ---------------------------------------------------------------------------


async def test_self_transfer_rejected_before_db_read(session: AsyncSession) -> None:
    """Pinned because self-transfer is a typo-class error the user
    can fix immediately; surfacing it as a distinct outcome lets
    handler UX say "you can't gift yourself" instead of a generic
    "invalid amount" that hides the real problem."""
    service = _build_service(session)
    result = await service.send(from_id=42, to_id=42, amount=100)
    assert result.outcome is TransferOutcome.SELF_TRANSFER
    # No wallets seeded → if the validator wasn't first, we'd get
    # NO_SENDER_WALLET instead. The exact outcome here pins the
    # validation order.
    assert await _ledger_count(session) == 0


async def test_zero_amount_rejected(session: AsyncSession) -> None:
    service = _build_service(session)
    result = await service.send(from_id=42, to_id=99, amount=0)
    assert result.outcome is TransferOutcome.INVALID_AMOUNT


async def test_negative_amount_rejected(session: AsyncSession) -> None:
    """Defensive — handler input parser rejects, but a direct
    service call must not silently credit the sender."""
    service = _build_service(session)
    result = await service.send(from_id=42, to_id=99, amount=-100)
    assert result.outcome is TransferOutcome.INVALID_AMOUNT


# ---------------------------------------------------------------------------
# Wallet existence
# ---------------------------------------------------------------------------


async def test_missing_sender_wallet(session: AsyncSession) -> None:
    """Recipient exists but sender doesn't → NO_SENDER_WALLET, no
    mutation. Pinned to ensure the recipient read doesn't fire
    BEFORE the sender read (waste of a round-trip)."""
    await _seed_wallet(session, 99)
    service = _build_service(session)
    result = await service.send(from_id=42, to_id=99, amount=100)
    assert result.outcome is TransferOutcome.NO_SENDER_WALLET
    assert await _balance(session, 99) == 1_000  # untouched


async def test_missing_recipient_wallet(session: AsyncSession) -> None:
    """Sender exists, recipient doesn't → NO_RECIPIENT_WALLET, NO
    sender mutation. The legacy flow at bot.py:10279 fails here
    too; pinning means a future "auto-create recipient on transfer"
    feature has to update this test intentionally rather than
    silently materialising wallets for arbitrary user IDs (which
    would enable a probe-then-credit attack on the wallet table)."""
    await _seed_wallet(session, 42, balance=500)
    service = _build_service(session)
    result = await service.send(from_id=42, to_id=99, amount=100)
    assert result.outcome is TransferOutcome.NO_RECIPIENT_WALLET
    assert await _balance(session, 42) == 500  # sender untouched


async def test_insufficient_funds_at_pre_check(session: AsyncSession) -> None:
    await _seed_wallet(session, 42, balance=50)
    await _seed_wallet(session, 99)
    service = _build_service(session)
    result = await service.send(from_id=42, to_id=99, amount=100)
    assert result.outcome is TransferOutcome.INSUFFICIENT_FUNDS
    assert await _balance(session, 42) == 50  # no mutation


# ---------------------------------------------------------------------------
# Success path — no tax (free transfer between two users)
# ---------------------------------------------------------------------------


async def test_zero_rate_transfer_yields_no_tax_no_admin_credit(
    session: AsyncSession,
) -> None:
    """Operator can disable transfer tax entirely by setting
    ``base_tax_rate=0``. Pin: net == amount, admin row untouched,
    only one ledger row (no tax row). Catches a future refactor
    that always writes the tax row even when tax is 0 — would
    inflate the ``type='tax'`` audit query with empty rows."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.0)
    result = await service.send(from_id=42, to_id=99, amount=100)

    assert result.outcome is TransferOutcome.SUCCESS
    assert result.amount == 100
    assert result.received == 100
    assert result.tax == 0
    assert result.sender_balance == 400
    assert await _balance(session, 42) == 400
    assert await _balance(session, 99) == 1_100
    assert await _ledger_count(session) == 1


# ---------------------------------------------------------------------------
# Success path — tax burned (no admin destination configured)
# ---------------------------------------------------------------------------


async def test_tax_burned_when_admin_id_none(session: AsyncSession) -> None:
    """``admin_user_id=None`` ⇒ tax is removed from the sender but
    NEVER credited anywhere — coins disappear from circulation.
    Useful posture for tests AND for a future "deflationary mode"
    feature flag. Pinned because the math otherwise looks broken
    (sender -100, recipient +95, +5 unaccounted) — the burn is the
    *point*, not a bug."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05, admin_user_id=None)
    result = await service.send(from_id=42, to_id=99, amount=100)

    assert result.outcome is TransferOutcome.SUCCESS
    assert result.amount == 100
    assert result.received == 95
    assert result.tax == 5
    assert await _balance(session, 42) == 400  # -100 gross
    assert await _balance(session, 99) == 1_095  # +95 net
    # Two ledger rows: main transfer + tax (even though no admin
    # credit happened). Treasury audit query keys on type='tax'.
    assert await _ledger_count(session) == 2


# ---------------------------------------------------------------------------
# Success path — tax routed to admin wallet
# ---------------------------------------------------------------------------


async def test_missing_admin_wallet_auto_seeds_and_receives_tax(
    session: AsyncSession,
) -> None:
    """M-E-2: a configured-but-missing admin wallet must NOT silently
    burn the tax.

    Pre-fix behaviour (audit 01_economy.md M-E-2): if the operator
    sets ``TransferConfig.admin_user_id=1`` but never seeds the
    matching ``economy.users`` row, ``EconomyRepo.credit`` returns
    ``None`` for the missing wallet and the tax cut evaporates —
    sender debited gross, recipient credited net, the difference
    goes nowhere. The ``type='tax'`` ledger row is still written, so
    the books *look* balanced while the treasury wallet never
    receives the coins. In production this can drain tens of
    thousands of coins of revenue before anyone notices.

    Post-fix: the service ``get_or_create``'s the admin wallet
    idempotently before the credit, so a missing admin row
    self-heals. The wallet is created at the legacy default balance
    (100, per ``EconomyRepo._LEGACY_DEFAULT_BALANCE``) and the tax
    lands on top of it.
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    # Note: admin user id 1 is NOT seeded.
    service = _build_service(session, base_rate=0.05, admin_user_id=1)
    result = await service.send(from_id=42, to_id=99, amount=100)

    assert result.outcome is TransferOutcome.SUCCESS
    assert result.tax == 5
    assert await _balance(session, 42) == 400
    assert await _balance(session, 99) == 1_095
    # Admin wallet was auto-created at 100 default, then credited 5
    # tax → 105. Conservation check: the 5 coins of tax that were
    # being lost pre-fix now show up exactly here.
    assert await _balance(session, 1) == 105
    # Two ledger rows (main transfer + tax) as before.
    assert await _ledger_count(session) == 2


async def test_tax_credited_to_admin_wallet(session: AsyncSession) -> None:
    """Conservation: sender -gross == recipient +net + admin +tax.
    The handler-rendered receipt shows the gross debit and net
    credit; this test pins that the admin treasury actually receives
    the cut (the bot.py:10269 line that's quiet in legacy because
    ADMIN_CHAT_ID is global). A future refactor that drops the
    admin credit would leak money out of circulation silently."""
    await _seed_wallet(session, 1, balance=10_000)  # admin
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05, admin_user_id=1)
    result = await service.send(from_id=42, to_id=99, amount=100)

    assert result.outcome is TransferOutcome.SUCCESS
    assert result.tax == 5
    assert await _balance(session, 42) == 400  # -100
    assert await _balance(session, 99) == 1_095  # +95
    assert await _balance(session, 1) == 10_005  # +5 tax
    # Conservation:
    assert (500 - 400) == (1_095 - 1_000) + (10_005 - 10_000)
    assert await _ledger_count(session) == 2


# ---------------------------------------------------------------------------
# VIP discount path — TransferEffects integration
# ---------------------------------------------------------------------------


async def test_vip_discount_halves_tax(session: AsyncSession) -> None:
    """End-to-end VIP read (already covered at the effects/repo
    layer) propagating into the actual debit numbers. Pinned
    alongside the 50% legacy constant: a tiering PR that bumps VIP
    to 60% needs to update this test intentionally — otherwise
    user-visible tax silently shifts."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.10)  # 10% base
    effects = TransferEffects(tax_discount_percent=50)  # halved → 5%
    result = await service.send(from_id=42, to_id=99, amount=100, effects=effects)

    assert result.outcome is TransferOutcome.SUCCESS
    assert result.tax == 5  # 10% * (1 - 0.5) * 100
    assert result.received == 95


async def test_full_vip_discount_yields_tax_free_transfer(
    session: AsyncSession,
) -> None:
    """100% discount → tax=0 → ONE ledger row (the tax-row write
    skips when tax is 0). Pins the short-circuit so the audit
    table doesn't accumulate type='tax' rows with amount=0 for
    every founder/promo transfer."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05, admin_user_id=1)
    effects = TransferEffects(tax_discount_percent=100)
    result = await service.send(from_id=42, to_id=99, amount=100, effects=effects)

    assert result.outcome is TransferOutcome.SUCCESS
    assert result.tax == 0
    assert result.received == 100
    assert await _ledger_count(session) == 1


# ---------------------------------------------------------------------------
# Ledger row shape — the audit-table promise
# ---------------------------------------------------------------------------


async def test_main_ledger_row_records_net_amount(session: AsyncSession) -> None:
    """Main row stores the NET amount — the coins that actually
    reached the recipient's wallet — matching legacy, which inserts
    ``final_amount`` (bot.py:10283), the same variable it credits at
    bot.py:10265.

    This used to store the gross, justified as "the receipt reuses
    the row". It does not: ``send.handle_send`` renders the receipt from
    :class:`TransferResult`, never from the ledger. The gross made
    the recipient's history claim income they never received (#256).
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05)
    await service.send(from_id=42, to_id=99, amount=100)

    result = await session.execute(select(Transaction).where(Transaction.type == "transfer"))
    transfer_row = result.scalar_one()
    assert transfer_row.amount == 95  # net, not the 100 the sender typed
    assert transfer_row.from_id == 42
    assert transfer_row.to_id == 99


async def test_ledger_rows_read_back_as_conservation(session: AsyncSession) -> None:
    """The pair of rows must balance when read through the reader the
    finances panel actually uses (#256).

    ``TransactionsRepo.recent`` derives the sign from ``to_id ==
    user_id`` and takes the magnitude as given
    (transactions_repo.py:224). So the sender's two rows have to sum
    to exactly what left their wallet (-100) and the recipient's one
    row to exactly what arrived (+95). With the gross in the main row
    the sender summed to -105 — the tax counted twice — and the
    recipient read +100 against a wallet that grew by 95.
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05, admin_user_id=7)
    await _seed_wallet(session, 7, balance=0)
    await service.send(from_id=42, to_id=99, amount=100)

    ledger = TransactionsRepo(session)
    sender = [tx.signed_amount for tx in await ledger.recent(42, limit=10)]
    assert sorted(sender) == [-95, -5]
    assert sum(sender) == -100

    recipient = [tx.signed_amount for tx in await ledger.recent(99, limit=10)]
    assert recipient == [95]

    treasury = [tx.signed_amount for tx in await ledger.recent(7, limit=10)]
    assert treasury == [5]


async def test_recipient_credit_failure_reverts_sender_debit(
    session: AsyncSession,
) -> None:
    """R-FIX-002: a post-debit credit failure must NOT commit the
    sender's debit. Earlier the service returned ``NO_RECIPIENT_WALLET``
    normally after the debit landed; ``BaseSessionMiddleware`` only
    rolls back on raised exceptions, so the sender lost coins to the
    void. Fix uses a SAVEPOINT around the debit/credit pair so a
    credit miss undoes the debit atomically.

    Repro: seed sender + recipient, then monkey-patch
    ``EconomyRepo.credit`` to return ``None`` on the *recipient* call
    (simulating an admin /reset between step 3's existence check and
    step 6's credit). After the call, sender balance is unchanged.
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05)

    real_credit = service._economy.credit  # noqa: SLF001

    async def fake_credit(user_id: int, amount: int):  # type: ignore[no-untyped-def]
        # Only fail the recipient credit; an admin-tax credit (if any)
        # would also flow through here, but the SAVEPOINT-revert path
        # we're pinning fires on the recipient miss first.
        if user_id == 99:
            return None
        return await real_credit(user_id, amount)

    service._economy.credit = fake_credit  # type: ignore[method-assign]  # noqa: SLF001

    result = await service.send(from_id=42, to_id=99, amount=100)

    assert result.outcome is TransferOutcome.NO_RECIPIENT_WALLET
    # Critical assertion: sender's balance is unchanged. Before the
    # fix this was 400 (committed debit).
    assert await _balance(session, 42) == 500
    # Recipient untouched (the fake_credit returned None without
    # writing). Before the fix this was also 1000.
    assert await _balance(session, 99) == 1_000
    # No ledger rows — the savepoint also rolls back any in-flight
    # writes, and the service only writes ledger rows on the success
    # path (step 8) anyway.
    assert await _ledger_count(session) == 0


async def test_transfer_uses_savepoint_for_atomicity(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-FIX-002-fp: pin that the service actually calls
    ``session.begin_nested()`` on the happy path. The R-FIX-002 fix
    relies on SQLAlchemy 2.x autobegin so the nested call lands a real
    SAVEPOINT; if a refactor were to drop the ``async with
    session.begin_nested()`` wrapper (or replace it with a plain
    ``async with session.begin()`` against an already-begun session,
    which would raise), the credit-failure revert behaviour would
    silently regress and only the dedicated revert test (which
    monkey-patches the credit method) would catch it. This test makes
    the SAVEPOINT call itself observable so a missing wrapper trips
    immediately."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05)

    real_begin_nested = session.begin_nested
    calls: list[None] = []

    def spy_begin_nested():  # type: ignore[no-untyped-def]
        calls.append(None)
        return real_begin_nested()

    monkeypatch.setattr(session, "begin_nested", spy_begin_nested)

    result = await service.send(from_id=42, to_id=99, amount=100)
    assert result.outcome is TransferOutcome.SUCCESS
    assert len(calls) == 1


async def test_tax_ledger_row_routed_to_admin_id(session: AsyncSession) -> None:
    """Tax row's ``to_id`` matches the configured admin so the
    treasury query (`type='tax' AND to_id=admin`) reads cleanly.
    A future "tax goes to per-chat treasury" feature would change
    this; pinning means it has to update the test deliberately."""
    await _seed_wallet(session, 1, balance=10_000)
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05, admin_user_id=1)
    await service.send(from_id=42, to_id=99, amount=100)

    result = await session.execute(select(Transaction).where(Transaction.type == "tax"))
    tax_row = result.scalar_one()
    assert tax_row.amount == 5
    assert tax_row.from_id == 42
    assert tax_row.to_id == 1


async def test_tax_moves_the_treasury_balance_without_its_lifetime_counters(
    session: AsyncSession,
) -> None:
    """#1521: tax is collected, not earned.

    Legacy's tax leg is a bare ``UPDATE users SET balance =
    balance + ?`` (bot.py:10268-10273) while its sender and
    recipient legs do move the lifetime counters — so the
    divergence is specific to this leg, not a general one. Routing
    the tax through ``credit`` bumped ``total_earned`` too, and the
    drift was one-way: ``bump_totals`` refuses negative arguments,
    so an inflated counter could never be walked back. Every future
    reading of ``total_earned`` as "coins earned in the game" would
    have been wrong by the whole tax stream.
    """
    await _seed_wallet(session, 1, balance=10_000)  # treasury
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99)
    service = _build_service(session, base_rate=0.05, admin_user_id=1)

    result = await service.send(from_id=42, to_id=99, amount=100)
    assert result.outcome is TransferOutcome.SUCCESS
    assert result.tax == 5

    row = await session.execute(select(EconomyUser).where(EconomyUser.user_id == 1))
    treasury = row.scalar_one()
    assert treasury.balance == 10_005
    assert treasury.total_earned == 0
    assert treasury.total_spent == 0


async def test_uncredited_tax_is_booked_as_burned_not_as_treasury_income(
    session: AsyncSession,
) -> None:
    """A treasury at the balance ceiling can't receive the tax.

    The coins still leave the sender, so the row must exist — but
    naming the treasury would inflate the ``type='tax' AND to_id=admin``
    audit sum with income the wallet never got. It's booked as a burn.
    """
    from telegram_invite_bot.utils.economy import _MAX_AMOUNT  # noqa: PLC0415

    await _seed_wallet(session, 1, balance=_MAX_AMOUNT)  # treasury, full
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=0)
    service = _build_service(session, base_rate=0.05, admin_user_id=1)

    result = await service.send(from_id=42, to_id=99, amount=100)
    await session.commit()

    # The transfer itself still goes through — only the tax leg failed.
    assert result.outcome is TransferOutcome.SUCCESS
    assert await _balance(session, 42) == 400
    assert await _balance(session, 99) == 95
    assert await _balance(session, 1) == _MAX_AMOUNT

    tax_row = (
        await session.execute(select(Transaction).where(Transaction.type == "tax"))
    ).scalar_one()
    assert tax_row.amount == 5
    assert tax_row.from_id == 42
    assert tax_row.to_id is None  # burned, NOT credited to the treasury
    assert tax_row.reason == "transfer_tax_burned"
