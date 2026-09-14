"""Read-only inventory access for ``economy.inventory``.

Stage 16 surface: list a user's last N purchases joined with
``shop_items``. The entry point mirrors the legacy call at
``bot.py:23867`` (``get_user_inventory(user_id, include_used=False,
include_expired=False)``); the query behind it is ``bot.py:12901-12911``:

    SELECT i.id, i.user_id, i.purchase_date, i.used, i.used_date, i.expires,
           s.id, s.name, s.description, s.price, s.stock, s.type, s.data
    FROM inventory i
    JOIN shop_items s ON i.item_id = s.id
    WHERE i.user_id = ?
      AND i.used = 0          -- appended only when include_used is False

Four deliberate divergences, none cosmetic:

* We select the five columns the caller renders, not all thirteen.
* Legacy has NO SQL expiry filter. It fetches every row and drops the
  expired ones in Python (``bot.py:12919``), which is why ``now`` is a
  parameter here rather than a ``datetime('now')`` in the statement.
* Legacy has neither ORDER BY nor LIMIT — the display cap lived in the
  caller. We sort newest-first and bound the row count in SQL.
* ``AND i.used = 0`` excludes SQL NULL and so do we. The comment on the
  ``include_used`` branch below is the authority on that choice; an
  earlier version of this docstring claimed a ``used IS NULL OR`` that
  neither side has ever had.

The used/expired filters are the legacy default for ``/inventory`` —
users only see entitlements they can still act on. Flip via flags
for admin/debug views.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, delete, or_, select, update

from telegram_invite_bot.core.entities.shop import InventoryDetail, InventoryEntry
from telegram_invite_bot.db.models.economy import InventoryItem, ShopItem

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_DEFAULT_LIMIT = 30


class InventoryRepo:
    """``economy.inventory`` reader."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = _DEFAULT_LIMIT,
        now: datetime | None = None,
        include_used: bool = False,
        include_expired: bool = False,
    ) -> list[InventoryEntry]:
        """Return a user's recent purchases, joined with shop_items.

        Defaults mirror the legacy ``/inventory`` call site
        (``bot.py:23867``: ``include_used=False, include_expired=False``)
        — used items and expired entitlements are hidden so the list
        only shows things the user can still act on. Pass the flags
        explicitly for admin/debug views.

        ``now`` is injectable so tests can pin expiry boundaries
        deterministically; defaults to naive local-time
        :func:`datetime.now` because legacy writes ``expires`` with
        ``datetime.now()`` (bot.py:12861) and compares with
        ``datetime.now()`` (bot.py:12919). Using ``utcnow`` here
        would produce off-by-TZ-hours boundary errors on rows seeded
        by legacy.

        Legacy was NOT self-consistent about this: its SQL cleanup
        (``bot.py:13984``) compared the same column against SQLite's
        ``datetime('now')``, which is UTC, never local. On MSK
        (UTC+3) that made the SQL sweep run three hours late relative
        to legacy's own Python comparison. Matching the Python side is
        deliberate — it is the one users actually saw.
        """
        if now is None:
            now = datetime.now()  # noqa: DTZ005
        stmt = (
            select(
                InventoryItem.id,
                ShopItem.name,
                InventoryItem.purchase_date,
                InventoryItem.used,
                InventoryItem.expires,
            )
            .join(ShopItem, ShopItem.id == InventoryItem.item_id)
            .where(InventoryItem.user_id == user_id)
        )
        if not include_used:
            # Legacy is literally ``AND i.used = 0`` (bot.py:12909) —
            # SQL NULL is explicitly excluded by that comparison. The
            # prod schema defines ``used BOOLEAN DEFAULT 0`` so a NULL
            # is a malformed row from before the DEFAULT existed;
            # hiding it matches what legacy users currently see.
            stmt = stmt.where(InventoryItem.used.is_(False))
        if not include_expired:
            stmt = stmt.where(or_(InventoryItem.expires.is_(None), InventoryItem.expires > now))
        stmt = stmt.order_by(InventoryItem.purchase_date.desc()).limit(limit)
        result = await self._session.execute(stmt)
        return [
            InventoryEntry(
                inventory_id=row.id,
                item_name=row.name,
                purchase_date=row.purchase_date,
                used=bool(row.used),
                expires=row.expires,
            )
            for row in result.all()
        ]

    async def get_for_user(
        self,
        user_id: int,
        inventory_id: int,
    ) -> InventoryDetail | None:
        """Look up a single inventory row constrained to ``user_id``.

        Stage 26 inspect-callback authorization hinges on this method:
        the lookup MUST be ``(user_id, inventory_id)`` AND, never
        ``inventory_id`` alone. A user B who hand-crafts an
        :class:`InventoryInspect` with user A's ``entry_id`` reaches
        this method with ``user_id == B.id``, the row's owning user is
        A, the WHERE clause filters it out, and the caller surfaces a
        "no longer available" toast. No information leak about whether
        the entry exists under a different owner — the same ``None``
        return covers "deleted", "expired", "used", AND "not yours",
        which is the right posture for a private-data lookup.

        The ``used`` and ``expires`` filters that ``list_for_user``
        applies are deliberately NOT applied here: the inspect flow
        targets an entry the user just saw in the list (so the list's
        filters already excluded it if applicable), and the entry's
        own ``used`` / ``expires`` fields are surfaced on the card so
        a state change between list-render and click renders honestly
        rather than silently disappearing.
        """
        stmt = (
            select(
                InventoryItem.id,
                InventoryItem.user_id,
                InventoryItem.item_id,
                ShopItem.name,
                ShopItem.description,
                InventoryItem.purchase_date,
                InventoryItem.used,
                InventoryItem.expires,
            )
            .join(ShopItem, ShopItem.id == InventoryItem.item_id)
            .where(
                InventoryItem.id == inventory_id,
                InventoryItem.user_id == user_id,
            )
        )
        result = await self._session.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return InventoryDetail(
            inventory_id=row.id,
            user_id=row.user_id,
            item_id=row.item_id,
            item_name=row.name,
            item_description=row.description or "",
            purchase_date=row.purchase_date,
            used=bool(row.used),
            expires=row.expires,
        )

    async def consume(
        self,
        *,
        user_id: int,
        inventory_id: int,
        now: datetime,
    ) -> bool:
        """Race-safe single-shot mark-as-used. Returns True if THIS call won.

        Stage 28 contract: the /use flow reads the entry, calls the
        planner to decide what grant to apply, then comes here to mark
        the entry consumed under the same outer session as the grant
        write. Two concurrent /use clicks must collapse to "exactly one
        grant applied" — the legacy code at ``bot.py:13141-13180``
        relied on the telebot blocking handler model to serialise these
        clicks, but aiogram processes updates concurrently and a
        double-tap on the same inventory row would otherwise apply the
        effect twice (two VIP grants, two busters) before the first
        write commits.

        The guard is a single ``UPDATE ... WHERE id=:id AND
        user_id=:uid AND used=0`` and a rowcount check: only one
        statement can satisfy the WHERE when two run interleaved,
        so the second returns rowcount=0 and we surface that as
        ``False`` to the caller (which translates to ALREADY_USED in
        the service-layer outcome).

        Two filters in WHERE on purpose:
        * ``user_id`` re-asserts ownership so a hand-crafted call with
          another user's ``inventory_id`` can't consume their row even
          if the caller forgot to pre-check via ``get_for_user``.
        * ``used = 0`` is the race guard — the boolean column is the
          single source of truth for "already consumed?" since legacy
          /use writes here (bot.py:13153 ``UPDATE inventory SET
          used=1, used_date=? WHERE id=?``, repeated once per item
          branch — 13158, 13174, 13178, 13183, 13187, 13191, 13195,
          13200). The companion ``used_date`` is
          written for parity with legacy's audit trail (admin queries
          that filter by use date), but the race-safety hinges on the
          ``used`` boolean alone — using ``used_date IS NULL`` instead
          would mis-classify legacy-imported rows that have a non-NULL
          ``used_date`` from a pre-schema-cleanup migration.

        ``now`` is injected (no clock read here) so the service's
        instant flows uniformly to both the inventory ``used_date``
        and the corresponding grant ``expires_at``/``vip_till`` —
        keeps grant TTLs aligned with the audit record they were
        triggered from, and keeps tests deterministic.

        Returns ``bool`` (not ``int``) because the only meaningful
        distinction at the call site is "did I win?" — the rowcount
        is either 0 or 1 by the WHERE clause (PK match), so a count
        would just be a thinly-disguised bool with a misleading type.
        """
        stmt = (
            update(InventoryItem)
            .where(
                InventoryItem.id == inventory_id,
                InventoryItem.user_id == user_id,
                InventoryItem.used.is_(False),
            )
            .values(used=True, used_date=now)
        )
        # Same ``CursorResult`` cast PurchaseService uses — the async
        # session stubs widen Update results to ``Result[Any]`` which
        # loses the ``rowcount`` attribute.
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return bool(result.rowcount or 0)

    async def delete_expired(self, now: datetime) -> int:
        """Hard-delete every inventory row whose ``expires`` is in the past.

        L-26 cleanup sweep — and a DELIBERATE divergence from legacy.
        Legacy's hourly ``cleanup_expired_inventory``
        (``bot.py:13973-13994``) did NOT delete anything: it ran
        ``UPDATE inventory SET used=1, used_date=? WHERE
        expires < datetime('now') AND used=0``, marking expired rows as
        used and leaving them in the table forever. (The similarly
        named ``_cleanup_expired_inventory`` at ``bot.py:11822`` is a
        misnomer — it only drops the ``balance_`` cache keys and never
        touches inventory at all.)

        We hard-delete instead. The rows are dead either way:
        ``list_for_user`` hides them (it filters ``expires > now`` on
        read), no production caller passes ``include_expired=True``, and
        :meth:`get_by_id` already collapses "deleted" and "expired" into
        the same ``None``, so no user-visible answer changes. What IS
        lost relative to legacy is the historical record of an
        entitlement that expired unused. Flipping this back to an UPDATE
        is an OWNER decision, not a refactor — it changes what the table
        retains about real purchases.

        Only rows with a NON-NULL ``expires`` that is strictly in the
        past are removed: a NULL ``expires`` means "never expires"
        (permanent entitlement) and MUST survive the sweep — the
        ``expires.isnot(None)`` guard is load-bearing, since
        ``NULL < now`` is SQL-NULL (not true) but stating the guard
        explicitly documents the intent and matches the legacy WHERE.

        ``used`` rows are NOT spared: a used + expired row is doubly
        dead, so the sweep reaps on the expiry predicate alone. Legacy's
        predicate carried an extra ``AND used=0``, but it had to — it
        WROTE ``used=1``, and without that term it would have rewritten
        the same rows every hour forever. A DELETE needs no such
        idempotence guard.

        ``now`` is injected (no clock read here) so the scheduler task
        and tests pin the boundary deterministically; the caller passes
        naive local-time ``datetime.now()`` to match the convention
        ``list_for_user`` / legacy use for the ``expires`` column
        (SQLite local time — see :meth:`list_for_user`).

        Returns the number of rows deleted (0 when nothing was due) so
        the scheduler task can log a one-line "reaped N" for ops.
        """
        stmt = delete(InventoryItem).where(
            InventoryItem.expires.isnot(None),
            InventoryItem.expires < now,
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return int(result.rowcount or 0)
