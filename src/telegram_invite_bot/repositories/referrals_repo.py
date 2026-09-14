"""Read-side repository for the single-level referral graph (L-37).

Legacy referrals are strictly one level deep: ``economy.users.referred_by``
points at the inviter who shared the ``ref_<id>`` deep-link, and the
commission writer tags ``economy.transactions`` rows with
``type='referral'`` (bot.py:9868). There is NO multi-level / grand-referral
commission in legacy — the "depth" the report surfaces is the *structural*
chain (who invited me, who I invited, who they invited) computed from the
same ``referred_by`` self-join, not a second commission tier.

This repo consolidates the four reads ``/referral`` and ``/referrals``
were issuing inline (invitees, lifetime earnings, display names) and adds
the chain/depth aggregation L-37 asks for:

* :meth:`fetch_inviter` — the row that referred *the caller* (level up).
* :meth:`fetch_invitees` — direct invitees + their wallet balances.
* :meth:`fetch_earnings` — lifetime referral-commission COM (``SUM`` over
  ``transactions WHERE to_id=? AND type='referral'``).
* :meth:`count_second_level` — how many users were invited by the caller's
  own invitees (the second ring of the chain). Read-only structural depth;
  no money attaches to it (legacy pays one level only).

All reads are against ``economy.db``. Display-name joins live in
``users.db`` and stay in the handler (cross-engine, same split
``handlers/referrals.py`` already used).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from telegram_invite_bot.db.models.economy import EconomyUser, Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Tag the legacy commission writer stamps on ``transactions.type``
# (bot.py:9868 ``transaction_type="referral"``).
_REFERRAL_TXN_TYPE = "referral"


@dataclass(frozen=True, slots=True)
class ReferralInvitee:
    """One direct invitee — wallet identity + current balance."""

    user_id: int
    balance: int


class ReferralsRepo:
    """``economy.users`` + ``economy.transactions`` referral reads.

    Constructed per call with an open ``economy`` session. Pure reads —
    no flush/commit here; the caller owns the session lifecycle.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_inviter(self, user_id: int) -> int | None:
        """Return the user_id that referred ``user_id``, or ``None``.

        ``SELECT referred_by FROM users WHERE user_id=?``. ``referred_by``
        is NULL for organically-joined users and for everyone who predates
        the column backfill — both render as "no inviter" upstream. A
        stored ``0`` (legacy sentinel for "unset" on some rows) is
        normalised to ``None`` so the report never links to user 0.
        """
        result = await self._session.execute(
            select(EconomyUser.referred_by).where(EconomyUser.user_id == user_id)
        )
        referred_by = result.scalar_one_or_none()
        if referred_by is None or int(referred_by) == 0:
            return None
        return int(referred_by)

    async def fetch_invitees(self, referrer_id: int) -> list[ReferralInvitee]:
        """Direct invitees of ``referrer_id`` ordered by ``user_id``.

        ``SELECT user_id, balance FROM users WHERE referred_by=? ORDER BY
        user_id`` — mirrors legacy ``get_referrals_list`` (bot.py:9826),
        which orders chronologically-ish (Telegram ids are monotonic) so
        the inviter's "I invited Alice first" mental model holds. No LIMIT:
        the caller needs the full count for the "total invited" line and
        slices for display itself.
        """
        result = await self._session.execute(
            select(EconomyUser.user_id, EconomyUser.balance)
            .where(EconomyUser.referred_by == referrer_id)
            .order_by(EconomyUser.user_id)
        )
        return [
            ReferralInvitee(user_id=int(uid), balance=int(balance or 0))
            for uid, balance in result.all()
        ]

    async def count_invitees(self, referrer_id: int) -> int:
        """How many users ``referrer_id`` invited directly.

        :meth:`fetch_invitees` deliberately has no LIMIT because the
        ``/referrals`` card lists the rows. The profile social panel only
        prints the number, and pulling every invitee wallet across the
        wire to call ``len()`` on it is a cost that grows with the bot's
        best inviters — exactly the users most likely to open that panel.
        """
        result = await self._session.execute(
            select(func.count()).where(EconomyUser.referred_by == referrer_id)
        )
        return int(result.scalar_one())

    async def fetch_earnings(self, referrer_id: int) -> int:
        """Lifetime referral-commission COM credited to ``referrer_id``.

        ``SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE to_id=?
        AND type='referral'``. COALESCE so a caller with zero commissions
        gets ``0`` rather than ``None`` (the value renders inline).
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.to_id == referrer_id,
                Transaction.type == _REFERRAL_TXN_TYPE,
            )
        )
        return int(result.scalar_one())

    async def count_second_level(self, referrer_id: int) -> int:
        """Count users invited *by the caller's own invitees* (ring 2).

        ``SELECT COUNT(*) FROM users WHERE referred_by IN (SELECT user_id
        FROM users WHERE referred_by=?)`` — the structural second level of
        the referral chain. Purely informational depth: legacy pays
        commission on the first level only, so no earnings attach here.

        Returns ``0`` when the caller has no invitees (the subquery is
        empty) — no special-casing needed, the IN-of-empty just matches
        nothing.
        """
        first_level = select(EconomyUser.user_id).where(EconomyUser.referred_by == referrer_id)
        result = await self._session.execute(
            select(func.count()).where(EconomyUser.referred_by.in_(first_level))
        )
        return int(result.scalar_one())
