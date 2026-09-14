"""Async repository for the ``economy.checks`` / ``economy.check_claims``
tables — the coin-code voucher feature (#26).

Surface mirrors :class:`EconomyRepo`'s posture: guarded
``UPDATE ... WHERE ...`` statements carry the race safety in the WHERE
clause, ``rowcount`` is the success signal, and the repo trusts its
arguments (validation lives at the service layer).

The load-bearing method is :meth:`claim_decrement`. Legacy's claim path
(``bot.py:10081`` ``_activate_check``) reads ``remaining_amount`` in
Python, computes ``remaining - amount`` and writes it back in a
separate statement — a classic TOCTOU window where two concurrent
claims both read the same ``remaining`` and both write a decrement,
draining the check below zero (minting coins). We fold the read, the
guard and the write into ONE ``UPDATE`` whose WHERE clause re-checks
``remaining_amount >= :amount`` and the max-claims cap, so the loser of
a race finds the row already drained and gets ``rowcount == 0``.

The double-claim guard is a different race, caught a different way: the
``UNIQUE(check_id, user_id)`` constraint on ``check_claims`` (see
:class:`CheckClaim`). :meth:`has_claimed` is a friendly fast pre-check
that short-circuits the common "I already redeemed this" case, but it
can race; the constraint is the real guard and
:meth:`insert_claim` lets the :class:`IntegrityError` propagate so the
service can roll the whole transaction back.

Ledger writes (``Transaction`` rows) and wallet mutations live in
:class:`TransactionsRepo` / :class:`EconomyRepo`; the service composes
all three over one shared session so a claim is one atomic transaction.
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, select, text, update

from telegram_invite_bot.db.models.economy import Check, CheckClaim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_CODE_ALPHABET = string.ascii_uppercase + string.digits


class ChecksRepo:
    """``economy.checks`` + ``economy.check_claims`` access.

    Constructed per request with an open session shared with
    :class:`EconomyRepo` / :class:`TransactionsRepo`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def generate_unique_code(self, length: int = 8) -> str:
        """Return a fresh code not present in ``checks.code``.

        Mirrors legacy ``_generate_check_code`` (``bot.py:10030``): try
        up to 20 random ``A-Z0-9`` codes, returning the first one not
        already used. On the (astronomically unlikely) event that all 20
        collide, fall back to a longer code (``length + 4``) which the
        legacy code also does — the larger keyspace makes a second
        collision effectively impossible, and we accept the tiny
        residual risk rather than loop forever holding the session.

        The check is a cheap ``SELECT 1 ... LIMIT 1``; the actual
        race-safety against two creators generating the same code at
        once is the table's ``UNIQUE(code)`` constraint, not this probe.

        Drawn from :mod:`secrets`, not :mod:`random`. This code IS the
        credential: :meth:`CheckService.claim_check` takes ``(user_id,
        code)`` and credits whoever presents it, so a multi-claim check
        is a bearer voucher for real coins. ``random`` is one global
        Mersenne Twister shared with every other unseeded caller in the
        process; its output is a deterministic function of a recoverable
        state, and each generated code hands an observer 8 samples of
        that stream. No exploit is claimed here — recovering MT state
        from truncated draws is real work — but the argument for
        ``random`` was only ever "this isn't crypto", and for a token
        that pays out, that premise is wrong. ``secrets`` costs nothing
        (eight draws per check) and removes the question entirely.
        """
        for _ in range(20):
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
            exists = await self._session.execute(
                select(1).select_from(Check).where(Check.code == code).limit(1)
            )
            if exists.scalar_one_or_none() is None:
                return code
        # Fallback: longer code, vastly larger keyspace.
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length + 4))

    async def create(
        self,
        *,
        code: str,
        creator_id: int,
        type: str,
        total_amount: int,
        remaining_amount: int,
        min_amount: int | None,
        max_amount: int | None,
        fixed_amount: int | None,
        target_user_id: int | None,
        max_claims: int,
        required_language: str | None,
        required_premium: int,
        required_subscription: int,
        expires_at: datetime | None,
        now: datetime,
        min_age: int | None = None,
        min_activity: int | None = None,
        allowed_countries: str | None = None,
        blocked_users: str | None = None,
    ) -> int:
        """INSERT a new check row and return its autoincrement id.

        Debiting the creator is the SERVICE's job (it composes this with
        ``EconomyRepo.debit`` and a ledger row under one transaction) —
        the repo only owns the INSERT, same single-responsibility split
        every other repo in this package follows.
        """
        row = Check(
            code=code,
            creator_id=creator_id,
            type=type,
            total_amount=total_amount,
            remaining_amount=remaining_amount,
            min_amount=min_amount,
            max_amount=max_amount,
            fixed_amount=fixed_amount,
            target_user_id=target_user_id,
            max_claims=max_claims,
            claims_count=0,
            required_language=required_language,
            required_premium=required_premium,
            required_subscription=required_subscription,
            min_age=min_age,
            min_activity=min_activity,
            allowed_countries=allowed_countries,
            blocked_users=blocked_users,
            expires_at=expires_at,
            created_at=now,
            is_active=1,
        )
        self._session.add(row)
        # Flush (not commit) so the autoincrement id is populated and any
        # UNIQUE(code) violation surfaces here, inside the service's
        # transaction, rather than at the outer middleware commit.
        await self._session.flush()
        return int(row.id)

    async def get_active_by_code(self, code: str) -> Check | None:
        """Return the active check for ``code`` or ``None``.

        ``WHERE code = :code AND is_active = 1`` — an exhausted /
        expired / deactivated check returns ``None`` (the service maps
        that to NOT_FOUND, same UX as legacy's "не найден или уже
        неактивен").
        """
        result = await self._session.execute(
            select(Check).where(Check.code == code, Check.is_active == 1).limit(1)
        )
        return result.scalar_one_or_none()

    async def has_claimed(self, check_id: int, user_id: int) -> bool:
        """``True`` iff ``user_id`` already has a claim row for ``check_id``.

        A friendly fast pre-check so the common "you already redeemed
        this" case short-circuits before the decrement. It CAN race with
        a concurrent claim from the same user (two devices, double-tap) —
        the authoritative double-claim guard is the
        ``UNIQUE(check_id, user_id)`` constraint enforced at
        :meth:`insert_claim`.
        """
        result = await self._session.execute(
            select(1)
            .select_from(CheckClaim)
            .where(CheckClaim.check_id == check_id, CheckClaim.user_id == user_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def deactivate_expired(self, now: datetime) -> int:
        """Flip ``is_active = 0`` on every expired-but-still-active check.

        Sweep hygiene for L-95. Legacy deactivated an expired check only
        LAZILY — at claim time inside ``_activate_check``
        (``bot.py:10110-10117``: ``if now > exp_dt: UPDATE checks SET
        is_active = 0``); a check nobody tries to claim after its
        deadline stays ``is_active = 1`` forever. The new claim path
        ports the same lazy gate (``CheckService.claim_check`` →
        EXPIRED), so this bulk pass is pure table hygiene plus
        consistency for anything that filters on ``is_active``.

        Strict ``expires_at < now`` mirrors the claim-side ``now >
        expires_at`` cutoff. It is a SQL comparison on a SQLite TEXT
        column, i.e. byte-wise: it is sound only because every row is
        written in one frame and one format. The port writes naive UTC
        with a SPACE separator (SQLAlchemy's SQLite bind processor);
        legacy wrote naive LOCAL with ``'T'`` (0x54 > 0x20), which would
        sort AFTER any port timestamp and so never expire. Inert — the
        legacy writer is not running and the production table is empty
        — but see ``handlers.checks._utcnow`` before importing old rows.
        No refund of ``remaining_amount`` — legacy
        never refunded an expired check's remainder, and inventing a
        credit here would mint coins the legacy economy never paid.
        Zero-remaining / max-claims checks need no sweep: the claim
        guard (:meth:`claim_decrement`) already flips ``is_active``
        atomically when a check drains.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(Check)
                .where(
                    Check.is_active == 1,
                    Check.expires_at.is_not(None),
                    Check.expires_at < now,
                )
                .values(is_active=0)
            ),
        )
        return result.rowcount

    async def deactivate(self, check_id: int) -> None:
        """Set ``is_active = 0`` for ``check_id`` (expired / exhausted)."""
        await self._session.execute(update(Check).where(Check.id == check_id).values(is_active=0))

    async def claim_decrement(self, check_id: int, amount: int, max_claims: int) -> bool:
        """Atomically reserve ``amount`` coins from a check. Returns success.

        This is the race fix. The single guarded UPDATE:

        * subtracts ``amount`` from ``remaining_amount``,
        * increments ``claims_count``,
        * flips ``is_active`` to 0 when the check is now drained
          (``remaining - amount <= 0``) OR the max-claims cap is reached,
        * and — crucially — only matches the row if it is still active,
          still has enough remaining (``remaining_amount >= :amount``)
          and is still under the claims cap.

        Two concurrent claims of the last coins cannot both succeed: the
        first commits ``remaining -= amount``; the second's WHERE clause
        sees the post-first ``remaining`` and fails the
        ``remaining_amount >= :amount`` guard, returning ``rowcount == 0``
        (which the service maps to RACE_LOST). Legacy needed no lock and
        had this race wide open — it read ``remaining`` in Python and
        wrote ``remaining - amount`` back unconditionally.

        Rendered as raw SQL (mirroring legacy's exact CASE) because the
        ``is_active`` flip references the *pre-decrement* column values
        in the same statement, which is clearer as literal SQL than as a
        SQLAlchemy ``case()`` over bound column expressions.
        """
        stmt = text(
            "UPDATE checks "
            "SET remaining_amount = remaining_amount - :amount, "
            "    claims_count = claims_count + 1, "
            "    is_active = CASE "
            "        WHEN remaining_amount - :amount <= 0 "
            "          OR (:max_claims > 0 AND claims_count + 1 >= :max_claims) "
            "        THEN 0 ELSE 1 END "
            "WHERE id = :id "
            "  AND is_active = 1 "
            "  AND remaining_amount >= :amount "
            "  AND (:max_claims = 0 OR claims_count < :max_claims)"
        )
        # Same ``CursorResult`` cast inventory_repo / PurchaseService use —
        # the async session stubs widen an UPDATE result to ``Result[Any]``,
        # which loses the ``rowcount`` attribute under mypy --strict.
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                stmt,
                {"amount": amount, "max_claims": max_claims, "id": check_id},
            ),
        )
        return result.rowcount > 0

    async def insert_claim(self, check_id: int, user_id: int, amount: int, now: datetime) -> None:
        """INSERT one ``check_claims`` row; let IntegrityError propagate.

        The ``UNIQUE(check_id, user_id)`` constraint is how a
        double-claim is caught atomically: a second concurrent claim
        from the same user raises
        :class:`sqlalchemy.exc.IntegrityError` here. We deliberately do
        NOT swallow it — the service catches it, rolls the transaction
        back (undoing the :meth:`claim_decrement` reservation) and
        reports ALREADY_CLAIMED, so the user is never paid twice.
        """
        row = CheckClaim(
            check_id=check_id,
            user_id=user_id,
            amount=amount,
            claimed_at=now,
        )
        self._session.add(row)
        await self._session.flush()
