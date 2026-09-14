"""Async repository for the PvP escrow stake-game tables (AUD-2).

Owns ``pvp_offers`` / ``pvp_escrow`` — data-shape operations only. Money
composition (escrow debits, payout/refund credits, ledger rows,
rollbacks) lives in
:class:`~telegram_invite_bot.services.pvp_service.PvpService`, which runs
this repo on the SAME shared ``economy`` session as ``EconomyRepo`` /
``TransactionsRepo`` so every flow commits atomically — the same
composition posture as :class:`P2pRepo` / :class:`P2pService`.

Race posture (the hardening over legacy bot.py:14894):

* :meth:`claim_for_accept` is the project-standard atomic guard::

      UPDATE pvp_offers SET opponent_id=:uid, status='active', accepted_at=:now
       WHERE id=:id AND status='pending' AND opponent_id IS NULL

  with a rowcount check. Two opponents tapping Accept on the same card
  cannot both win — the second sees ``status='active'`` and gets
  ``rowcount == 0``, so the game resolves and pays out exactly once
  (legacy checked status in Python first, then wrote — a TOCTOU window
  that could double-resolve).

* :meth:`cancel_guard` flips ``pending → cancelled`` for the creator in
  one statement (double tap can't refund twice).

* :meth:`expire_guard` flips a stale ``pending → expired`` with the
  ``created_at < cutoff`` re-check riding the UPDATE, so a concurrent
  accept at the deadline wins cleanly (whichever statement lands first;
  the loser's guard fails).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import select, update

from telegram_invite_bot.db.models.pvp import PvpEscrow, PvpOffer

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


# Offer statuses (TEXT in the schema; centralised here so the service,
# the handler and tests share one vocabulary).
OFFER_PENDING = "pending"
OFFER_ACTIVE = "active"
OFFER_FINISHED = "finished"
OFFER_CANCELLED = "cancelled"
OFFER_EXPIRED = "expired"

# Escrow statuses.
ESCROW_HELD = "held"
ESCROW_RELEASED = "released"
ESCROW_REFUNDED = "refunded"


class PvpRepo:
    """``pvp_offers`` / ``pvp_escrow`` access. Per-request, open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Offers
    # ------------------------------------------------------------------

    async def create_offer(
        self,
        *,
        game: str,
        creator_id: int,
        bet: int,
        params_json: str | None,
        chat_id: int | None,
        now: datetime,
    ) -> PvpOffer:
        """Insert a ``pending`` offer (legacy bot.py:14844).

        The caller (service) has ALREADY debited the creator and written
        the matching :meth:`hold_escrow` row — the escrow invariant is
        "this row exists ⇔ the wallet paid for it", enforced by both
        landing in one transaction.
        """
        row = PvpOffer(
            game=game,
            status=OFFER_PENDING,
            chat_id=chat_id,
            message_id=None,
            creator_id=creator_id,
            opponent_id=None,
            bet=bet,
            params_json=params_json,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_offer(self, offer_id: int) -> PvpOffer | None:
        return await self._session.get(PvpOffer, offer_id)

    async def set_message(self, offer_id: int, chat_id: int, message_id: int) -> None:
        """Pin the published group card (chat + message id) onto the offer.

        Mirrors legacy ``pvp_post_offer`` (bot.py:14982) minus the
        status guard — the strangler publishes the card BEFORE inserting
        the offer is impossible (we need the id for the callback), so we
        send the card, then store its id; a failed store is non-fatal
        (the card just can't be edited on resolve, which the handler
        tolerates).
        """
        await self._session.execute(
            update(PvpOffer)
            .where(PvpOffer.id == offer_id)
            .values(chat_id=chat_id, message_id=message_id)
        )

    async def claim_for_accept(self, offer_id: int, opponent_id: int, now: datetime) -> bool:
        """Atomically claim a ``pending`` offer for ``opponent_id``.

        THE race guard (the double-accept fix)::

            UPDATE pvp_offers
               SET opponent_id=:uid, status='active', accepted_at=:now
             WHERE id=:id AND status='pending' AND opponent_id IS NULL

        ``rowcount == 0`` (False) means the offer was already accepted,
        cancelled or expired between the caller's read and this write —
        the caller MUST abort (no second escrow hold, no resolve). The
        creator-can't-accept-own-offer check lives in the service (it
        needs the offer row anyway); doing it here too would need the
        creator id in the WHERE and gains nothing.
        """
        stmt = (
            update(PvpOffer)
            .where(
                PvpOffer.id == offer_id,
                PvpOffer.status == OFFER_PENDING,
                PvpOffer.opponent_id.is_(None),
            )
            .values(opponent_id=opponent_id, status=OFFER_ACTIVE, accepted_at=now)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def revert_accept(self, offer_id: int) -> None:
        """Roll an ``active`` claim back to ``pending`` (opponent's escrow
        hold failed right after the claim won).

        Compensating transition: the service won the
        :meth:`claim_for_accept` guard but then couldn't debit the
        opponent (drained between the read and the hold), so the offer
        must return to ``pending`` for someone else to accept. Mirrors
        legacy's accept rollback (bot.py:14922-14926).
        """
        await self._session.execute(
            update(PvpOffer)
            .where(PvpOffer.id == offer_id, PvpOffer.status == OFFER_ACTIVE)
            .values(opponent_id=None, status=OFFER_PENDING, accepted_at=None)
        )

    async def finish(self, offer_id: int, result_json: str, now: datetime) -> None:
        """Stamp the resolved offer ``finished`` with its result blob.

        Called by the service AFTER the payout/refund credits + escrow
        flips have landed in this same transaction (legacy bot.py:14976).
        """
        await self._session.execute(
            update(PvpOffer)
            .where(PvpOffer.id == offer_id)
            .values(status=OFFER_FINISHED, result_json=result_json, finished_at=now)
        )

    async def cancel_guard(self, offer_id: int, creator_id: int) -> int | None:
        """Cancel the creator's own ``pending`` offer; return the held bet.

        Guarded ``pending → cancelled`` by the creator in one statement,
        so a double tap / concurrent accept can't double-refund: the
        loser sees no row. ``None`` == guard rejected (not found / not
        yours / already accepted/cancelled/expired). The refundable
        amount is the offer's ``bet`` (the creator's single held stake);
        the service credits it back and flips the escrow row.
        """
        stmt = (
            update(PvpOffer)
            .where(
                PvpOffer.id == offer_id,
                PvpOffer.creator_id == creator_id,
                PvpOffer.status == OFFER_PENDING,
            )
            .values(status=OFFER_CANCELLED, finished_at=None)
            .returning(PvpOffer.bet)
        )
        result = await self._session.execute(stmt)
        bet = result.scalar_one_or_none()
        return int(bet) if bet is not None else None

    async def list_pending_older_than(self, cutoff: datetime, *, limit: int) -> list[PvpOffer]:
        """Pending offers created before ``cutoff`` — the expiry scan.

        Backs the background sweep
        (:meth:`~telegram_invite_bot.services.pvp_service.PvpService.sweep_expired`)
        ONLY. The lazy on-access expiry that legacy ran at accept time
        (``pvp_expire_offers``, bot.py:14798 called from bot.py:14905)
        is ported in ``PvpService.accept_and_resolve``, but it works off
        the single offer the clicker named rather than scanning — see
        #267, where this docstring's claim that the lazy path existed
        here was the thing that hid its absence.

        Only ``pending`` (unaccepted) offers expire; an ``active`` offer
        is mid-resolution and never times out here.

        ``limit`` is mandatory, not defaulted (#1619), the same way
        ``WithdrawalsRepo.list_stale_pending`` makes it mandatory: this
        runs on the 60-second money tick, and the caller opens a
        savepoint per row it gets back. The WHERE does ride an index on
        prod (``idx_pvp_offers_status``), so the scan itself is over the
        ``pending`` subset rather than the table — what the cap bounds is
        the WORK PER PASS, not the read. It is needed because the set is
        not guaranteed to drain: ``PvpService.expire`` leaves an offer
        ``pending`` when its refund does not apply, so a permanently
        unrefundable offer is re-selected on every pass forever. The cap
        does not fix that (a terminal status for such an offer is a
        product decision, deliberately left to its own ticket) — it only
        stops the residue from setting the size of a pass.
        """
        stmt = (
            select(PvpOffer)
            .where(
                PvpOffer.status == OFFER_PENDING,
                PvpOffer.opponent_id.is_(None),
                PvpOffer.created_at < cutoff,
            )
            .order_by(PvpOffer.id.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def expire_guard(self, offer_id: int, cutoff: datetime, now: datetime) -> bool:
        """Guarded ``pending → expired`` for a stale offer.

        The ``created_at < cutoff`` re-check rides the UPDATE so a
        concurrent :meth:`claim_for_accept` (opponent clicking at the
        deadline) wins cleanly: whichever statement runs first flips the
        status and the loser's guard fails. ``True`` iff this call did
        the expire (so the service refunds exactly once).

        ``cutoff`` is the deadline the row must predate; ``now`` is what
        goes into ``finished_at``. They are deliberately two arguments:
        stamping the cutoff (as this did before #267) back-dates the row
        by the whole TTL and desynchronises it from the refund's ledger
        row, which carries the real time. Prod offer #80 shows the
        symptom — ``finished_at=09:18:51`` against a refund at
        ``09:48:51``. Legacy stamped the sweep time (bot.py:14812).
        """
        stmt = (
            update(PvpOffer)
            .where(
                PvpOffer.id == offer_id,
                PvpOffer.status == OFFER_PENDING,
                PvpOffer.opponent_id.is_(None),
                PvpOffer.created_at < cutoff,
            )
            .values(status=OFFER_EXPIRED, finished_at=now)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Escrow
    # ------------------------------------------------------------------

    async def hold_escrow(
        self, *, offer_id: int, user_id: int, amount: int, now: datetime
    ) -> PvpEscrow:
        """Insert a ``held`` escrow row for one seat (legacy bot.py:14750).

        The caller has ALREADY won the matching atomic
        :meth:`EconomyRepo.debit` for ``amount`` in this same
        transaction — this row records the hold so the expiry/payout
        paths know how much to release/refund.
        """
        row = PvpEscrow(
            offer_id=offer_id,
            user_id=user_id,
            amount=amount,
            status=ESCROW_HELD,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def release_all(self, offer_id: int) -> None:
        """Flip every held escrow row for the offer to ``released``.

        Called on a resolved win after the winner's pot credit lands
        (legacy ``_pvp_payout`` flips ``pvp_escrow.status='released'``,
        bot.py:14794).
        """
        await self._session.execute(
            update(PvpEscrow)
            .where(PvpEscrow.offer_id == offer_id, PvpEscrow.status == ESCROW_HELD)
            .values(status=ESCROW_RELEASED)
        )

    async def refund_one(self, offer_id: int, user_id: int) -> None:
        """Flip one seat's held escrow row to ``refunded`` (tie / expiry).

        Legacy ``_pvp_refund`` flips a single user's hold
        (bot.py:14770). Called by the service after the matching refund
        credit lands.
        """
        await self._session.execute(
            update(PvpEscrow)
            .where(
                PvpEscrow.offer_id == offer_id,
                PvpEscrow.user_id == user_id,
                PvpEscrow.status == ESCROW_HELD,
            )
            .values(status=ESCROW_REFUNDED)
        )
