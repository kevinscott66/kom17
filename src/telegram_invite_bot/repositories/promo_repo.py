"""Async repository for the promo / gift-code tables (L-96).

Surface mirrors :class:`ChecksRepo`'s posture: the load-bearing
:meth:`reserve_use` is a single guarded ``UPDATE ... WHERE ...`` whose
WHERE clause carries the race safety (active + ``used_count < max_uses``)
and whose ``rowcount`` is the success signal. The per-user-once guard is
a conditional ``INSERT ... SELECT ... WHERE NOT EXISTS`` so a concurrent
double-redeem of a once-only code cannot insert two rows.

Crediting the redeemer's wallet and writing the ledger row are the
SERVICE's job (it composes those with this repo over one shared session)
— the repo owns only the promo tables, same single-responsibility split
every other repo here follows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, func, select, text

from telegram_invite_bot.db.models.promo import PromoCode, PromoRedemption

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _iso(value: datetime) -> str:
    """Render a naive datetime as the ``YYYY-MM-DD HH:MM:SS`` string SQLite
    stores for the ORM ``DateTime`` columns — used when binding through a
    raw ``text()`` statement (which bypasses the ORM datetime adapter)."""
    return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


class PromoRepo:
    """``economy.promo_codes`` + ``economy.promo_redemptions`` access.

    Constructed per request with an open session shared with
    :class:`EconomyRepo` / :class:`TransactionsRepo` so a redemption
    (reserve-use + redemption-row + wallet credit + ledger) lands as one
    atomic transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_code(self, code: str) -> PromoCode | None:
        """Return the promo row for ``code`` (any active state) or ``None``.

        ``code`` is matched verbatim — the SERVICE uppercases / strips
        before calling, and codes are stored uppercased, so this is an
        exact-match indexed lookup.
        """
        result = await self._session.execute(
            select(PromoCode).where(PromoCode.code == code).limit(1)
        )
        return result.scalar_one_or_none()

    async def create_code(
        self,
        *,
        code: str,
        reward_coins: int,
        max_uses: int,
        per_user_once: bool,
        created_by: int | None,
        description: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """INSERT a new promo code and return its autoincrement id.

        Flushes (not commits) so the id is populated and a
        ``UNIQUE(code)`` violation surfaces here, inside the caller's
        transaction, rather than at the outer middleware commit —
        :meth:`PromoService.create_code` catches the
        :class:`~sqlalchemy.exc.IntegrityError` and reports DUPLICATE
        (#1943; before that it caught nothing and the collision reached
        the user as a crash).
        """
        now = now or datetime.now(UTC).replace(tzinfo=None)
        row = PromoCode(
            code=code,
            reward_coins=reward_coins,
            max_uses=max_uses,
            used_count=0,
            per_user_once=per_user_once,
            created_by=created_by,
            active=True,
            description=description,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def has_redeemed(self, code_id: int, user_id: int) -> bool:
        """``True`` iff ``user_id`` already has a redemption row for ``code_id``.

        A friendly fast pre-check so the common "you already redeemed
        this" case short-circuits before the reserve. It CAN race with a
        concurrent redemption from the same user — the authoritative
        per-user-once guard is the conditional insert in
        :meth:`insert_redemption_once`.
        """
        result = await self._session.execute(
            select(1)
            .select_from(PromoRedemption)
            .where(
                PromoRedemption.code_id == code_id,
                PromoRedemption.user_id == user_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def reserve_use(self, code_id: int, max_uses: int) -> bool:
        """Atomically reserve one use of a code. Returns success.

        The single guarded UPDATE:

        * increments ``used_count`` by one,
        * flips ``active`` to 0 when the code is now exhausted
          (``max_uses > 0 AND used_count + 1 >= max_uses``),
        * and only matches the row if it is still active and still under
          the cap (``max_uses = 0`` means unlimited).

        Two concurrent redemptions of the last slot cannot both succeed:
        the first commits ``used_count += 1`` and (if it was the last)
        flips ``active = 0``; the second's WHERE clause fails the
        ``used_count < max_uses`` / ``active`` guard and returns
        ``rowcount == 0`` (which the service maps to EXHAUSTED).

        Rendered as raw SQL (mirroring :meth:`ChecksRepo.claim_decrement`)
        because the ``active`` flip references the pre-increment column
        value in the same statement, clearer as literal SQL than a
        SQLAlchemy ``case()`` over bound columns.
        """
        stmt = text(
            "UPDATE promo_codes "
            "SET used_count = used_count + 1, "
            "    active = CASE "
            "        WHEN :max_uses > 0 AND used_count + 1 >= :max_uses "
            "        THEN 0 ELSE active END "
            "WHERE id = :id "
            "  AND active = 1 "
            "  AND (:max_uses = 0 OR used_count < :max_uses)"
        )
        result = cast(
            "CursorResult[object]",
            await self._session.execute(stmt, {"max_uses": max_uses, "id": code_id}),
        )
        return result.rowcount > 0

    async def insert_redemption_once(
        self, code_id: int, user_id: int, reward_coins: int, now: datetime
    ) -> bool:
        """Conditionally INSERT a redemption row IFF the user has none yet.

        ``INSERT ... SELECT ... WHERE NOT EXISTS`` — the per-user-once
        guard. Returns ``True`` iff a row was inserted (the user had not
        redeemed this code before). A concurrent double-redeem of a
        once-only code: at most one of the two conditional inserts can
        match (SQLite serialises writes), so the loser gets ``rowcount ==
        0`` and the service rolls its reserve back.

        Used for ``per_user_once`` codes. Non-once codes use
        :meth:`insert_redemption` instead (unconditional append).
        """
        stmt = text(
            "INSERT INTO promo_redemptions "
            "  (code_id, user_id, reward_coins, redeemed_at) "
            "SELECT :code_id, :user_id, :reward_coins, :now "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM promo_redemptions "
            "  WHERE code_id = :code_id AND user_id = :user_id"
            ")"
        )
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                stmt,
                {
                    "code_id": code_id,
                    "user_id": user_id,
                    "reward_coins": reward_coins,
                    # Bind as an ISO string, not a raw ``datetime``: a
                    # ``text()`` INSERT goes straight to the DBAPI, which
                    # under Python 3.12+ rejects the default datetime
                    # adapter (deprecation → error). The ORM-mapped
                    # ``redeemed_at`` column reads it back as a datetime
                    # via SQLite's text storage, same as every other
                    # naive-UTC timestamp in this DB.
                    "now": _iso(now),
                },
            ),
        )
        return result.rowcount > 0

    async def insert_redemption(
        self, code_id: int, user_id: int, reward_coins: int, now: datetime
    ) -> None:
        """Unconditional append of a redemption row (non-once codes).

        For codes with ``per_user_once = 0`` the per-user guard does not
        apply, so the redemption is recorded directly. The global cap was
        already enforced by :meth:`reserve_use`.
        """
        self._session.add(
            PromoRedemption(
                code_id=code_id,
                user_id=user_id,
                reward_coins=reward_coins,
                redeemed_at=now,
            )
        )
        await self._session.flush()

    async def redemption_count(self, code_id: int) -> int:
        """Return how many times ``code_id`` has been redeemed (audit/UI)."""
        result = await self._session.execute(
            select(func.count())
            .select_from(PromoRedemption)
            .where(PromoRedemption.code_id == code_id)
        )
        return int(result.scalar() or 0)
