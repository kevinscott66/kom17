"""Stage 9 — composition layer over EconomyRepo + TransactionsRepo.

Every wallet move in the new pipeline goes through this service, not
through the repos directly. Why a service at all when the repos are
already pretty narrow:

1. **Validation belongs here.** The repos trust their inputs (Stage 8
   docstring). The service runs ``validate_credit_amount`` /
   ``validate_balance_target`` before the UPDATE, so an invalid call
   short-circuits before opening a connection.

2. **Ledger composition.** The ``Transaction`` row write is the
   companion to the wallet UPDATE. Doing them in two separate handler
   calls would scatter the "every wallet move logs a row" rule across
   N call sites — and one missed site = a desynced ledger that
   ``/cstats`` and ``/admin_donations`` would expose to users.

3. **Transfer atomicity.** A user→user transfer is two wallet writes
   that must both succeed or both fail. The repo-level methods don't
   know about each other; only a service that holds both can express
   the rollback-on-second-failure rule. SQLAlchemy session-level
   transactions give us that for free as long as the service composes
   inside one ``async with session.begin()`` block.

Result types
------------
Service methods return the updated :class:`Wallet` on success and
``None`` on the "expected" failure modes (insufficient funds, missing
wallet, validation rejection). Truly exceptional outcomes (DB down,
session closed) propagate as exceptions — the handler middleware
catches them. This matches Stage 7's :class:`UserService` posture
(``None``-on-not-found, raise-on-broken).

Future surface
--------------
``claim_daily`` and the ``/buy`` transactional flow land here once
the inventory writes need composing with balance writes. Each of
those would be a few lines on top of the primitives below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram_invite_bot.utils.economy import (
    validate_balance_target,
    validate_credit_amount,
)

if TYPE_CHECKING:
    from telegram_invite_bot.core.entities.wallet import Wallet
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


class EconomyService:
    """Wallet-mutation entry point composing balance + ledger writes."""

    def __init__(
        self,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
    ) -> None:
        self._economy = economy_repo
        self._ledger = transactions_repo

    async def credit(
        self,
        user_id: int,
        amount: int,
        *,
        type: str,
        reason: str | None = None,
        from_id: int | None = None,
        ensure_wallet: bool = False,
    ) -> Wallet | None:
        """Add ``amount`` to ``user_id``'s wallet and log a ledger row.

        Returns the updated wallet on success; ``None`` if validation
        rejects the amount OR the wallet does not exist. The two
        ``None`` cases are operationally distinct (caller bug vs.
        phantom user) but produce the same handler response ("we
        couldn't credit that amount"), so the single signal is the
        cheaper API. Bugs surface in the validation tests, not at
        runtime.

        ``ensure_wallet`` seeds the row before the UPDATE when the
        caller cannot afford a "phantom user" refusal. Legacy had no
        such switch: ``add_coins`` opened with ``register_user`` on
        EVERY path (bot.py:9724, commented "иначе UPDATE ничего не
        изменит"), so legacy always self-healed. The port defaults to
        ``False`` deliberately — for a *transfer* a missing recipient
        should refuse rather than conjure a wallet — and turns it on
        where the money has already changed hands and refusing would
        strand a real payment (#770). ``get_or_create`` is an
        ``ON CONFLICT DO NOTHING`` upsert, so the extra call is safe
        under concurrency and costs one cheap probe when the row is
        already there.

        ``type`` is required because the ledger schema does not
        accept null types — and every concrete site has a meaningful
        value (``"referral"``, ``"daily"``, ``"admin_give"``, …).
        Making it keyword-only forces the caller to think about it.
        """
        if not validate_credit_amount(amount):
            return None
        if ensure_wallet:
            await self._economy.get_or_create(user_id)
        wallet = await self._economy.credit(user_id, amount)
        if wallet is None:
            return None
        await self._ledger.record(
            from_id=from_id,
            to_id=user_id,
            amount=amount,
            reason=reason,
            type=type,
        )
        return wallet

    async def debit(
        self,
        user_id: int,
        amount: int,
        *,
        type: str,
        reason: str | None = None,
        to_id: int | None = None,
    ) -> Wallet | None:
        """Subtract ``amount`` from ``user_id``'s wallet (if affordable)
        and log a ledger row.

        Same validation / None semantics as :meth:`credit`. Note
        ``to_id`` is the *recipient* in a transfer-style debit (e.g.
        a fee paid into the dev wallet); leave it ``None`` for a
        pure spend (shop purchase). The legacy code conflates the
        two via ``from_id=user_id, to_id=admin_id_or_0``, which made
        ``SUM(amount) WHERE to_id=admin_id`` lie when the admin
        wasn't actually receiving anything. The new shape is
        explicit.

        Ledger row is NOT written if the debit fails — would
        corrupt the running totals.
        """
        if not validate_credit_amount(amount):
            return None
        wallet = await self._economy.debit(user_id, amount)
        if wallet is None:
            return None
        await self._ledger.record(
            from_id=user_id,
            to_id=to_id,
            amount=amount,
            reason=reason,
            type=type,
        )
        return wallet

    async def hold(
        self,
        user_id: int,
        amount: int,
        *,
        type: str,
        reason: str | None = None,
        to_id: int | None = None,
    ) -> Wallet | None:
        """Escrow ``amount`` out of the wallet and log a ledger row (#238).

        :meth:`debit` with the lifetime counter left alone — see
        :meth:`telegram_invite_bot.repositories.economy_repo.EconomyRepo.hold`
        for the legacy evidence that an escrow is not a spend.
        Validation and the ``None`` contract are identical to
        :meth:`debit`.

        The companion ledger row is still written. Parking coins is a
        real movement an audit has to be able to follow; what it is
        *not* is lifetime expenditure, and those two facts live in
        different columns.

        ``type`` stays required for the same reason it is on
        :meth:`debit`, and ``tests/regression/test_money_call_sites``
        keys its "already booked" heuristic off its presence.
        """
        if not validate_credit_amount(amount):
            return None
        wallet = await self._economy.hold(user_id, amount)
        if wallet is None:
            return None
        await self._ledger.record(
            from_id=user_id,
            to_id=to_id,
            amount=amount,
            reason=reason,
            type=type,
        )
        return wallet

    async def release(
        self,
        user_id: int,
        amount: int,
        *,
        type: str,
        reason: str | None = None,
        from_id: int | None = None,
    ) -> Wallet | None:
        """Return escrowed ``amount`` to the wallet and log a ledger row.

        :meth:`credit` with ``total_earned`` left alone — the mirror of
        :meth:`hold`, and its only correct partner.

        Choosing between this and :meth:`credit` is the whole point of
        the pair: use :meth:`release` when the coins are being handed
        *back* to someone who already owned them (a rejected withdrawal,
        a cancelled order, an expired match), and :meth:`credit` when
        they are genuinely new to that wallet (a reward, a payout, a
        gift received). Getting it wrong does not misplace a coin — it
        rewrites the user's lifetime history.
        """
        if not validate_credit_amount(amount):
            return None
        wallet = await self._economy.release(user_id, amount)
        if wallet is None:
            return None
        await self._ledger.record(
            from_id=from_id,
            to_id=user_id,
            amount=amount,
            reason=reason,
            type=type,
        )
        return wallet

    async def settle_hold(self, user_id: int, amount: int) -> Wallet | None:
        """Book a completed :meth:`hold` as a genuine lifetime spend (#1546).

        The third leg of the escrow pair. :meth:`hold` parks coins
        without touching ``total_spent`` precisely because the caller
        may hand every one of them back; when the caller instead
        *keeps* them, the spend still has to reach the lifetime
        counter, or ``/balance`` under-reports what the user actually
        paid. ``EconomyRepo.hold``'s own docstring names this step:
        "A hold that later turns into a genuine spend pairs this with
        :meth:`bump_totals` at settlement time."

        Use it for a purchase whose write can still fail *after* the
        coins have left the wallet — ``/marry_extend`` and the couple
        activities, where the bond row can vanish between the gate and
        the UPDATE. Those paths cannot use :meth:`debit` because their
        rollback would then be a :meth:`credit`, and the pair inflates
        BOTH lifetime counters on a round trip that moved no money.
        The withdrawal path, by contrast, deliberately never settles —
        legacy's ``admin_confirm_withdrawal`` (bot.py:20717) issues no
        balance UPDATE at all.

        Moves no coins and writes no ledger row: :meth:`hold` already
        booked the movement, and a second row for the same coins would
        double-count in ``/cstats``. That is also why the name is not
        ``credit``/``debit``/``hold``/``release`` — see
        :meth:`telegram_invite_bot.repositories.economy_repo.EconomyRepo.bump_totals`
        for the same reasoning about
        ``tests/regression/test_money_call_sites``.

        Returns the refreshed wallet, or ``None`` if the wallet row is
        gone or ``amount`` is negative / out of range.
        """
        if amount < 0:
            return None
        return await self._economy.bump_totals(user_id, spent=amount)

    async def transfer(
        self,
        *,
        from_user_id: int,
        to_user_id: int,
        amount: int,
        reason: str | None = None,
        type: str = "transfer",
    ) -> tuple[Wallet, Wallet] | None:
        """Move ``amount`` from one wallet to another with one ledger row.

        Returns ``(sender_wallet, recipient_wallet)`` on success;
        ``None`` if validation rejects, sender lacks funds, or the
        recipient wallet does not exist. The single None is OK
        because handler responses (``/gift``) say "transfer failed"
        without distinguishing.

        Atomicity: both wallet writes and the ledger row run inside
        a SAVEPOINT (``session.begin_nested``), so a credit that
        fails after the sender was already debited rolls the pair
        back by itself instead of trusting the caller to do it.

        This docstring used to promise the opposite — "the outer
        rollback restores the sender" — and the promise was false
        for the wrapper this project actually runs.
        ``BaseSessionMiddleware`` (middlewares/base.py:111-138)
        rolls back only on a raised exception and commits on every
        normal return, so the plain ``return None`` below committed
        a debit that no credit matched and the sender's coins
        vanished. ``TransferService.send`` had to close that same
        hole (R-FIX-002); #1520 closes it here, in the primitive,
        so the method is safe whatever the caller does.

        The SAVEPOINT relies on SQLAlchemy 2.x autobegin — a session
        constructed with ``autobegin=False`` would degrade the
        nested call to a no-op and reopen the window. Every caller
        path runs under ``BaseSessionMiddleware``, which takes the
        default; ``test_transfer_uses_a_savepoint`` pins that
        ``begin_nested`` is really called.

        Self-transfer is rejected as a no-op (legacy allowed it but
        always to the same effect — a wasted ledger row). The
        caller can show a "can't gift yourself" message based on
        the ``None`` return.
        """
        if from_user_id == to_user_id:
            return None
        if not validate_credit_amount(amount):
            return None
        session = self._economy._session  # noqa: SLF001 — same-package use
        async with session.begin_nested() as savepoint:
            sender = await self._economy.debit(from_user_id, amount)
            if sender is None:
                # Nothing was mutated (the UPDATE matched no row), so
                # letting the context manager release an empty
                # savepoint on the way out is exactly right.
                return None
            recipient = await self._economy.credit(to_user_id, amount)
            if recipient is None:
                # Undo the sender's debit before returning, so the
                # handler's "transfer failed" message is true about
                # the wallet as well as about the transfer. The
                # ambient transaction is untouched: unrelated writes
                # in the same session still commit.
                await savepoint.rollback()
                return None
            await self._ledger.record(
                from_id=from_user_id,
                to_id=to_user_id,
                amount=amount,
                reason=reason,
                type=type,
            )
        return sender, recipient

    async def set_balance(
        self,
        user_id: int,
        new_balance: int,
        *,
        admin_id: int,
        reason: str | None = None,
    ) -> Wallet | None:
        """Admin-style direct set with a ledger row recording the delta.

        Mirrors legacy ``set_balance`` (bot.py:9628) — computes the
        delta vs the previous balance and writes that as the ledger
        amount. ``type='admin_set'`` matches what legacy records,
        keeping the ``/admin_donations`` view consistent across
        pipelines.

        A zero-delta call is a no-op — no ledger row, no counter
        bump — mirroring legacy's ``if amount_diff != 0`` guard
        (bot.py:9666).

        Direction, not sign (#257)
        --------------------------
        Legacy stores the delta *signed* (bot.py:9670 passes
        ``amount=amount_diff``). This pipeline stores magnitudes and
        puts the direction in the ``from_id``/``to_id`` pair, on
        purpose (the "Sign convention" section of the
        ``transactions_repo`` module docstring), so a deduction is
        recorded as the user paying the admin rather than the admin
        paying a negative amount. Writing ``from_id=admin_id`` for
        both directions — as this method used to — made every admin
        deduction render as *income* on the read side, because
        ``TransactionsRepo.recent`` derives the sign purely from
        ``to_id == user_id`` and ABSes the magnitude, as does
        ``TransactionsRepo.window_stats``.
        Storing legacy's negative number instead would not have helped
        for exactly the same reason.

        Lifetime counters (#257)
        ------------------------
        Legacy also bumps ``total_earned`` / ``total_spent`` from the
        same delta (bot.py:9676-9679). ``EconomyRepo.set_balance``
        writes only the ``balance`` column, so the counters are moved
        here through :meth:`EconomyRepo.bump_totals`, giving the same
        end state in one transaction.
        """
        if not validate_balance_target(new_balance):
            return None
        # Read the old balance BEFORE the write so we know the delta
        # to log. The read+write is not atomic without a transaction
        # wrapper, but for admin set_balance that's acceptable — a
        # concurrent admin write between the read and our UPDATE
        # would just produce a ledger row with a slightly stale
        # delta, which the row's timestamp lets an auditor untangle.
        existing = await self._economy.get(user_id)
        if existing is None:
            return None
        delta = new_balance - existing.balance
        wallet = await self._economy.set_balance(user_id, new_balance)
        if wallet is None:
            return None
        if delta != 0:
            credited = delta > 0
            await self._ledger.record(
                from_id=admin_id if credited else user_id,
                to_id=user_id if credited else admin_id,
                amount=abs(delta),
                reason=reason or "admin balance set",
                type="admin_set",
            )
            refreshed = await self._economy.bump_totals(
                user_id,
                earned=delta if credited else 0,
                spent=0 if credited else -delta,
            )
            # ``bump_totals`` targets the row the UPDATE above just
            # returned, inside the same session and transaction, with
            # magnitudes bounded by ``validate_balance_target`` — so
            # ``None`` here is unreachable in practice. Falling back to
            # the pre-bump wallet rather than failing the whole call
            # keeps the balance write (already applied) and the return
            # value consistent; the counters are a statistic, and
            # losing one is not worth discarding a successful admin
            # adjustment.
            if refreshed is not None:
                wallet = refreshed
        return wallet
