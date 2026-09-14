"""Idempotency repository for payment-webhook deliveries (T-025).

One ``ProcessedWebhook`` row per (provider, external_id) we have
already credited. Stripe / YooKassa / Crypto Pay all retry deliveries
on missed acknowledgements; the row is the gate that turns a second
delivery into a no-op (caller returns 200 + does NOT credit again).

Why a dedicated repo over inlining the two SQL statements in the
service: keeping the table access behind a typed surface lets the
service-level test mock the idempotency without spinning up SQLite,
and the legacy code's own idempotency call site lives in
``bot.is_payment_transaction_processed`` — having a parallel new-side
repo with the same purpose makes the "what owns idempotency" question
answerable by grep.

Every method is kw-only and returns primitive types or a frozen
snapshot — the service doesn't need the ORM row, only "did we credit
this already" and "record that we did". The ORM instance stays inside
the repo so a caller cannot accidentally read a detached attribute
after its session closed.

#174 adds the reversal half: :meth:`get` (who did we credit for this
payment id) and :meth:`mark_reversed` (the provider took it back).
Both are called from the audit path, not the credit hot path — but
since #226 what :meth:`mark_reversed` writes can be a row for a credit
that never happened, and the gate above will honour it. See
:attr:`CreditRecord.is_tombstone`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import false, select, update

from telegram_invite_bot.db.models.economy import ProcessedWebhook

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class CreditRecord:
    """A snapshot of one credited webhook, safe to read after commit.

    Detached from the session on purpose: :meth:`ProcessedWebhooksRepo.get`
    is called from the reversal alert path, which reads these fields
    while composing a Telegram message long after the session that
    produced them is gone.
    """

    provider: str
    external_id: str
    user_id: int
    credited_amount: int
    processed_at: datetime
    reversed_at: datetime | None
    reversed_event: str | None

    @property
    def is_tombstone(self) -> bool:
        """``True`` iff this row records a reversal that never credited.

        #226: a refund can reach us before the payment it cancels —
        providers retry a ``paid`` callback for the best part of an
        hour, and a refund issued inside that hour overtakes the
        retry. :meth:`mark_reversed` then writes a row for a credit
        that does not exist, so the late ``paid`` finds the
        idempotency gate already shut instead of minting coins for
        money the merchant has since sent back.

        ``credited_amount == 0`` is a sound discriminator because a
        real credit can never be zero:
        :func:`telegram_invite_bot.utils.economy.validate_credit_amount`
        requires ``0 < amount``, and ``EconomyService`` refuses the
        credit long before :meth:`mark_processed` is reached.
        """
        return self.credited_amount == 0


class ProcessedWebhooksRepo:
    """``economy.processed_webhooks`` reader + appender."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_processed(self, *, provider: str, external_id: str) -> bool:
        """Return ``True`` iff a row for ``(provider, external_id)`` exists.

        The SELECT is a primary-key lookup so it's a single B-tree
        descent — cheap relative to the wallet UPDATE the caller is
        about to issue.

        It is a fast pre-check, NOT the guard, and the distinction
        matters because the thing on the other side of it is money.
        The engine promotes a session to ``BEGIN IMMEDIATE`` lazily,
        on its first WRITE (see ``db/engines.py``), so this read runs
        before the write lock is taken and two simultaneous
        redeliveries of one payment can both see "not processed".
        What stops the second one from crediting twice is the
        ``PRIMARY KEY (provider, external_id)`` on the table: the
        loser's :meth:`mark_processed` INSERT raises
        :class:`~sqlalchemy.exc.IntegrityError`, which propagates out
        of the service's ``async with session.begin()`` and rolls its
        credit back with it. Same shape as
        ``ChecksRepo.has_claimed`` — friendly probe in front, database
        constraint behind.
        """
        stmt = select(ProcessedWebhook.provider).where(
            ProcessedWebhook.provider == provider,
            ProcessedWebhook.external_id == external_id,
        )
        result = await self._session.execute(stmt)
        return result.first() is not None

    async def mark_processed(
        self,
        *,
        provider: str,
        external_id: str,
        user_id: int,
        credited_amount: int,
        processed_at: datetime | None = None,
        fiat_amount: Decimal | None = None,
        fiat_currency: str | None = None,
        fx_rate: Decimal | None = None,
    ) -> None:
        """Insert the idempotency row.

        Flush (not commit) so the row participates in the outer
        transaction the service is composing — if the wallet credit
        UPDATE that follows fails, the rollback clears this row too
        and the next retry will reprocess cleanly. Without that
        coupling, a half-written credit (row recorded but wallet not
        updated) would silently swallow the user's payment.

        ``processed_at`` defaults to UTC-now; tests inject a fixed
        value for determinism.

        The three ``fiat_*`` arguments (#239) are the audit trail and
        default to ``None`` for every caller that has nothing to
        record. They are stringified here rather than at the call
        site: the columns are TEXT precisely so a ``Decimal`` reaches
        SQLite without passing through a float, and ``str(Decimal)``
        is the exact, lossless spelling of that value. Doing the
        conversion in one place also means the adapters keep handing
        this repository the money type rather than pre-formatted text
        they might each format differently.
        """
        row = ProcessedWebhook(
            provider=provider,
            external_id=external_id,
            user_id=user_id,
            credited_amount=credited_amount,
            processed_at=processed_at or datetime.now(UTC).replace(tzinfo=None),
            fiat_amount=None if fiat_amount is None else str(fiat_amount),
            fiat_currency=fiat_currency,
            fx_rate=None if fx_rate is None else str(fx_rate),
        )
        self._session.add(row)
        await self._session.flush()

    async def get(self, *, provider: str, external_id: str) -> CreditRecord | None:
        """Return the credit we recorded for this payment, if any (#174).

        A primary-key read like :meth:`is_processed`, but it hands back
        the whole row: the reversal path needs the user and the coins
        we minted, not just the yes/no.

        ``None`` is an ordinary answer, not an error. It means one of
        two honest things — the provider is reversing a payment this
        bot never credited (a checkout abandoned then refunded by the
        merchant), or the provider keys its reversal by a different id
        than its success (Stripe credits by ``session_id`` and reverses
        by ``payment_intent``). The caller must say which it cannot
        tell rather than invent a user.
        """
        stmt = select(ProcessedWebhook).where(
            ProcessedWebhook.provider == provider,
            ProcessedWebhook.external_id == external_id,
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return CreditRecord(
            provider=row.provider,
            external_id=row.external_id,
            user_id=row.user_id,
            credited_amount=row.credited_amount,
            processed_at=row.processed_at,
            reversed_at=row.reversed_at,
            reversed_event=row.reversed_event,
        )

    async def lock_writer(self) -> None:
        """Take this database's writer lock before the reversal read.

        #1440. ``db/engines.py`` opens transactions lazily: it issues
        ``BEGIN IMMEDIATE`` only when it sees a *write*-headed
        statement, and ``"select"`` is not one. So the SELECT that
        decides whether a credit exists runs outside any transaction,
        and a ``paid`` delivery for the same identifier can commit its
        :meth:`mark_processed` row in the window between that read and
        the tombstone INSERT that follows. The INSERT then collides
        with the composite primary key, the caller's broad ``except``
        swallows the ``IntegrityError``, and ``reversed_at`` is never
        written — so the payout desk's chargeback marker never lights
        and ``TransactionsRepo.lifetime_deposits`` keeps counting a
        reversed top-up as a real deposit in the withdrawal gates.

        The remedy is the one
        :meth:`repositories.withdrawals_repo.WithdrawalsRepo.lock_writer`
        already uses for the quota reads: make the first statement of
        the critical section a write, so the lock is held across the
        read that decides the outcome. This UPDATE matches nothing —
        ``WHERE false`` — but its ``UPDATE`` head is what the engine
        listener keys on, so a second writer blocks on
        ``PRAGMA busy_timeout`` and then reads committed state.

        Writing no rows is the point: the lock is taken before anyone
        knows whether a stamp will happen at all, and a lookup that
        stamps nothing must be able to release it having changed
        nothing.
        """
        await self._session.execute(
            update(ProcessedWebhook)
            .where(false())
            .values(user_id=ProcessedWebhook.user_id)
            .execution_options(synchronize_session=False)
        )

    async def mark_reversed(
        self,
        *,
        provider: str,
        external_id: str,
        event: str,
        reversed_at: datetime | None = None,
        tombstone: bool = False,
    ) -> bool:
        """Stamp a credited row as taken back. ``True`` iff a row is stamped.

        No wallet is touched — the debit is deliberately a human
        decision (see ``webhook/payments._alert_reversal``). This only
        makes the fact durable so a later payout decision can see it.

        A redelivery of the same reversal overwrites the stamp with the
        same values, which is why this returns "was there a row" rather
        than "did anything change": the caller distinguishes "first
        time we hear this" from "we already knew" by reading
        :attr:`CreditRecord.reversed_at` via :meth:`get` beforehand.

        ``tombstone=True`` (#226) changes the "nothing to stamp" case
        from a no-op into an INSERT: a row with ``user_id=0`` and
        ``credited_amount=0``, already stamped as reversed. It is a
        claim about a payment we never credited — "if this id is ever
        paid, do not pay it out" — and it is the only way a reversal
        that outran its own payment can stop the retry that follows
        it. Callers must only set it for a reversal they authenticated
        AND for a provider whose reversal carries the same id as the
        credit; see ``webhook/payments._resolve_reversal_credit``.

        Flush, not commit, matching :meth:`mark_processed` — the caller
        owns the transaction.
        """
        stmt = select(ProcessedWebhook).where(
            ProcessedWebhook.provider == provider,
            ProcessedWebhook.external_id == external_id,
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        stamp = reversed_at or datetime.now(UTC).replace(tzinfo=None)
        if row is None:
            if not tombstone:
                return False
            row = ProcessedWebhook(
                provider=provider,
                external_id=external_id,
                user_id=0,
                credited_amount=0,
                processed_at=stamp,
            )
            self._session.add(row)
        row.reversed_at = stamp
        row.reversed_event = event
        await self._session.flush()
        return True
