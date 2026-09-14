"""ORM mappings for the PvP escrow stake games (AUD-2).

Two tables on the ``economy`` metadata, kept in THIS module (not the
shared ``db/models/economy.py``) so the PvP feature never forces an
edit to the file both pipelines' economy models share — same posture as
:mod:`telegram_invite_bot.db.models.p2p` /
:mod:`telegram_invite_bot.db.models.promo`.

Ports the legacy ``/pvp_coin`` / ``/pvp_dice`` escrow games
(bot.py:20906 / bot.py:20867 + the ``pvp_create_offer`` /
``pvp_accept_and_resolve`` core at bot.py:14844-14960). Legacy kept this
state in a ``pvp_offers`` / ``pvp_escrow`` pair too (bot.py:14752,
14844), and that path WAS deployed: prod carries 80 offers and 93
escrow holds written by three players between 2026-03-17 and
2026-06-15 (nothing stranded — the held stakes sum to 0). Migration
``0012_pvp_stake_games`` therefore only adopts the tables — it creates
each object solely when absent and its ``downgrade`` is a no-op, since
dropping them would destroy history no upgrade can rebuild.
``create_all`` builds them for tests directly.

Money semantics (mirrors legacy + the P2P escrow-on-create posture):

* **Escrow-on-create** — the creator's stake is debited and an
  ``pvp_escrow`` row (``status='held'``) is written when the offer is
  published; the opponent's stake is debited + held when they accept.
* **Atomic accept** — the ``pending → active`` claim is a status-guarded
  UPDATE (:meth:`PvpRepo.claim_for_accept`); two near-simultaneous
  accepts can't both win, so the game can't double-resolve / double-pay.
* On resolve the pot (2×bet) is credited to the winner and both escrow
  rows flip to ``released``; a tie refunds both stakes and flips both to
  ``refunded``; a stale ``pending`` offer's single held stake is
  refunded by the expiry sweep (``refunded``).

Status vocabularies (TEXT, not enums — matches the legacy writer and the
P2P module's posture):

* offer: ``pending`` → ``active`` → ``finished`` | ``cancelled`` |
  ``expired``.
* escrow: ``held`` → ``released`` | ``refunded``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import EconomyBase


class PvpOffer(EconomyBase):
    """One published PvP challenge in ``economy.pvp_offers``.

    ``game`` is ``coin`` | ``dice``. ``params_json`` carries the coin
    side the creator chose for /pvp_coin (``{"side": "heads"}``). For
    /pvp_dice there is no creator choice, and prod holds two shapes for
    it: ``{"mode": "higher"}`` written by legacy (bot.py:20889, 60 rows)
    and ``{}`` written by the new pipeline (``services/pvp_service.py``,
    1 row). Readers must tolerate both — the column is NOT NULL, so
    neither is ever None.

    ``chat_id`` / ``message_id`` pin the published group card so the
    accept/expiry paths can edit it. ``result_json`` is the opaque
    resolution blob (flip side / both rolls + winner), written once at
    resolution for the audit/read side.
    """

    __tablename__ = "pvp_offers"

    # Python attribute names stay descriptive (game/creator_id/opponent_id)
    # but map to the LEGACY prod column names (type/player1_id/player2_id) —
    # the prod ``pvp_offers`` table already exists from the old telebot with
    # those columns + real rows, so the new pipeline reads/writes the same
    # shape. ``game`` (=column ``type``) values are ``coin`` | ``dice``.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game: Mapped[str] = mapped_column("type", String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # NOT NULL in the prod schema — a PvP challenge is always created in
    # a group.
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    creator_id: Mapped[int] = mapped_column("player1_id", Integer, nullable=False)
    opponent_id: Mapped[int | None] = mapped_column("player2_id", Integer, nullable=True)
    bet: Mapped[int] = mapped_column(Integer, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Prod carries legacy's own indexes, so ``0012`` skips creating
    # these there — see its ``_covered`` helper. What follows is what a
    # fresh database gets, and it is NOT what production has (#1621,
    # schema read read-only):
    #   * ``idx_pvp_offers_player1`` here vs ``idx_pvp_offers_p1``
    #     there — different name, same column; prod also carries an
    #     ``idx_pvp_offers_p2`` (and an ``idx_pvp_offers_chat``) that
    #     nothing below declares;
    #   * ``idx_pvp_offers_status`` shares its NAME with prod's but not
    #     its shape: two columns here, ``(status)`` alone there. Since
    #     the name matches, no migration will ever reconcile them.
    # The consequence is small and worth knowing: the expiry scan's
    # ``created_at < cutoff`` half is unindexed in production, so it
    # filters the ``pending`` subset in memory (#1619 caps that scan).
    # Read plans off the production schema, never off this block.
    __table_args__ = (
        Index("idx_pvp_offers_player1", "player1_id"),
        # Backs the expiry scan (status='pending' AND created_at < cutoff).
        Index("idx_pvp_offers_status", "status", "created_at"),
    )


class PvpEscrow(EconomyBase):
    """One held stake in ``economy.pvp_escrow``.

    One row per seat: the creator's at publish-time, the opponent's at
    accept-time. ``status='held'`` ⇔ the matching wallet was debited in
    the same transaction (the escrow invariant). Exactly one terminal
    happens to it: ``released`` (paid into the winner's pot, or the
    creator's pot on a self-win) or ``refunded`` (tie / expiry).
    """

    __tablename__ = "pvp_escrow"

    # Follows the legacy prod ``pvp_escrow`` shape: NO surrogate ``id`` —
    # one hold per (offer, user), so (offer_id, user_id) is the natural
    # composite primary key. #1949: the two things that used to be
    # missing here — the real ``FOREIGN KEY (offer_id) REFERENCES
    # pvp_offers(id) ON DELETE CASCADE`` and ``created_at`` NOT NULL,
    # both of which prod declares (``docs/prod_schemas.sql:639``) — are
    # declared below, so a ``create_all`` database no longer accepts a
    # write prod would reject. Neither constrains an existing writer:
    # ``PvpService.create_offer`` inserts the offer before either hold,
    # and both ``PvpRepo`` writers already pass ``now``.
    offer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pvp_offers.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="held")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Redundant on any real database — ``offer_id`` leads the composite
    # primary key, whose implicit index already serves lookups by offer.
    # Prod has never had it (its only extra index is on ``status``) and no
    # migration creates it, so ``create_all`` databases carry one index
    # more than prod does — harmless, but a divergence, not a promise.
    __table_args__ = (Index("idx_pvp_escrow_offer", "offer_id"),)
