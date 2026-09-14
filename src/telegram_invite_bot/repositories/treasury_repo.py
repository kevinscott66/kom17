"""Group-treasury balance ops over ``economy.groups_donations`` (L-28/L-41).

The "казна" (treasury) of a group is the prod-existing
``groups_donations.total_donations`` column — NOT ``group_xp`` and NOT a
separate table. Verified against legacy:

* ``get_group_total_donations`` (bot.py:10914-10920) reads
  ``SELECT total_donations FROM groups_donations WHERE group_id=?`` and its
  own docstring calls it «Сумма монет в „казне" группы (legacy: для вывода
  владельцем)».
* ``group_treasury_pay`` (bot.py:10956-10982) debits the same column with a
  race guard: ``UPDATE groups_donations SET total_donations =
  total_donations - ? WHERE group_id = ? AND total_donations >= ?`` and
  checks ``cur.rowcount`` (bot.py:10969-10974).

So no schema change ships with this repository — the column already exists
in the prod dump (``docs/prod_schemas.sql:404``) and is mapped on
:class:`GroupDonationsAggregate`.

FUNDING discrepancy — the backlog and the code disagree, and the code wins:
backlog says treasury is "funded by splits of donations/economy flows".
That was true in an older era; CURRENT legacy does NOT fund it anywhere.
``get_group_total_donations``'s docstring states it plainly («Сейчас не
пополняется — донаты идут во владельца и в очки группы», bot.py:10915), and
both write paths confirm it: ``donations_do_donate`` (bot.py:10687) and the
shop-purchase split ``donation_from_purchase`` (bot.py:10715-10760, the
``PURCHASE_DONATION_TO_GROUP_PERCENT`` cut) update ``group_xp`` + credit the
group OWNER's wallet (minus dev commission) — neither touches
``total_donations``. This repo therefore implements NO funding write-side;
it only reads / debits (and compensating-credits) the residual balances
groups accumulated before the funding flow was retired.

Session is shared with the caller; the repo flushes implicitly via execute
and never commits — the handler owns commit/rollback (same contract as
``DonationsRatingRepo``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, select, update

from telegram_invite_bot.db.models.economy import GroupDonationsAggregate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class TreasuryRepo:
    """``groups_donations.total_donations`` read + race-safe debit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_balance(self, group_id: int) -> int | None:
        """Current treasury balance, or ``None`` when the group has no
        ``groups_donations`` row at all.

        ``None`` vs ``0`` matters to the caller the same way it does in
        legacy: a missing row and a zero balance both reject the payout,
        but the distinction keeps the boundary typed (mirrors
        ``_fetch_aggregate`` in ``handlers/groupstats.py``). A NULL column
        on an existing row collapses to 0 — legacy's reader does the same
        (``row[0] is not None`` guard, bot.py:10917-10918).
        """
        result = await self._session.execute(
            select(GroupDonationsAggregate.total_donations).where(
                GroupDonationsAggregate.group_id == group_id
            )
        )
        row = result.first()
        if row is None:
            return None
        return int(row[0] or 0)

    async def debit(self, group_id: int, amount: int) -> bool:
        """Race-safe treasury debit. ``True`` iff a row was updated.

        Mirrors legacy's guard verbatim (bot.py:10969-10974): the
        ``total_donations >= :amount`` predicate lives in the WHERE
        clause so a concurrent payout that drains the treasury between
        the caller's balance pre-check and this UPDATE collapses to
        rowcount=0 (→ insufficient funds) instead of driving the column
        negative. A NULL ``total_donations`` fails the comparison and
        also rejects — same as legacy.
        """
        if amount <= 0:
            return False
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(GroupDonationsAggregate)
                .where(
                    GroupDonationsAggregate.group_id == group_id,
                    GroupDonationsAggregate.total_donations >= amount,
                )
                .values(total_donations=GroupDonationsAggregate.total_donations - amount)
            ),
        )
        return result.rowcount > 0

    async def credit(self, group_id: int, amount: int) -> bool:
        """Compensating credit back into the treasury. ``True`` iff a row
        was updated.

        DELIBERATELY UNUSED in ``src/``. The live repair for the
        multi-step failure it was written for (treasury debited,
        owner-wallet credit failed) is the savepoint rollback at
        ``services/treasury_service.py:154-162``, which already restores
        ``total_donations``. Calling this next to that rollback would
        credit the amount a SECOND time — a failed payout would MINT
        coins into the treasury, once per failure.

        Kept rather than deleted because a future flow may need a real
        compensating credit, and because
        ``tests/integration/repositories/test_treasury_repo.py:138``
        pins its refusal on a non-existent row. There is deliberately no
        public funding flow (see the module docstring).
        ``COALESCE``-free because the debit that preceded it proves the
        column is non-NULL.
        """
        if amount <= 0:
            return False
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(GroupDonationsAggregate)
                .where(GroupDonationsAggregate.group_id == group_id)
                .values(total_donations=GroupDonationsAggregate.total_donations + amount)
            ),
        )
        return result.rowcount > 0
