"""Async repository for ``economy.withdrawal_requests`` — write side (T-027).

The read slice (pending count / sample) already powers
``/admin_withdrawals``; this adds the writes the user-side ``/withdraw``
flow and the admin approve/reject callbacks need:

* ``create`` — insert a ``pending`` row (the wallet escrow debit is the
  caller's job, in the same transaction).
* ``get`` — load one row by id.
* ``claim_terminal`` — transition ``from_status`` → ``completed`` /
  ``rejected`` *conditionally* on the row still being in that status.
  ``rowcount`` is the success signal: two admins racing on the same
  request both call this, but only the first sees ``rowcount == 1`` —
  the second gets ``0`` and the handler renders "already processed".
  This is the same TOCTOU-safe single-statement guard
  ``ChecksRepo.claim_decrement`` uses.
* ``count_stale_pending`` / ``list_stale_pending`` / ``mark_alerted``
  — the #169 ageing signal. Payouts are manual, so ``pending`` is a
  queue a human works; these let the sweeper notice the queue has
  stopped moving, tell the owner, and remember that it did. The
  remembering is durable (``alerted_at``) since #1518 — it used to
  be a set on the sweeper instance, which every deploy emptied.
* ``claim_processing`` / ``release_processing`` — the non-terminal
  ``processing`` lease that brackets the Crypto Pay transfer (T-020
  R13). A status-guarded claim BEFORE the network call is what stops a
  concurrent ``reject`` from refunding the escrow while the payout is
  in flight; ``release_processing`` hands the row back on a provider
  error so a failed payout stays actionable.

The repo trusts its arguments — limit checks, dev/admin gating, and the
Crypto Pay transfer all live above it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, cast

from sqlalchemy import false, func, select, update

from telegram_invite_bot.db.models.economy import WithdrawalRequest

if TYPE_CHECKING:
    from collections.abc import Collection

    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

# Statuses whose ``amount_com`` counts against a user's withdrawal quota.
# A ``rejected`` request had its escrow refunded, so it must NOT consume
# quota; ``pending`` (escrow held, awaiting an admin), ``processing``
# (escrow held, payout in flight — see ``claim_processing``) and
# ``completed`` (paid out) all represent real, non-returned outflow and
# DO count. Omitting ``processing`` would open a quota hole exactly as
# wide as the provider round-trip: a request mid-payout would stop
# counting, and a second withdrawal could slip through the window.
PROCESSING_STATUS = "processing"
_QUOTA_STATUSES = ("pending", PROCESSING_STATUS, "completed")


class StalePending(NamedTuple):
    """One overdue ``pending`` request, as the #169 alert needs it.

    Deliberately does NOT carry ``payment_details``. The alert is a DM
    to the owner saying "this queue stopped moving"; the card behind
    ``/admin_withdrawals`` is where the payout details belong, behind a
    private-only router. Keeping card numbers out of a push message
    means a forwarded or screenshotted alert leaks nothing.
    """

    request_id: int
    user_id: int
    amount_com: int
    created_at: str | None


class WithdrawalsRepo:
    """``economy.withdrawal_requests`` write access."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: int,
        amount_com: int,
        amount_crypto: float,
        asset: str,
        created_at: str,
        payment_method: str = "crypto_pay",
    ) -> int:
        """Insert a ``pending`` request; return the new row id.

        The wallet escrow debit is NOT done here — the caller wraps this
        insert and the debit in one ``session.begin()`` so the coins are
        held and the row created atomically. ``flush`` populates the
        autoincrement id without committing the outer transaction.
        """
        row = WithdrawalRequest(
            user_id=user_id,
            amount_com=amount_com,
            amount_crypto=amount_crypto,
            currency=asset,
            payment_method=payment_method,
            status="pending",
            created_at=created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def get(self, request_id: int) -> WithdrawalRequest | None:
        """Load one request by id, or ``None`` if it does not exist."""
        result = await self._session.execute(
            select(WithdrawalRequest).where(WithdrawalRequest.id == request_id)
        )
        return result.scalar_one_or_none()

    async def lock_writer(self) -> None:
        """Take this database's writer lock now, before the quota reads.

        #776. SQLite serialises writers, not readers, and this project
        opens transactions lazily: ``db/engines.py:204-210`` issues
        ``BEGIN IMMEDIATE`` only when it sees a *write*-headed statement,
        and ``"select"`` is in ``_NON_WRITE_HEADS``. So a SELECT-only
        prologue runs outside any transaction at all, and two concurrent
        callers can both read the same pre-debit totals before either one
        writes. That is not a theoretical window: the whole quota design
        (see :meth:`period_usage`) derives usage by summing rows, so two
        requests that each fit under the cap individually will both be
        admitted if they read before the other inserts.

        The remedy is to make the *first* statement of the critical
        section a write, so the lock is held across the reads that decide
        the outcome. This UPDATE matches nothing — ``WHERE false`` — but
        its ``UPDATE`` head is what ``engines.py:207`` keys on, so the
        connection enters ``BEGIN IMMEDIATE`` and a second caller blocks
        on ``PRAGMA busy_timeout`` (5 000 ms, ``db/pragma.py:63``) until
        the first commits. The second then reads *committed* totals.

        Writing no rows is the point: the lock must be acquirable before
        we know whether the request will be granted, and a refusal must
        be able to release it having changed nothing.
        """
        await self._session.execute(
            update(WithdrawalRequest)
            .where(false())
            .values(status=WithdrawalRequest.status)
            .execution_options(synchronize_session=False)
        )

    async def lifetime_usage(self, user_id: int) -> int:
        """Sum every quota-consuming ``amount_com`` this user has ever
        requested — the withdrawn side of the T-020 (R6) payout cap.

        Deliberately NOT ``period_usage`` with an empty lower bound: that
        query compares ``created_at >= since_iso``, which drops rows whose
        ``created_at`` is NULL (legacy imports). For a *rolling window*
        excluding them is the safe default — they can't inflate today's
        cap. For a *lifetime* total the safe default is the opposite:
        money that left is money that left, timestamp or not, and
        forgetting it would hand the user free headroom under the cap.
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(WithdrawalRequest.amount_com), 0)).where(
                WithdrawalRequest.user_id == user_id,
                WithdrawalRequest.status.in_(_QUOTA_STATUSES),
            )
        )
        return int(result.scalar_one())

    async def period_usage(self, user_id: int, *, since_iso: str) -> int:
        """Sum the user's quota-consuming ``amount_com`` since ``since_iso``.

        Race-free, counter-free quota tracking: instead of storing a
        ``daily_used`` column that a scheduled job must zero on the cycle
        boundary, we derive current-period usage on demand by summing the
        ``amount_com`` of this user's non-rejected requests whose
        ``created_at`` is ``>= since_iso`` (the start of the current day or
        month). The "reset" is implicit — once the window's lower bound
        moves past yesterday's rows they simply stop matching, so there is
        no counter to drift out of sync and no UPDATE race between a reset
        and a concurrent withdrawal.

        What derivation does *not* buy on its own is isolation between two
        concurrent withdrawals: this is a SELECT, and a SELECT does not
        open a write transaction here (``db/engines.py:207-208``). Callers that
        act on the result must hold the writer lock first — see
        :meth:`lock_writer` and its caller ``WithdrawService.create``.

        ``created_at`` is the ISO ``"YYYY-MM-DD HH:MM:SS"`` text the
        write-side stamps (see ``WithdrawService._now_iso``); lexicographic
        comparison on that fixed-width format is equivalent to chronological
        comparison, so a plain string ``>=`` filters the window correctly.
        Rows with a NULL ``created_at`` (legacy imports) are excluded by the
        comparison, which is the safe default — they don't inflate the cap.
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(WithdrawalRequest.amount_com), 0)).where(
                WithdrawalRequest.user_id == user_id,
                WithdrawalRequest.status.in_(_QUOTA_STATUSES),
                WithdrawalRequest.created_at >= since_iso,
            )
        )
        return int(result.scalar_one())

    async def count_stale_pending(self, *, older_than_iso: str) -> int:
        """How deep the overdue ``pending`` queue is, alerted or not.

        This is the header total of the #169 alert: the sample below it
        names at most five requests, so without an honest total a
        five-bullet list reads as the whole queue.

        It used to return the id SET, because the sweeper kept its
        ledger in memory and pruned it against this result every pass
        (#208). #1518 moved the ledger into ``alerted_at``, so the ids
        are no longer needed here — only the depth — and a ``COUNT`` is
        both cheaper and honest about what the caller does with it.

        Same lexicographic-``<``-is-chronological-``<`` reasoning as
        :meth:`period_usage`: ``created_at`` is fixed-width
        ``"YYYY-MM-DD HH:MM:SS"`` text. Rows with a NULL ``created_at``
        (legacy imports) fail the comparison and are NOT counted —
        unknown age must not raise an alarm the owner cannot act on,
        and the ``/admin_withdrawals`` card takes the same position so
        the two surfaces agree.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(WithdrawalRequest)
            .where(
                WithdrawalRequest.status == "pending",
                WithdrawalRequest.created_at < older_than_iso,
            )
        )
        return int(result.scalar_one())

    async def list_stale_pending(self, *, older_than_iso: str, limit: int) -> list[StalePending]:
        """Oldest-first sample of overdue ``pending`` requests not yet reported.

        ``limit`` is mandatory, not defaulted: this feeds a Telegram
        message, and an unbounded list is how a courtesy alert turns
        into a 400 the owner never sees. Oldest-first matches the card's
        ordering, so the ids in the alert are the ids at the head of the
        queue the owner is about to open.

        "Not yet reported" is ``alerted_at IS NULL`` (#1518). #208 is
        what this clause is for: without it the window is pinned to the
        five oldest overdue requests, and a head that never clears hides
        everything queued behind it, so the alarm falls silent exactly
        when the queue stops moving. The predicate used to be an
        ``exclude_ids`` argument fed from a set on the sweeper — same
        semantics while the process lived, reset on every deploy.
        """
        stmt = (
            select(
                WithdrawalRequest.id,
                WithdrawalRequest.user_id,
                WithdrawalRequest.amount_com,
                WithdrawalRequest.created_at,
            )
            .where(
                WithdrawalRequest.status == "pending",
                WithdrawalRequest.created_at < older_than_iso,
                WithdrawalRequest.alerted_at.is_(None),
            )
            .order_by(WithdrawalRequest.id.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [StalePending(int(r[0]), int(r[1]), int(r[2]), r[3]) for r in result.all()]

    async def mark_alerted(self, request_ids: Collection[int], *, alerted_at: str) -> int:
        """Stamp ``alerted_at`` on the requests the owner was just told about.

        Returns how many rows were stamped. Guarded on ``alerted_at IS
        NULL`` so a concurrent pass (or a retry of the same pass) can
        never overwrite the timestamp of the DM that actually named the
        request — the value is evidence of WHEN the owner was told, and
        a later pass rewriting it would quietly reset that.

        Called AFTER a successful send, in its own session: a Telegram
        round trip must not sit inside a money-DB transaction, and an
        alert that failed to deliver has to stay unmarked so the next
        pass retries it.
        """
        if not request_ids:
            return 0
        result = await self._session.execute(
            update(WithdrawalRequest)
            .where(
                WithdrawalRequest.id.in_(tuple(request_ids)),
                WithdrawalRequest.alerted_at.is_(None),
            )
            .values(alerted_at=alerted_at)
        )
        return cast("CursorResult[Any]", result).rowcount

    async def claim_terminal(
        self,
        request_id: int,
        *,
        status: str,
        processed_by: int,
        processed_at: str,
        tx_hash: str | None = None,
        admin_note: str | None = None,
        from_status: str = "pending",
    ) -> bool:
        """Move a request out of ``from_status`` into a terminal ``status``.

        Single conditional UPDATE guarded on the current status —
        ``rowcount == 1`` means this caller won the claim, ``0`` means
        the row was already processed (or never existed). The guard is
        what makes a double-approve / approve-then-reject race safe: the
        loser sees ``False`` and the handler tells the admin it is
        already done, instead of issuing a second Crypto Pay transfer or
        a double refund.

        ``from_status`` defaults to ``"pending"`` — the manual-payout and
        reject paths transition straight from the queue. The automatic
        payout path finalises from :data:`PROCESSING_STATUS` instead,
        because it took the lease before calling the provider.
        """
        result = await self._session.execute(
            update(WithdrawalRequest)
            .where(
                WithdrawalRequest.id == request_id,
                WithdrawalRequest.status == from_status,
            )
            .values(
                status=status,
                processed_by=processed_by,
                processed_at=processed_at,
                tx_hash=tx_hash,
                admin_note=admin_note,
            )
        )
        return cast("CursorResult[Any]", result).rowcount == 1

    async def claim_processing(self, request_id: int, *, processed_by: int) -> bool:
        """Lease a request for an in-flight payout: → ``processing``.

        The lease exists because a Crypto Pay transfer is network I/O
        measured in seconds, and a DB transaction must not be held open
        across it. Before T-020 R13 the caller simply read
        ``status == 'pending'``, released the transaction, paid, and
        claimed afterwards — so a :meth:`WithdrawService.reject` landing
        in that window flipped the row to ``rejected`` and credited the
        escrow back to the user while the USDT was already on its way.
        The user kept both. Claiming first closes the window: ``reject``
        and ``approve_manual`` both guard on ``pending``, so neither can
        touch a leased row.

        Matches ``pending`` OR ``processing`` — re-claiming an already
        leased row is deliberate. A process that dies mid-transfer would
        otherwise strand the request forever; re-driving it is safe
        because the provider dedupes on ``spend_id``, and the row stays
        un-refundable throughout. Terminal rows (``completed`` /
        ``rejected``) never match, so this can't resurrect a finished
        payout.
        """
        result = await self._session.execute(
            update(WithdrawalRequest)
            .where(
                WithdrawalRequest.id == request_id,
                WithdrawalRequest.status.in_(("pending", PROCESSING_STATUS)),
            )
            .values(status=PROCESSING_STATUS, processed_by=processed_by)
        )
        return cast("CursorResult[Any]", result).rowcount == 1

    async def release_processing(self, request_id: int) -> bool:
        """Hand a leased request back to the queue: ``processing`` → ``pending``.

        Called when the provider *refused* the transfer — an empty app
        wallet, or an ``ok:false`` API error. Those are the cases where
        no money moved, so the request must become actionable again
        rather than sitting in a status no admin surface lists.

        "The provider raised" is NOT the same condition, and the
        difference is money: a timeout or a 5xx may sit on top of a
        transfer that executed, and queueing such a row makes it
        rejectable — which refunds the escrow the user was paid for.
        ``WithdrawService.approve`` therefore calls this only for
        definitive refusals; see ``CryptoPayUnconfirmed``.

        Guarded on ``processing`` so it can never pull a finished payout
        back into the queue.

        Clears ``alerted_at`` (#1518). This is the one path that puts a
        row BACK into ``pending``, so it is the one path where the #169
        alert has to re-arm: the request became actionable again for a
        new reason, and a stamp from the DM that named it before the
        payout attempt would suppress the alert forever. Ageing is
        measured from ``created_at``, which this does not touch — a
        request refused after two days is overdue the moment it lands
        back in the queue, and should be.
        """
        result = await self._session.execute(
            update(WithdrawalRequest)
            .where(
                WithdrawalRequest.id == request_id,
                WithdrawalRequest.status == PROCESSING_STATUS,
            )
            .values(status="pending", alerted_at=None)
        )
        return cast("CursorResult[Any]", result).rowcount == 1
