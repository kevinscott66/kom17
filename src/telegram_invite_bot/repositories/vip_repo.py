"""Read-side access for the two VIP grant sources.

Legacy ``ItemEffects.get_vip_profile`` (``bot.py:13476``) reads from
two distinct tables based on the ``group_id`` argument:

* ``group_id=None`` → ``users.vip_till`` — the *global* VIP grant
  that travels with the user across every chat. This is the source
  most handlers read (``/daily``, ``/profile``, message-reward bonus).
* ``group_id=<positive>`` → ``user_group_vip`` — a per-chat VIP grant
  with the same ``vip_till`` shape but scoped to one community. Used
  for chat-administered VIP perks.

The legacy reader also lazy-deletes expired rows during the SELECT
("if expires_at <= now, fire a DELETE before returning None"). The
new pipeline does NOT — same reasoning as :class:`PrivilegesRepo`:
read-on-write hides racing writers and forces a write transaction
on every read. Bulk cleanup of expired VIP rows is a future cron
method; the read side selects ``vip_till`` and compares it in Python,
which is also where :func:`utils.time.unix_ts` guards the frame of the
``now`` it is compared against — a naive ``now`` would be read in the
host's local zone and keep an expired VIP paying for three extra hours
on the MSK production host.

VIP profile constants
---------------------
Legacy hardcodes the profile payload at ``bot.py:13511-13517``::

    {"message_bonus": 1, "daily_bonus_percent": 15, "tax_discount_percent": 50}

The values do NOT vary per user — VIP is a binary status. The new
pipeline keeps the constants on :class:`VipProfile` (a frozen
dataclass) so future tiering ("VIP+", "Founder") is a typed change,
not a magic-dict mutation. For Stage 12 a single :data:`DEFAULT_VIP`
instance covers every VIP user; the repo returns that instance when
a grant is active, and ``None`` otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.economy import EconomyUser, UserGroupVip
from telegram_invite_bot.utils.time import unix_ts

if TYPE_CHECKING:
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class VipProfile:
    """Per-user VIP perks. Constants today; per-tier later.

    Values mirror the legacy hardcode at ``bot.py:13511-13517``. Changing
    any of these silently shifts payouts for every VIP across both
    pipelines, so the change should be a deliberate two-line PR
    that updates this dataclass and the matching legacy literals
    together.
    """

    message_bonus: int = 1
    """Extra coins per message for VIPs. Consumed by
    :func:`~telegram_invite_bot.services.vip_bonus.active_message_bonus`
    on the per-message earn path in ``middlewares/message_activity.py``
    (legacy ``bot.py:43841``). It is an addend applied *after* the
    ``xp_boost`` multiplier, so a boost never multiplies it."""
    daily_bonus_percent: int = 15
    """Percent added to the /daily payout. Drives the VIP gap that
    Stage 11 hard-zeroed in :class:`EffectsService` — porting this
    repo closes that gap."""
    tax_discount_percent: int = 50
    """Percent discount on transfer commission (read by /gift flow
    once the transfer port lands)."""


@dataclass(frozen=True, slots=True)
class VipExpiryCandidate:
    """One global-VIP grant inside the expiry-notice window (L-95).

    ``language`` rides along from ``economy.users.language`` so the
    sweep can render the DM in the user's stored language without a
    second query — a background task has no Telegram update to derive
    an injected ``lang`` from, and the stored preference is exactly
    what the language middleware would have resolved anyway.
    """

    user_id: int
    vip_till: float
    language: str


DEFAULT_VIP = VipProfile()
"""Single instance returned for every active VIP grant. Allocating
one shared frozen object beats per-call ``VipProfile()`` in the
hot path and the immutability guarantees nobody mutates it."""


class VipRepo:
    """Read-only access to global + group-scoped VIP grants."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_profile(
        self,
        user_id: int,
        *,
        now: datetime,
        group_id: int | None = None,
    ) -> VipProfile | None:
        """Return :data:`DEFAULT_VIP` if a VIP grant is active, else None.

        ``group_id is None`` reads the global grant
        (``users.vip_till``); a positive ``group_id`` reads the
        group-scoped grant (``user_group_vip``). The two are
        independent — a global-VIP user is NOT automatically
        group-VIP, and vice versa, matching legacy semantics.

        Active means ``vip_till`` is strictly greater than ``now``'s
        epoch: a row at exactly the deadline is read as expired,
        matching legacy's ``<= time.time()`` cutoff. ``now`` must be
        AWARE — :func:`unix_ts` coerces-and-logs a naive one rather
        than letting it be read in the host's local zone.
        """
        deadline = unix_ts(now, where="VipRepo.get_active_profile")
        if group_id is None:
            global_stmt = select(EconomyUser.vip_till).where(EconomyUser.user_id == user_id)
            vip_till = (await self._session.execute(global_stmt)).scalar_one_or_none()
        else:
            group_stmt = select(UserGroupVip.vip_till).where(
                UserGroupVip.user_id == user_id,
                UserGroupVip.group_id == group_id,
            )
            vip_till = (await self._session.execute(group_stmt)).scalar_one_or_none()

        if vip_till is None:
            return None
        if vip_till <= deadline:
            return None
        return DEFAULT_VIP

    async def get_vip_till(self, user_id: int, *, group_id: int | None = None) -> float | None:
        """Return the raw ``vip_till`` epoch for a grant, or ``None``.

        Unlike :meth:`get_active_profile` this does not apply the
        active/expired cutoff — the caller gets the stored timestamp
        verbatim so the ``/vip`` card can render the exact expiry date
        and days-left (and distinguish "never VIP" → ``None`` from
        "expired" → a past timestamp). ``group_id`` selects the global
        (``None``) or group-scoped grant, mirroring
        :meth:`get_active_profile`.
        """
        # Two executes rather than one rebound ``stmt``: the global column
        # is nullable and the group one is not, so ``Select`` — invariant
        # in its row type — refuses the reassignment under --strict. Same
        # shape :meth:`get_active_profile` already uses above.
        if group_id is None:
            global_stmt = select(EconomyUser.vip_till).where(EconomyUser.user_id == user_id)
            vip_till = (await self._session.execute(global_stmt)).scalar_one_or_none()
        else:
            group_stmt = select(UserGroupVip.vip_till).where(
                UserGroupVip.user_id == user_id,
                UserGroupVip.group_id == group_id,
            )
            vip_till = (await self._session.execute(group_stmt)).scalar_one_or_none()
        return float(vip_till) if vip_till is not None else None

    async def grant_global(self, *, user_id: int, now: datetime, duration: timedelta) -> datetime:
        """Extend ``users.vip_till`` by ``duration`` and return the new expiry.

        Legacy ``apply_vip_status`` STACKS (``bot.py:13442-13444``)::

            new_expires = current["expires"] + duration * 86400

        A user who buys a second month while the first is still running
        gets sixty days, not thirty. #192: the port replaced that with
        ``vip_till = MAX(existing, now + duration)``, which silently
        turns the second purchase into a no-op — 5000 COM for zero extra
        days on the one VIP row production actually sells
        (``👑 VIP статус``, ``stock=-1``, so it is buyable forever).
        MAX is only equivalent to stacking when the previous grant has
        already expired, and that is exactly the case a repeat buyer is
        not in.

        The arithmetic stays inside one statement, as it did for MAX::

            vip_till = MAX(IFNULL(vip_till, 0), :now) + :duration_seconds

        ``MAX(..., :now)`` is the "start from now if the old grant has
        lapsed" clamp: without it, a grant bought a year after the last
        one expired would be measured from that stale timestamp and land
        in the past. ``IFNULL(vip_till, 0)`` matters for the same reason
        it did before — a never-VIP wallet stores NULL, and SQL
        ``MAX(NULL, x)`` is NULL, not ``x``.

        Additive-from-max is BETTER under concurrency than MAX was, not
        worse: two interleaved grants under MAX collapse into one (the
        loser's days vanish), while here each UPSERT adds its own
        duration to whatever the previous one left, so both grants
        survive in either ordering. Both are still single statements
        under SQLite's write lock.

        The expiry is read back rather than computed in Python because
        only the database knows what the row held. The read is inside
        the caller's transaction, which by then holds the write lock
        (the UPSERT above opened ``BEGIN IMMEDIATE``), so no other
        writer can slip between the write and the read.

        UPSERT (not bare UPDATE) handles the cold-cache edge where a
        user's wallet row is somehow absent — extremely rare since
        every /buy seeds the wallet, but a deleted-then-rebuilt row
        between purchase and /use shouldn't silently drop the grant.
        The canonical default balance from
        ``EconomyRepo._LEGACY_DEFAULT_BALANCE`` is intentionally NOT
        mirrored here — if the row didn't exist, the user can't have
        paid for the VIP they're now redeeming, so the only realistic
        path that hits the insert branch is a test that forgot to
        seed. Picking ``balance=0`` rather than 100 makes that
        accidental-seed surface as a wrong balance, not an
        invisible-bonus.
        """
        now_ts = now.timestamp()
        duration_seconds = duration.total_seconds()
        stmt = sqlite_insert(EconomyUser).values(
            user_id=user_id,
            balance=0,
            language="ru",
            vip_till=now_ts + duration_seconds,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "vip_till": func.max(
                    func.ifnull(EconomyUser.vip_till, 0.0),
                    now_ts,
                )
                + duration_seconds,
            },
        )
        await self._session.execute(stmt)
        granted = (
            await self._session.execute(
                select(EconomyUser.vip_till).where(EconomyUser.user_id == user_id)
            )
        ).scalar_one()
        # Both UPSERT branches write a non-NULL ``vip_till``; the column
        # is nullable at the ORM level only because a never-VIP wallet
        # has never been through this method.
        # #286: narrowing, not a guard — the UPSERT above has already
        # committed a non-NULL value, and a miss rolls the grant back
        # rather than shortening it.
        assert granted is not None  # noqa: S101
        # Mirror the tz-awareness of ``now`` instead of forcing UTC. The
        # whole inventory stack runs on naive local datetimes (legacy
        # convention — ``handlers/shop.py`` passes a bare
        # ``datetime.now()``), and ``datetime.timestamp()`` reads a naive
        # value as LOCAL time. Rebuilding it as UTC would round-trip the
        # same instant into a different wall clock and quote the buyer an
        # expiry off by the local UTC offset.
        if now.tzinfo is not None:
            return datetime.fromtimestamp(float(granted), tz=now.tzinfo)
        return datetime.fromtimestamp(float(granted))  # noqa: DTZ006

    async def list_expiring_global(
        self, *, now: datetime, within: timedelta, limit: int
    ) -> list[VipExpiryCandidate]:
        """Global-VIP grants expiring within ``within``, not yet notified.

        Legacy's ``maybe_notify_vip_expiring`` (``bot.py:6957``) checked
        ``0 < expires - now <= 86400`` per user, but only when that user
        happened to render a profile (``bot.py:17877``/``39707``), with a
        24h in-process cache as the only repeat guard (``bot.py:6970``).
        The sweep version selects ALL eligible users in one query:

        * still active — ``vip_till > now`` (an already-expired grant
          gets no notice, matching legacy's ``left_sec <= 0`` bail);
        * inside the window — ``vip_till <= now + within``;
        * not yet notified for THIS deadline —
          ``vip_notified_till IS NULL OR vip_notified_till != vip_till``
          (durable, unlike legacy's restart-amnesiac cache).

        Group-scoped VIP (``user_group_vip``) is deliberately excluded —
        legacy never notified about it either (``get_vip_profile`` is
        called with ``group_id=None`` at ``bot.py:6962``).

        ``limit`` is mandatory, not defaulted (#1617), like
        ``WithdrawalsRepo.list_stale_pending``: every row this returns
        becomes a Telegram DM and a separate write session in the
        caller, and the caller is a sweeper whose money half cannot run
        while this fan-out does. Neither ``vip_till`` nor
        ``vip_notified_till`` is indexed in production (``economy.users``
        carries only ``idx_users_balance`` and ``idx_users_streak``), so
        the read is a full table scan whatever we do here — the cap is
        about the SENDS, not the SELECT.

        ``ORDER BY vip_till ASC`` is what makes the cap safe to apply:
        the grants nearest their deadline are the ones a delayed notice
        actually harms, so a truncated pass drops the least urgent. A
        candidate whose DM keeps failing is never marked and therefore
        holds its slot on the next pass too — bounded, because the row
        leaves the window for good once ``vip_till`` goes by (the
        ``vip_till > now`` clause above), but real: see #1617 for why
        an attempt counter would need durable state we chose not to add.
        """
        floor = now.timestamp()
        ceiling = (now + within).timestamp()
        stmt = (
            select(EconomyUser.user_id, EconomyUser.vip_till, EconomyUser.language)
            .where(
                EconomyUser.vip_till.is_not(None),
                EconomyUser.vip_till > floor,
                EconomyUser.vip_till <= ceiling,
                or_(
                    EconomyUser.vip_notified_till.is_(None),
                    EconomyUser.vip_notified_till != EconomyUser.vip_till,
                ),
            )
            .order_by(EconomyUser.vip_till.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            VipExpiryCandidate(user_id=int(row[0]), vip_till=float(row[1]), language=str(row[2]))
            for row in rows
        ]

    async def mark_expiry_notified(self, *, user_id: int, vip_till: float) -> bool:
        """Record that the expiry notice for ``vip_till`` was delivered.

        Guarded ``UPDATE ... WHERE vip_till = :vip_till`` — if the grant
        was extended between the candidate SELECT and the DM landing,
        the deadline we notified about no longer exists, the guard
        misses (``rowcount == 0``) and the user stays eligible for a
        fresh notice about the NEW deadline. Returns whether the mark
        landed.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(EconomyUser)
                .where(
                    EconomyUser.user_id == user_id,
                    EconomyUser.vip_till == vip_till,
                )
                .values(vip_notified_till=vip_till)
            ),
        )
        return result.rowcount > 0
