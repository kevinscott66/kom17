"""Reads, grants and deletes on ``economy.user_privileges``.

The legacy ``PrivilegeManager`` (``bot.py:13268``) is a polymorphic
key/value store with a 7-deep cache layer and an exception swallower
on every method. The new pipeline's near-term needs are narrower:

* :meth:`get_active` + :meth:`remove` — /daily's ``double_daily``
  buster, plus the effects layer's per-perk lookups.
* :meth:`grant_buster` + :meth:`grant_with_value` — the write side.
  It no longer lives in legacy: the shop port issues grants here.
* :meth:`delete_expired` — storage hygiene for the hourly sweep.

VIP status is NOT read here. It lives in :class:`VipRepo`, because
legacy kept it on ``users.vip_till`` / ``user_group_vip`` rather than
in ``user_privileges``.

Why a focused repo over porting the full PrivilegeManager
=========================================================
The legacy class mixes concerns we want to split:

1. *DB access* (this repo's job).
2. *Cache invalidation* (legacy's ``cache_set/get/delete`` calls) —
   the new pipeline runs in-process under aiogram, so a request-scoped
   :class:`AsyncSession` already provides per-update memoisation
   for free. A separate cache layer is a Stage-N follow-up if a
   profiler shows it matters.
3. *Lazy garbage collection* (legacy's "if expires_at <= now, delete
   here" inside ``get_privilege``) — the new shape keeps reads
   read-only. :meth:`get_active` selects the PK row unfiltered and
   discards an expired one in Python: the read is a single PK hit, so
   an SQL-side predicate buys nothing, and keeping the comparison in
   Python is what lets :func:`unix_ts` guard the frame of ``now``.
   The cleanup happens via a separate
   :meth:`delete_expired`, called by the hourly hygiene pass in
   ``scheduler/economy_cleanup.py`` — that is the only caller, and it
   is storage hygiene only, since every reader already filters. Mixing
   the two made legacy's ``get_privilege`` write-on-read, which broke
   ``RETURNING``-style atomic reads in concurrent flows.

Sign convention
---------------
``expires_at`` is the legacy ``time.time()`` REAL — a float unix
timestamp. ``0`` (or any non-positive value) means "never expires".
The repo accepts a :class:`datetime` ``now`` parameter for
testability rather than reading the wall clock itself; the caller
passes an AWARE ``datetime.now(UTC)`` (or a fixed instant in tests).

"Aware" is load-bearing, not stylistic. ``.timestamp()`` on a naive
value interprets the wall clock in the HOST's zone, so a naive-UTC
``now`` (:func:`utils.time.db_now`) reads 10 800 s in the past on the
MSK production host and an expired grant keeps paying out for three
more hours. :func:`utils.time.unix_ts` catches and logs that instead
of letting it pass silently.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import case, delete, false, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.economy import UserPrivilege
from telegram_invite_bot.utils.time import unix_ts

if TYPE_CHECKING:
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import AsyncSession


class PrivilegesRepo:
    """``economy.user_privileges`` read + delete surface.

    Constructed per request with an open :class:`AsyncSession` so the
    repo participates in the caller's outer transaction (e.g. a
    /daily flow that consumes the double-buster and credits the
    wallet in one commit).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_writer(self) -> None:
        """Take this database's writer lock now, before the gate read.

        Same mechanism as ``WithdrawalsRepo.lock_writer`` (#776) and
        ``P2pRepo.lock_writer`` (#1503). SQLite serialises writers, not
        readers, and this project opens transactions lazily:
        ``db/engines.py:204-210`` issues ``BEGIN IMMEDIATE`` only for a
        *write*-headed statement, and ``"select"`` is not one. So a
        caller that reads :meth:`get_active` to decide whether a grant
        may proceed does that read outside any transaction, and two
        concurrent decisions see the same "nothing armed".

        The one caller is ``InventoryUseService``'s ``double_daily``
        branch, where the consequence is a destroyed item rather than a
        misplaced coin: the consume is keyed on ``inventory_id`` so two
        different entries both win it, and :meth:`grant_buster` upserts
        with ``MAX(expires_at, …)`` so the two grants collapse into one
        row by design. Two items in, one buster out.

        This UPDATE matches nothing (``WHERE false``); its ``UPDATE``
        head is what the engines hook keys on, so the connection enters
        ``BEGIN IMMEDIATE`` and a second caller blocks on
        ``PRAGMA busy_timeout`` (5 000 ms, ``db/pragma.py:63``) until
        the first commits, then reads the *committed* grant. Writing no
        rows is the point: the lock has to be acquirable before we know
        whether the grant will be made, and a refusal must release it
        having changed nothing.
        """
        await self._session.execute(
            update(UserPrivilege)
            .where(false())
            .values(value=UserPrivilege.value)
            .execution_options(synchronize_session=False)
        )

    async def get_active(
        self,
        user_id: int,
        priv_type: str,
        *,
        now: datetime,
        group_id: int = 0,
    ) -> UserPrivilege | None:
        """Return the matching privilege row only if not expired.

        ``expires_at <= 0`` means "never expires" — return the row.
        Otherwise compare against ``now``'s epoch and return ``None``
        if the deadline has passed. ``now`` must be AWARE; a naive
        value is coerced-and-logged by :func:`unix_ts` rather than
        being read silently in the host's local zone.

        Returning the ORM row (not just a bool) lets the caller
        inspect ``value`` if the privilege carries a payload (e.g.
        ``color_nick`` ships ``{"color": "rainbow"}``). For
        ``double_daily`` presence alone is the signal HERE — callers
        can ``is not None``-check the return. That is this pipeline's
        rule, not legacy's: legacy stored ``{"active": True}`` and its
        reader required it truthy (``bot.py:13662`` / ``:13673``). See
        :class:`UserPrivilege` for why the two must not be mixed over
        one database.

        ``group_id`` defaults to ``0`` which matches the legacy
        global-scope default. Passing a positive ``group_id`` reads
        the per-chat row (different PK slot — does NOT fall back to
        global, by design, so a group-scoped grant doesn't bleed
        into a different chat's check).
        """
        stmt = select(UserPrivilege).where(
            UserPrivilege.user_id == user_id,
            UserPrivilege.privilege_type == priv_type,
            UserPrivilege.group_id == group_id,
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        deadline = unix_ts(now, where="PrivilegesRepo.get_active")
        if row.expires_at > 0 and row.expires_at <= deadline:
            return None
        return row

    async def remove(
        self,
        user_id: int,
        priv_type: str,
        *,
        group_id: int = 0,
    ) -> bool:
        """Delete the matching privilege row. Returns whether one was deleted.

        Used by /daily to consume the one-shot ``double_daily``
        buster after a successful claim, and by future flows for
        cancellation/refund paths. Idempotent — deleting a missing
        row is a successful no-op, returning ``False`` so the caller
        can distinguish "I just consumed it" from "it wasn't there".
        """
        stmt = delete(UserPrivilege).where(
            UserPrivilege.user_id == user_id,
            UserPrivilege.privilege_type == priv_type,
            UserPrivilege.group_id == group_id,
        )
        # ``CursorResult.rowcount`` is correctly typed under the async
        # session protocol but the generic Result alias loses it —
        # cast preserves the intent without # type: ignore noise.
        result = await self._session.execute(stmt)
        return bool(getattr(result, "rowcount", 0) or 0)

    async def grant_buster(
        self,
        *,
        user_id: int,
        privilege_type: str,
        expires_at: datetime,
        group_id: int = 0,
    ) -> None:
        """Arm (or extend) a single-use buster row.

        Stage 28 writer for ``DOUBLE_DAILY_BUSTER`` plans. The legacy
        ``apply_double_daily`` at ``bot.py:13656`` does an unconditional
        upsert with ``expires = time.time() + 86400 * 3`` — re-applying
        a buster while one is already armed *shortens* the active row's
        TTL if the existing expiry was further out (e.g. user bought
        two busters back to back; the second click cuts the first
        click's 3-day safety window down to a fresh 3 days from now,
        which is harmless but only because legacy's TTL is so generous
        relative to /daily's 24h cooldown).

        We tighten that with MAX semantics on ``expires_at`` so that
        applying a shorter TTL on top of a longer one never shortens
        the window. This used to claim the same posture as
        :meth:`VipRepo.grant_global`; that stopped being true in #192,
        which made the VIP write ADDITIVE precisely because MAX turned
        a repeat purchase into a paid no-op. The two differ on purpose:
        VIP days are bought and must accumulate, a buster is a safety
        window and only ever needs to be long enough.
        Today both busters are 3 days TTL so the MAX is a no-op
        either way, but a future xp_boost / mute_protection write
        path reaching this method (or a longer-TTL buster variant
        the operator adds) gets the safe behaviour for free instead
        of needing a separate grant_long_buster method.

        ``value`` is intentionally NOT a parameter on this signature
        — busters are presence-only *in this pipeline*, so the write
        below pins NULL. Legacy wrote ``{"active": True}`` for the same
        grant (``bot.py:13662``); the divergence is deliberate and is
        documented on :class:`UserPrivilege` — and rows carrying that
        payload are still in the table, so the read side cannot assume
        NULL. A future grant method for payload-carrying privileges
        (color_nick, custom_title) will be a separate ``grant_with_value``
        to keep this one's surface small. That method is what those
        privileges are waiting on: the legacy path this note used to
        defer to was removed in T-011.

        UPSERT (not bare INSERT) because a user can re-buy and
        re-redeem the same buster type before the first is consumed;
        the composite PK ``(user_id, privilege_type, group_id)``
        guarantees one row per slot, and a conflict means we're
        extending an existing armed buster — see MAX rationale above.
        """
        ts = expires_at.timestamp()
        stmt = sqlite_insert(UserPrivilege).values(
            user_id=user_id,
            privilege_type=privilege_type,
            group_id=group_id,
            expires_at=ts,
            value=None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "privilege_type", "group_id"],
            set_={
                "expires_at": func.max(
                    UserPrivilege.expires_at,
                    stmt.excluded.expires_at,
                ),
            },
        )
        await self._session.execute(stmt)

    async def grant_with_value(
        self,
        *,
        user_id: int,
        privilege_type: str,
        value: str,
        now: datetime,
        duration: timedelta,
        group_id: int = 0,
    ) -> datetime:
        """Upsert a payload-carrying privilege row; return the resulting expiry.

        Stage 30 writer for ``COLOR_NICK``, ``MUTE_PROTECTION``,
        ``XP_BOOST`` and (via the FSM) ``CUSTOM_TITLE``. Legacy
        ``PrivilegeManager.set_privilege`` (``bot.py:13272``) is an
        unconditional UPSERT — both the JSON ``value`` and the
        ``expires_at`` are overwritten on conflict regardless of what
        was there — and this method used to match that posture exactly.

        #1950 splits the conflict path in two, on whether the payload
        the caller is writing is the SAME one the row already carries:

        * **Different payload — REPLACE, unchanged.** The value carries
          semantic content the user can vary (a different color). Keeping
          the longer window would silently keep the old color the user
          thought they replaced, so latest-wins is what the legacy code
          and the user's mental model both expect ("I just activated
          this, so this is what's active").
        * **Same payload — ADD, from whichever of the old expiry and
          ``now`` is later.** A repeat redemption of the same item is a
          repeat PURCHASE of duration, and REPLACE turned it into a
          partial refund of itself: prod's ``⚡ Ускорение`` is 60 minutes
          for 2500, so redeeming a second one ten minutes into the first
          used to buy 50 minutes of nothing. This is #192's finding — the
          VIP write was made additive for exactly this reason — and
          #1903's, one branch over. Measured on prod: the catalog carries
          ONE row per payload-carrying type
          (``🔇 Защита от мута`` 24 h, ``⚡ Ускорение`` 60 min / x2), so
          in practice every repeat redemption lands on this branch.

        The comparison is ``IS NOT DISTINCT FROM`` (SQLite ``IS``), not
        ``=``, so a NULL payload on either side compares as a value
        rather than swallowing the CASE into NULL. This pipeline always
        writes a non-NULL ``value`` here, but the column is nullable and
        ``grant_buster`` pins NULL into the same table.

        The ``expires_at <= 0`` arm keeps a never-expiring row of the
        same payload never-expiring: there is no window to extend, and
        adding a duration to it would DOWNGRADE an unlimited grant to a
        timed one. This pipeline never writes that sentinel (only legacy
        did), which is why the arm exists at all — it is the one input
        where "add" would take something away.

        Additive-from-max is also better under concurrency than REPLACE
        was: two interleaved grants under REPLACE collapse into one (the
        loser's duration vanishes), while here each UPSERT adds its own
        duration to whatever the previous one left, so both survive in
        either ordering.

        The expiry is read back rather than computed in Python because
        only the database knows what the row held — the caller renders
        "active until X" from it, and after #1950 that X is no longer
        ``now + duration`` in the common case. The read is inside the
        caller's transaction, which by then holds the write lock (the
        UPSERT opened ``BEGIN IMMEDIATE``), so no other writer can slip
        between the write and the read.

        ``now`` must be naive LOCAL, matching :meth:`delete_expired` and
        the legacy ``time.time()`` REAL the column stores: the arithmetic
        happens in epoch seconds inside SQLite, so a UTC-naive ``now``
        would land the sum three hours off on the MSK host.

        ``value`` is a pre-encoded string (the caller does
        :func:`json.dumps`). Keeping the encoding in the caller lets the
        repo stay schema-shaped — the column type is TEXT, the repo
        doesn't need to know JSON exists.

        ``group_id`` defaults to 0 (global scope), matching legacy's
        ``_priv_group_id(None) -> 0`` convention.
        """
        now_ts = now.timestamp()
        duration_seconds = duration.total_seconds()
        # Start from whichever of the live expiry and ``now`` is later:
        # a lapsed window must not measure the new one from a stale
        # timestamp and land it in the past.
        extended = func.max(UserPrivilege.expires_at, now_ts) + duration_seconds
        stmt = sqlite_insert(UserPrivilege).values(
            user_id=user_id,
            privilege_type=privilege_type,
            group_id=group_id,
            expires_at=now_ts + duration_seconds,
            value=value,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "privilege_type", "group_id"],
            set_={
                "expires_at": case(
                    (
                        UserPrivilege.value.is_not_distinct_from(stmt.excluded.value),
                        case(
                            (UserPrivilege.expires_at <= 0, UserPrivilege.expires_at),
                            else_=extended,
                        ),
                    ),
                    else_=stmt.excluded.expires_at,
                ),
                "value": stmt.excluded.value,
            },
        )
        await self._session.execute(stmt)
        granted = await self._session.scalar(
            select(UserPrivilege.expires_at).where(
                UserPrivilege.user_id == user_id,
                UserPrivilege.privilege_type == privilege_type,
                UserPrivilege.group_id == group_id,
            )
        )
        # ``fromtimestamp`` with no tz mirrors the naive-LOCAL contract
        # on ``now`` above: the column holds a local epoch, so reading it
        # back in any other zone would shift the rendered date.
        return datetime.fromtimestamp(float(granted or 0.0))  # noqa: DTZ006 — naive local, see above

    async def delete_expired(self, *, now: datetime) -> int:
        """Bulk-prune all privileges past their ``expires_at``.

        Returns the number of rows deleted. Called once an hour by
        ``EconomyCleanupSweeper.sweep_once``; not in the /daily hot
        path. ``now`` must be naive LOCAL — ``expires_at`` is the
        legacy ``time.time()`` REAL and this compares via
        ``.timestamp()``, which reads a naive datetime as local. The
        ``> 0`` clause skips the never-expires rows.
        """
        stmt = delete(UserPrivilege).where(
            UserPrivilege.expires_at > 0,
            UserPrivilege.expires_at <= now.timestamp(),
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)
