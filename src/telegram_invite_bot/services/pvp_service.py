"""Atomic money composition for the PvP escrow stake games (AUD-2).

Ports legacy ``pvp_create_offer`` / ``pvp_accept_and_resolve`` /
``_pvp_payout`` / ``_pvp_refund`` (bot.py:14739-14976) to the new
pipeline, hardened with the project's escrow-on-create + atomic
status-claim posture (same as :class:`P2pService` / :class:`DuelService`).

Runs :class:`PvpRepo` + :class:`EconomyRepo` + :class:`TransactionsRepo`
on ONE shared ``economy`` session so every flow commits atomically:
the offer row, the escrow rows, the wallet moves and the ledger rows
either all land or all roll back.

Money invariants (the no-mint / no-loss guarantees):

* **Escrow-on-create** — the creator's stake is *held*
  (``EconomyRepo.hold``: the balance column only, lifetime counters
  untouched) and a ``held`` escrow row written before the offer is
  published.
* **Atomic accept** — :meth:`PvpRepo.claim_for_accept` (status-guarded
  UPDATE) means two opponents can't both win the same offer; the loser
  gets ``rowcount == 0`` and aborts. If the opponent's hold fails right
  after the claim, the offer is reverted to ``pending`` (the creator's
  hold stays — someone else can still accept).
* **Every hold()/release()/credit() return is checked** (the money-guard),
  and checked with a real ``if`` that logs the ids and amounts before
  it raises — never a bare ``assert`` (#263).
* **Pot = 2×bet**, of which the winner collects ``1.9×bet`` and the
  remainder is burned (T-020/R8, :func:`~telegram_invite_bot.games.pot.split_pot`);
  a dice tie refunds both stakes and takes no rake.
* **Lifetime counters move once, at settlement** (#1559) — the escrow
  legs are hold/release, so create→cancel, create→expire and a dice tie
  leave ``total_spent``/``total_earned`` exactly where they were. Only a
  decided game settles both stakes as a genuine spend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.games.limits import MAX_BET as _MAX_BET
from telegram_invite_bot.games.limits import MIN_BET as _MIN_BET
from telegram_invite_bot.games.pot import split_pot
from telegram_invite_bot.games.pvp import (
    CREATOR,
    CoinResult,
    DiceResult,
    resolve_coin,
    resolve_dice,
)
from telegram_invite_bot.repositories.pvp_repo import PvpRepo

if TYPE_CHECKING:
    import random

    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo

# Bet bounds — re-exported from the shared ceiling (T-020/R9) rather
# than re-declared, so "pinned to the other stake games" is enforced by
# the import instead of by a comment that can go stale. Kept as
# module-level names because handlers and tests import them from here.
_log = logger.bind(component="services.pvp")

MIN_BET = _MIN_BET
MAX_BET = _MAX_BET

# #267: legacy ``PVP_OFFER_TTL_SEC = 600`` (bot.py:3782) — ten minutes.
# Mirrored here so a ``PvpService`` built without the settings knob
# (tests, the economy DI middleware) still uses the legacy deadline
# rather than the 30-minute P2P one it used to inherit.
DEFAULT_OFFER_TTL_MINUTES = 10

# #1619: how many stale offers one expiry pass may refund.
#
# The pass runs every 60 seconds (``economy_cleanup``), so this is a
# throughput of 200 refunds a minute against a table that holds 80 rows
# in production — the cap is not a rate limit, it is a ceiling on the
# residue. :meth:`PvpService.expire` leaves an offer ``pending`` when
# its refund does not apply, and nothing ever retires such an offer, so
# the head of the ``id ASC`` scan can accumulate rows that will be
# selected, attempted and logged on every pass for as long as the unit
# runs. Without a ceiling that residue sets the size of every pass.
_EXPIRY_SCAN_LIMIT = 200


class PvpCreateOutcome(StrEnum):
    OK = "ok"
    INVALID_BET = "invalid_bet"
    NO_WALLET = "no_wallet"
    INSUFFICIENT_FUNDS = "insufficient_funds"


class PvpAcceptOutcome(StrEnum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    ALREADY_TAKEN = "already_taken"
    CREATOR_CANNOT_ACCEPT = "creator_cannot_accept"
    NO_WALLET = "no_wallet"
    INSUFFICIENT_FUNDS = "insufficient_funds"


@dataclass(frozen=True, slots=True)
class PvpCreateResult:
    outcome: PvpCreateOutcome
    offer_id: int | None = None
    balance_after: int | None = None


@dataclass(frozen=True, slots=True)
class PvpAcceptResult:
    outcome: PvpAcceptOutcome
    game: str = ""
    bet: int = 0
    creator_id: int = 0
    opponent_id: int = 0
    winner_id: int | None = None  # None ⇒ tie (dice only)
    coin: CoinResult | None = None
    dice: DiceResult | None = None
    # T-020/R8. Carried on the result rather than re-derived by the
    # handler so the card can never quote a pot the wallet didn't get:
    # ``payout`` is what the winner was actually credited, ``rake`` the
    # burned remainder. Both ``0`` on a tie (stakes refunded in full).
    payout: int = 0
    rake: int = 0
    expired: ExpiredOffer | None = None
    """Set on the ``NOT_FOUND`` this call's own lazy expiry produced (#2020).

    The lazy path below retires an offer and refunds its stake, and
    :meth:`expire` hands back the retired row — which is the only carrier
    of the ``chat_id``/``message_id`` the challenge card lives at. Left
    on the floor, that card kept its «Accept» keyboard forever: the
    sweeper's :meth:`sweep_expired` only ever sees offers it retires
    itself, and ``expire_guard`` retires each one exactly once, so an
    offer closed here can never reach the pass that closes cards.

    ``None`` on every other ``NOT_FOUND`` — a card from another chat, an
    offer that was already ``expired``, one that never existed. Only the
    caller whose tap did the retiring owns the edit, for the same reason
    it owns the refund.
    """


@dataclass(frozen=True, slots=True)
class ExpiredOffer:
    """One offer :meth:`PvpService.expire` retired and refunded (#1766)."""

    offer_id: int
    creator_id: int
    bet: int
    chat_id: int
    message_id: int | None
    """The published challenge card, when the handler managed to pin one
    (:meth:`PvpService.set_offer_message` is best-effort). ``None`` means
    there is no card to close — not an error, just nothing to do."""


@dataclass(frozen=True, slots=True)
class PvpSweepReport:
    """Expiry-pass result, for observability + the card fix-up (#1766).

    Same shape as :class:`~telegram_invite_bot.services.p2p_service.SweepReport`
    and for the same reason: the money move and the Telegram edit that
    announces it cannot share a transaction, so the pass hands its caller
    what it retired and the caller does the announcing once the session
    has closed.
    """

    expired: tuple[ExpiredOffer, ...] = field(default=())

    @property
    def count(self) -> int:
        return len(self.expired)


class PvpService:
    """Atomic PvP offer creation, accept-and-resolve, cancel and expiry."""

    def __init__(
        self,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        session: AsyncSession,
        *,
        offer_ttl_minutes: int = DEFAULT_OFFER_TTL_MINUTES,
    ) -> None:
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._session = session
        self._pvp = PvpRepo(session)
        # #267: how long an unanswered challenge holds the creator's
        # stake. Keyword-only with the legacy default so the ~15 call
        # sites that don't care (tests, the DI middleware) stay valid.
        self._offer_ttl = timedelta(minutes=offer_ttl_minutes)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_offer(
        self,
        *,
        creator_id: int,
        game: str,
        bet: int,
        side: str | None,
        chat_id: int | None,
        now: datetime,
    ) -> PvpCreateResult:
        """Validate, escrow the creator's stake, and insert a pending offer."""
        if bet < MIN_BET or bet > MAX_BET:
            return PvpCreateResult(outcome=PvpCreateOutcome.INVALID_BET)
        wallet = await self._economy.get(creator_id)
        if wallet is None:
            return PvpCreateResult(outcome=PvpCreateOutcome.NO_WALLET)
        if wallet.balance < bet:
            return PvpCreateResult(outcome=PvpCreateOutcome.INSUFFICIENT_FUNDS)

        escrowed = await self._economy.hold(creator_id, bet)
        if escrowed is None:
            return PvpCreateResult(outcome=PvpCreateOutcome.INSUFFICIENT_FUNDS)

        # ``params_json`` is NOT NULL in the prod schema — store an empty
        # object for /pvp_dice (which has no creator choice) rather than NULL.
        params = json.dumps({"side": side}) if side is not None else "{}"
        offer = await self._pvp.create_offer(
            game=game,
            creator_id=creator_id,
            bet=bet,
            params_json=params,
            chat_id=chat_id,
            now=now,
        )
        await self._pvp.hold_escrow(offer_id=offer.id, user_id=creator_id, amount=bet, now=now)
        await self._ledger.record(
            from_id=creator_id,
            to_id=None,
            amount=bet,
            reason=f"pvp {game}: escrow hold (offer #{offer.id})",
            type="pvp_hold",
        )
        return PvpCreateResult(
            outcome=PvpCreateOutcome.OK,
            offer_id=offer.id,
            balance_after=escrowed.balance,
        )

    async def set_offer_message(self, offer_id: int, chat_id: int, message_id: int) -> None:
        """Pin the published group card onto the offer (best-effort)."""
        await self._pvp.set_message(offer_id, chat_id, message_id)

    # ------------------------------------------------------------------
    # Accept + resolve
    # ------------------------------------------------------------------

    async def accept_and_resolve(
        self,
        *,
        offer_id: int,
        opponent_id: int,
        chat_id: int,
        now: datetime,
        rng: random.Random | None = None,
    ) -> PvpAcceptResult:
        """Claim a pending offer, escrow the opponent, resolve and pay out.

        The whole method runs in one transaction; the caller commits.

        ``chat_id`` is the chat the Accept tap arrived in. It is
        REQUIRED rather than defaulted (#1604): the offer row has
        carried the chat it was published in since it was written,
        but nothing ever read it back, so an offer was acceptable
        from any chat its card reached. Telegram keeps an inline
        keyboard live across a forward (the same behaviour recorded
        at ``handlers/admin/withdrawals.py``), so a challenge
        forwarded out of its group could be taken by someone who
        never saw the original — and, because the forwarded card
        belongs to the forwarder, the result was announced in the
        wrong chat while the original card kept showing an open
        Accept button until somebody tapped it.

        Nobody was robbed: the acceptor stakes their own coins and
        the payout is symmetric. This is a missing SCOPE, not a
        missing authorization. A default here would reintroduce it
        exactly — the caller that forgets is the caller with the bug.
        """
        offer = await self._pvp.get_offer(offer_id)
        if offer is None:
            return PvpAcceptResult(outcome=PvpAcceptOutcome.NOT_FOUND)
        if offer.chat_id != chat_id:
            # Deliberately BEFORE the lazy expiry below: a tap from a
            # chat the offer was never published in must not drive
            # any state change at all, not even an expiry the sweeper
            # would perform anyway. ``NOT_FOUND`` is also the honest
            # answer to give the tapper — telling them the offer
            # exists somewhere else is information they never had.
            return PvpAcceptResult(outcome=PvpAcceptOutcome.NOT_FOUND)
        # #267: lazy on-access expiry, the port of legacy's
        # ``pvp_expire_offers()`` call at the top of accept
        # (bot.py:14905). Without it the ONLY thing that could expire a
        # challenge was the sweeper (hourly when this was written,
        # once a minute since #268), so a dead offer stayed
        # clickable — and, worse, still ACCEPTABLE — for up to 90
        # minutes. Legacy scanned every stale offer here; scanning the
        # one offer the clicker named does the same job for the hole
        # that matters (the rest still fall to the sweeper) without
        # putting a table scan on the hot path.
        #
        # ``created_at`` is nullable, and a NULL is deliberately NOT
        # stale: the repo guards (``expire_guard``, the sweep's WHERE)
        # both compare ``created_at < cutoff`` in SQL, where NULL yields
        # false. Treating it as stale here would make the lazy path
        # reject an offer the guard then refuses to expire — the stake
        # would be frozen with no way to release it.
        created_at = offer.created_at
        if (
            offer.status == "pending"
            and created_at is not None
            and created_at < now - self._offer_ttl
        ):
            retired = await self.expire(offer_id=offer_id, cutoff=now - self._offer_ttl, now=now)
            return PvpAcceptResult(outcome=PvpAcceptOutcome.NOT_FOUND, expired=retired)
        if offer.status != "pending":
            return PvpAcceptResult(outcome=PvpAcceptOutcome.NOT_FOUND)
        if offer.creator_id == opponent_id:
            return PvpAcceptResult(outcome=PvpAcceptOutcome.CREATOR_CANNOT_ACCEPT)

        bet = offer.bet
        game = offer.game
        creator_id = offer.creator_id

        wallet = await self._economy.get(opponent_id)
        if wallet is None:
            return PvpAcceptResult(outcome=PvpAcceptOutcome.NO_WALLET)
        if wallet.balance < bet:
            return PvpAcceptResult(outcome=PvpAcceptOutcome.INSUFFICIENT_FUNDS)

        # Atomic claim — the double-accept guard. Loser sees rowcount 0.
        if not await self._pvp.claim_for_accept(offer_id, opponent_id, now):
            return PvpAcceptResult(outcome=PvpAcceptOutcome.ALREADY_TAKEN)

        escrowed = await self._economy.hold(opponent_id, bet)
        if escrowed is None:
            # Opponent drained between the pre-check and the hold — undo
            # the claim so the (still-held) offer can be accepted again.
            await self._pvp.revert_accept(offer_id)
            return PvpAcceptResult(outcome=PvpAcceptOutcome.INSUFFICIENT_FUNDS)
        await self._pvp.hold_escrow(offer_id=offer_id, user_id=opponent_id, amount=bet, now=now)
        # The opponent's stake leaves their wallet exactly like the
        # creator's did at ``create_offer`` — so it gets the same row.
        # Without it the tie path below credits them a refund with no
        # matching debit anywhere in the ledger, and the ``/profile``
        # finances panel shows a draw as income.
        await self._ledger.record(
            from_id=opponent_id,
            to_id=None,
            amount=bet,
            reason=f"pvp {game}: escrow hold (offer #{offer_id})",
            type="pvp_hold",
        )

        # Resolve (pure). ``game`` is the legacy offer type "coin"/"dice".
        stat_label = f"pvp_{game}"  # stable /chatstats label: pvp_coin / pvp_dice
        coin: CoinResult | None = None
        dice: DiceResult | None = None
        if game == "coin":
            side = json.loads(offer.params_json or "{}").get("side") or "heads"
            coin = resolve_coin(side, rng=rng)
            winner_seat: int | None = coin.winner
        else:  # dice
            dice = resolve_dice(rng=rng)
            winner_seat = dice.winner

        # T-020/R8 — the pot is still ``2 × bet``, but the winner no
        # longer collects all of it. Same split as /duel and /cpc so a
        # player can't shop the three PvP commands for the best odds.
        payout, rake = split_pot(bet)
        if winner_seat is None:
            # Dice tie — refund both stakes.
            refunded_c = await self._economy.release(creator_id, bet)
            refunded_o = await self._economy.release(opponent_id, bet)
            if refunded_c is None or refunded_o is None:
                # #263: this was a bare ``assert``. The prod unit runs
                # WITHOUT ``-O``, so asserts are live — the statement was
                # a crash, not a no-op. Defensive: both wallets were
                # held for ``bet`` a few lines up, so releasing ``bet``
                # back can neither overflow the ceiling nor find the row
                # missing. Raising (not returning) is deliberate: the
                # accept is one transaction the CALLER commits, so only
                # an exception undoes the two escrow holds.
                msg = f"pvp tie refund failed (offer #{offer_id})"
                _log.bind(
                    offer_id=offer_id,
                    creator_id=creator_id,
                    opponent_id=opponent_id,
                    bet=bet,
                ).error(msg)
                raise RuntimeError(msg)
            await self._pvp.refund_one(offer_id, creator_id)
            await self._pvp.refund_one(offer_id, opponent_id)
            await self._economy.record_game(
                creator_id, game=stat_label, bet=bet, won=False, profit=0
            )
            await self._economy.record_game(
                opponent_id, game=stat_label, bet=bet, won=False, profit=0
            )
            await self._ledger.record(
                from_id=None,
                to_id=creator_id,
                amount=bet,
                reason=f"pvp {game}: tie refund (offer #{offer_id})",
                type="pvp_refund",
            )
            await self._ledger.record(
                from_id=None,
                to_id=opponent_id,
                amount=bet,
                reason=f"pvp {game}: tie refund (offer #{offer_id})",
                type="pvp_refund",
            )
            winner_id: int | None = None
            # A tie is a non-event — both stakes go straight back and
            # the house takes nothing. Charging for an outcome neither
            # player chose is the one part of R8 a player could fairly
            # call unfair. Same posture as /duel and /cpc.
            payout, rake = 0, 0
        else:
            winner_id = creator_id if winner_seat == CREATOR else opponent_id
            loser_id = opponent_id if winner_seat == CREATOR else creator_id
            credited = await self._economy.credit(winner_id, payout)
            if credited is None:
                # #263: was a bare ``assert``. Same reasoning as the tie
                # refund above — the prod unit runs without ``-O``, so it
                # was a live crash that recorded nothing. The winner's
                # wallet existed when they created or accepted the offer,
                # so reaching here means the row went away mid-flow; the
                # caller's rollback then releases both escrow holds, which
                # is the outcome we want, but only if we raise.
                msg = f"pvp payout to winner failed (offer #{offer_id})"
                _log.bind(
                    offer_id=offer_id,
                    winner_id=winner_id,
                    loser_id=loser_id,
                    bet=bet,
                    amount=payout,
                    rake=rake,
                ).error(msg)
                raise RuntimeError(msg)
            # ``release_all`` clears BOTH holds — the winner's own stake
            # is inside ``payout``, so releasing the escrow rows after
            # crediting is what makes the seats net out correctly.
            await self._pvp.release_all(offer_id)
            # #1559: the two stakes were HELD, not spent — the escrow
            # legs above deliberately leave the lifetime counters alone
            # so that a create/cancel round trip, an expiry or a dice
            # tie moves nothing. A decided game is where the pot is
            # genuinely consumed, so both seats settle their hold as a
            # real spend here — the same three-leg pattern #1546 gave
            # /marry_extend, and the same net counters the old
            # debit-at-escrow code produced for this branch.
            # ``bump_totals`` is called on the repo directly because
            # ``PvpService`` holds an ``EconomyRepo``, not the service
            # that owns ``settle_hold``; it moves no coins, which is
            # why it is deliberately outside the money-guard sweep.
            # A failure here is bookkeeping-only (the coins are already
            # correct), so it is logged rather than raised: undoing a
            # decided game over a counter would be the worse outcome.
            for settled_id in (creator_id, opponent_id):
                if await self._economy.bump_totals(settled_id, spent=bet) is None:
                    _log.bind(offer_id=offer_id, user_id=settled_id, bet=bet).error(
                        "pvp stake settlement failed; lifetime counters not moved"
                    )
            await self._economy.record_game(
                winner_id, game=stat_label, bet=bet, won=True, profit=payout - bet
            )
            await self._economy.record_game(
                loser_id, game=stat_label, bet=bet, won=False, profit=-bet
            )
            # ``from_id`` is NULL for the same reason the rake row
            # below is: the payout comes out of the pot, and the pot is
            # not a wallet. The loser never sent it — they sent their
            # stake, and their ``pvp_hold`` row already books that.
            # Naming them here charged them the whole pot on top of
            # their stake in every aggregate that sums by ``from_id``.
            await self._ledger.record(
                from_id=None,
                to_id=winner_id,
                amount=payout,
                reason=f"pvp {game}: pot {bet * 2} (offer #{offer_id})",
                type="pvp_win",
            )
            # T-020/R8 — the burned slice. ``from_id``/``to_id`` are
            # NULL on purpose: the burn belongs to neither wallet, and
            # charging it to the loser would double-count against them
            # (the escrow already took their whole stake) and skew any
            # per-user aggregate that sums by ``from_id`` untyped.
            # Guarded on the amount so a restored 2× multiplier writes
            # no zero-value noise row.
            if rake > 0:
                await self._ledger.record(
                    from_id=None,
                    to_id=None,
                    amount=rake,
                    reason=f"pvp {game}: rake (offer #{offer_id})",
                    type="pvp_rake",
                )

        result_json = json.dumps(
            {
                "winner_seat": winner_seat,
                "flip": coin.flip if coin else None,
                "creator_roll": dice.creator_roll if dice else None,
                "opponent_roll": dice.opponent_roll if dice else None,
            }
        )
        await self._pvp.finish(offer_id, result_json, now)

        return PvpAcceptResult(
            outcome=PvpAcceptOutcome.SUCCESS,
            game=game,
            bet=bet,
            creator_id=creator_id,
            opponent_id=opponent_id,
            winner_id=winner_id,
            coin=coin,
            dice=dice,
            payout=payout,
            rake=rake,
        )

    # ------------------------------------------------------------------
    # Cancel + expire (refund the creator's single held stake)
    # ------------------------------------------------------------------

    async def cancel(self, *, offer_id: int, creator_id: int) -> bool:
        """Cancel the creator's own pending offer and refund the hold.

        Returns ``False`` when the guard rejects (not found / not yours /
        already accepted/cancelled/expired).
        """
        # #263: the status flip and the refund go under ONE savepoint.
        # ``cancel_guard`` moves the offer to ``cancelled`` BEFORE the
        # release, so a failed release outside a savepoint would leave a
        # cancelled offer whose stake is still ``held`` — the coins
        # would simply vanish. Same shape as TreasuryService.payout.
        async with self._session.begin_nested() as savepoint:
            bet = await self._pvp.cancel_guard(offer_id, creator_id)
            if bet is None:
                return False
            refunded = await self._economy.release(creator_id, bet)
            if refunded is None:
                await savepoint.rollback()
                _log.bind(offer_id=offer_id, creator_id=creator_id, bet=bet).error(
                    "pvp cancel refund failed; offer left pending"
                )
                return False
            await self._pvp.refund_one(offer_id, creator_id)
            # #1707: the ledger row belongs INSIDE the savepoint that
            # made the refund, not after it. ``P2pService.expire_pending``
            # has always written its refund row this way; this one did
            # not, so a failing INSERT here left a released savepoint
            # holding a real credit with nothing in ``transactions`` to
            # explain it. Cheap here (the caller's rollback would still
            # cover it), load-bearing in :meth:`expire` — see there.
            await self._ledger.record(
                from_id=None,
                to_id=creator_id,
                amount=bet,
                reason=f"pvp cancel: refund (offer #{offer_id})",
                type="pvp_refund",
            )
        return True

    async def sweep_expired(
        self, now: datetime, *, ttl_minutes: int | None = None
    ) -> PvpSweepReport:
        """Expire + refund every pending offer older than the TTL.

        Driven by the :class:`EconomyCleanupSweeper`. Reports the offers
        actually expired (the per-offer ``expire_guard`` makes a
        concurrent accept at the deadline win cleanly).

        #1766: the report carries each offer's card coordinates, not just
        a count. The refund closes the money side, but the challenge card
        published in the group keeps its «Accept» button — and this
        service has no ``Bot`` and shares a session it must not hold open
        across Telegram I/O. So it reports; the sweeper edits.

        ``ttl_minutes`` overrides the instance TTL; it exists for tests
        that want an explicit deadline. Production passes nothing and
        gets ``PVP_OFFER_TTL_MINUTES`` (#267).

        #268: one bad offer must not take the pass down with it. Each
        offer is its own savepoint (inside :meth:`expire`) AND its own
        try/except here, because ``list_pending_older_than`` orders by
        ``id ASC``: a permanently-failing offer sits at the head of the
        scan forever, so without this every later offer's refund is
        collateral damage on every pass, for good.

        #1619: that same residue is why the scan is now capped at
        ``_EXPIRY_SCAN_LIMIT``. #268 stopped it from poisoning the
        pass; the cap stops it from SIZING the pass. Neither retires
        the offer — it stays ``pending``, is re-selected next minute,
        and logs its ERROR again. A terminal status for an offer whose
        refund can never apply is a product decision and has its own
        ticket.
        """
        ttl = self._offer_ttl if ttl_minutes is None else timedelta(minutes=ttl_minutes)
        cutoff = now - ttl
        offers = await self._pvp.list_pending_older_than(cutoff, limit=_EXPIRY_SCAN_LIMIT)
        expired: list[ExpiredOffer] = []
        for offer in offers:
            # #1707: read the id ONCE, before the try. A savepoint that
            # rolls back expires the attributes of every ORM object it
            # touched, so ``offer.id`` inside the handler is a lazy
            # refresh — synchronous IO on an async session, which raises
            # ``MissingGreenlet`` FROM the handler and takes down the
            # very pass this ``except`` exists to protect.
            offer_id = offer.id
            try:
                retired = await self.expire(offer_id=offer_id, cutoff=cutoff, now=now)
                if retired is not None:
                    expired.append(retired)
            except Exception:  # noqa: BLE001 — one poisoned offer, not the pass
                _log.bind(offer_id=offer_id).exception("pvp expire failed; skipping this offer")
        return PvpSweepReport(expired=tuple(expired))

    async def expire(
        self, *, offer_id: int, cutoff: datetime, now: datetime
    ) -> ExpiredOffer | None:
        """Expire a stale pending offer and refund the creator's hold.

        Returns the retired offer iff THIS call did the expire (so the
        refund runs exactly once even under a concurrent accept at the
        deadline), and ``None`` on every path that did not move coins —
        which is also what keeps a still-``pending`` offer out of the
        sweep report, and therefore keeps its live card untouched.

        ``cutoff`` is the deadline the offer must predate; ``now`` is the
        real time, stamped into ``finished_at`` so the row agrees with
        the ledger row written below (#267).
        """
        offer = await self._pvp.get_offer(offer_id)
        if offer is None:
            return None
        # #1707/#1766: hoist EVERY scalar the caller will need before the
        # savepoint. A rollback expires the attributes of every ORM
        # object the savepoint touched, so reading ``offer.chat_id``
        # afterwards would be a lazy refresh — synchronous IO on an async
        # session, i.e. ``MissingGreenlet`` out of the very branch that
        # exists to survive a failure.
        creator_id, bet = offer.creator_id, offer.bet
        chat_id, message_id = offer.chat_id, offer.message_id
        # #263/#268: the guard flips ``pending → expired`` BEFORE the
        # release, so the two MUST be one atomic unit. Splitting the
        # sweep per offer without this savepoint would be strictly worse
        # than the old all-or-nothing pass: a failed refund would strand
        # the stake as an ``expired`` offer with a ``held`` escrow.
        async with self._session.begin_nested() as savepoint:
            if not await self._pvp.expire_guard(offer_id, cutoff, now):
                return None
            refunded = await self._economy.release(creator_id, bet)
            if refunded is None:
                # Was a bare ``assert`` — live in prod (no ``-O``), and
                # therefore a deterministic poison pill that killed the
                # whole cleanup pass on the FIRST offer whose creator
                # wallet had vanished or hit the balance ceiling.
                await savepoint.rollback()
                _log.bind(offer_id=offer_id, creator_id=creator_id, bet=bet).error(
                    "pvp expire refund failed; offer left pending"
                )
                return None
            await self._pvp.refund_one(offer_id, creator_id)
            # #1707: this one is the reason the pair moved. The sweep
            # calls ``expire`` inside a ``try/except Exception`` (#268)
            # and the session it shares COMMITS on clean exit, so a
            # ledger INSERT that raised out here was logged, swallowed,
            # and then committed anyway — refunded coins with no
            # ``transactions`` row, silently, once per poisoned offer.
            # Under the savepoint the whole offer is undone instead and
            # it stays ``pending`` for the next pass, which is the same
            # outcome the release-failure branch above already chooses.
            await self._ledger.record(
                from_id=None,
                to_id=creator_id,
                amount=bet,
                reason=f"pvp expire: refund (offer #{offer_id})",
                type="pvp_refund",
            )
        return ExpiredOffer(
            offer_id=offer_id,
            creator_id=creator_id,
            bet=bet,
            chat_id=chat_id,
            message_id=message_id,
        )
