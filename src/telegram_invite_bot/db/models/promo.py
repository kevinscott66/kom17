"""ORM mappings for the promo / gift-code subsystem (L-96).

A *promo code* is a redeemable string a developer mints that grants a
fixed number of coins to whoever redeems it, bounded by a ``max_uses``
cap and (optionally) a once-per-user constraint. It is the spiritual
successor to legacy "checks" but a deliberately separate, self-contained
feature: a check is funded out of a creator's wallet and pays variable
amounts; a promo code is minted by a developer (no funding wallet), pays
a flat ``reward_coins`` per redemption, and is the kind of thing you put
on a banner ("redeem GIFT100 for 100 coins").

Two NET-NEW tables on the ``economy`` metadata (absent from the prod
dump). They live in THIS module — not the shared
``db/models/economy.py`` — so the promo feature never forces an edit to
the file both pipelines' economy models share. Both bind to
:class:`EconomyBase` so ``Base.metadata.create_all`` (tests) and the
Alembic migration (``0007_promo_codes``) build them onto ``economy.db``
alongside the existing economy tables.

Money invariants the schema / repo SQL enforce, not Python:

* The global ``max_uses`` cap is enforced inside one guarded
  ``UPDATE ... WHERE used_count < max_uses`` that also increments
  ``used_count`` — so two concurrent redemptions of the last slot cannot
  both succeed (the loser sees ``rowcount == 0``).
* The per-user-once guard for ``per_user_once`` codes is a conditional
  ``INSERT ... SELECT ... WHERE NOT EXISTS`` against
  ``promo_redemptions`` (see
  :meth:`PromoRepo.insert_redemption_once`, ``promo_repo.py:157``; the
  service-level entry point is :meth:`PromoService.redeem`). We
  deliberately
  do NOT put a hard ``UNIQUE(code_id, user_id)`` constraint on the table
  because non-once codes (``per_user_once = 0``) legitimately let the
  same user redeem repeatedly, which such a constraint would block. The
  ``idx_promo_redemptions_code_user`` index makes the ``WHERE NOT
  EXISTS`` probe and the per-user count cheap.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import EconomyBase


class PromoCode(EconomyBase):
    """A mintable, redeemable gift code in ``economy.promo_codes`` (L-96).

    One row per code. ``code`` is uppercased at the service boundary and
    is ``UNIQUE`` so a re-mint of the same string is rejected at the DB,
    not just in Python.

    Counters / caps:

    * ``reward_coins`` — flat coins credited per successful redemption.
    * ``max_uses`` — global cap across ALL users; ``0`` means unlimited.
      The redeem guard re-checks ``used_count < max_uses`` inside the
      same atomic UPDATE that increments ``used_count``, so two
      concurrent redemptions of the last slot cannot both succeed.
    * ``used_count`` — running total of successful redemptions; bumped
      by the guarded UPDATE, never read-modify-written in Python.
    * ``per_user_once`` — when true, a user may redeem this code at most
      once (enforced by the conditional INSERT in
      :meth:`PromoRepo.insert_redemption_once`); when false the same
      user may redeem repeatedly until ``max_uses`` is hit.
    * ``active`` — soft on/off switch; an exhausted code (``used_count``
      reached ``max_uses``) is flipped to inactive by the redeem guard so
      a later lookup short-circuits to NOT_FOUND.

    ``created_by`` records the minting developer's user id for audit.
    """

    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    reward_coins: Mapped[int] = mapped_column(Integer, nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    per_user_once: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_promo_codes_code", "code"),)


class PromoRedemption(EconomyBase):
    """One redemption event in ``economy.promo_redemptions`` (L-96).

    Append-only: one row per successful redemption.

    For ``per_user_once`` codes the per-user guard is a conditional
    ``INSERT ... WHERE NOT EXISTS`` in
    :meth:`PromoRepo.insert_redemption_once` (NOT a
    hard UNIQUE constraint — that would block legitimate repeat
    redemptions of a non-once code). The conditional insert is run inside
    the same atomic transaction as the ``used_count`` bump and the coin
    credit, so a redemption that the guard rejects (a concurrent
    once-only double-redeem) leaves no row and the service rolls the bump
    back.

    The composite ``idx_promo_redemptions_code_user`` index backs both
    the ``WHERE NOT EXISTS`` probe (per-user-once) and the per-user
    redemption count.

    Schema source: NET-NEW (migration ``0007_promo_codes``).
    """

    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_coins: Mapped[int] = mapped_column(Integer, nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_promo_redemptions_code_user", "code_id", "user_id"),)
