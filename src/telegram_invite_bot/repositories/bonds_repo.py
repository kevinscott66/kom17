"""Repos for the marriages + relationships leaderboards (Stage 19)
and proposal / accept / divorce writes (Stage T-019).

Read side: ``MarriagesRepo`` and ``RelationshipsRepo`` provide the group
leaderboard queries (JOIN to ``users.users`` for first_name).

Write side: ``BondsWriteRepo`` owns all mutations introduced in T-019 —
``propose_marriage``, ``accept_proposal``, ``decline_proposal``,
``get_marriage``, ``soft_divorce``, ``get_relationship``,
``terminate_relationship``.  Legacy wrote the same tables until T-011
removed it; because the PKs are DB-assigned AUTOINCREMENT there is no
range to reconcile — its rows are simply earlier rows in the same
sequence, and every read here has to expect them.

Why two read repos, not one: see the Stage 19 note below.
Why ``BondsWriteRepo`` is separate from the read repos: the write side
needs ``MarriageProposal`` and the ``_pair`` normalisation helper — mixing
those concerns into ``MarriagesRepo`` (a pure leaderboard repo) would
blur the boundary between Stage 19's read-only contract and T-019's
proposal FSM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from telegram_invite_bot.core.entities.bonds import MarriagePair, RelationshipPair
from telegram_invite_bot.db.models.bond_activity import (
    MarriageActivityLog,
    RelationshipActivityLog,
)
from telegram_invite_bot.db.models.users import (
    Marriage,
    MarriageProposal,
    Relationship,
    RelationshipProposal,
    User,
)
from telegram_invite_bot.repositories._helpers import legacy_status_active

# How long a soft-divorced marriage can still be restored
# (bot.py:22016 writes ``now + 3 days`` into ``restore_until``). Named
# here because #482's sweeper dissolves marriages down a second path
# and the two windows must not be allowed to drift apart: a bond ended
# by the absence sweep gets exactly the same grace as one ended by
# ``/divorce``.
_MARRIAGE_RESTORE_WINDOW = timedelta(days=3)

# Relationship XP lost per full day of inactivity (bot.py:21583
# RELATIONSHIP_DECAY_PER_DAY = 5). Applied lazily on read so an inactive
# couple's level falls over time — affects RP gating, the level-6
# marriage-eligibility gate, and leaderboard ordering. Marriages do NOT
# decay (legacy ``_apply_relationship_decay`` is relationship-only).
_RELATIONSHIP_DECAY_PER_DAY = 5


def _decayed_experience(
    experience: int, last_activity_at: datetime | None, now: datetime
) -> tuple[int, int]:
    """Return ``(new_experience, days_consumed)`` after inactivity decay.

    Mirrors legacy ``_apply_relationship_decay`` (bot.py:21884): whole-day
    floor, ``new = max(0, exp - days*5)``; ``days_consumed == 0`` means no
    change (and no write should happen).

    Frame: ``last_activity_at`` is stamped naive-LOCAL by the bond writers
    (``datetime.now()``, mirroring legacy), so ``now`` MUST be the same
    naive-local frame — NOT ``db_now()`` (naive-UTC), which would skew the
    day count by the server's UTC offset (REV-2). Defensive against a
    legacy-migrated row whose ``last_activity_at`` decodes as an ISO
    string (bot.py:21896-21902 does the same): never let the subtraction
    raise.
    """
    if last_activity_at is None:
        return experience, 0
    if isinstance(last_activity_at, str):
        try:
            last_activity_at = datetime.fromisoformat(
                last_activity_at.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except ValueError:
            return experience, 0
    days = (now - last_activity_at).days
    if days <= 0:
        return experience, 0
    return max(0, experience - days * _RELATIONSHIP_DECAY_PER_DAY), days


if TYPE_CHECKING:
    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


@dataclass(frozen=True, slots=True)
class BondActivityEntry:
    """One row from a bond activity-log, shaped for the L-34/L-35 history view.

    ``activity_key`` is the catalog key (``"dinner"``, ``"big_gift"`` …);
    the handler resolves it to a localized label. ``created_at`` is the raw
    column value (``str`` from the legacy TEXT column or ``datetime`` from a
    fresh insert) — :func:`utils.bonds.format_db_date` accepts both.
    """

    activity_key: str
    xp_gained: int
    created_at: datetime | str | None


@dataclass(frozen=True, slots=True)
class UserBond:
    """One bond seen from ONE party's side, across every chat (RR-1 #2).

    The leaderboard entities (:class:`MarriagePair` /
    :class:`RelationshipPair`) are symmetric — two ids, two names — because
    a group table lists pairs. The private profile panel asks a different
    question: *who am I bonded to, and where*. So this carries the
    ``partner`` only, plus the ``chat_id`` the bond belongs to, since bonds
    are chat-scoped and one person can hold several in different groups.
    """

    chat_id: int
    partner_id: int
    partner_name: str | None
    experience: int
    created_at: datetime | str | None


@dataclass(frozen=True, slots=True)
class RelationshipView:
    """One active relationship, with the DECAYED experience already applied.

    #477: this exists so :meth:`BondsWriteRepo.list_relationships_for` can
    stop handing out live ORM rows. Two reasons, and the second is the
    reason it is a dataclass rather than an expunged entity:

    * the stored ``experience`` column is stale by design — decay is
      applied lazily on the next per-pair read
      (:meth:`BondsWriteRepo.get_relationship`), so a list rendered
      straight off the column shows numbers no reader will ever see
      again, and ORDERS by them;
    * writing the decayed value back onto an attached ``Relationship``
      would make a *display* call persist decay as a side effect, on
      every pair the caller happens to be in, with no activity to
      justify it. A frozen DTO cannot do that by construction.

    Carries exactly what the three callers read: the pair ids (to work
    out who the partner is), the decayed ``experience``, and
    ``created_at`` for the "together since" line. ``created_at`` may be a
    string on legacy-migrated rows, same as :class:`UserBond`.
    """

    id: int
    user1_id: int
    user2_id: int
    experience: int
    created_at: datetime | str | None


def _partner_of(
    user_id: int, u1_id: int, u2_id: int, u1_name: str | None, u2_name: str | None
) -> tuple[int, str | None]:
    """``(partner_id, partner_name)`` for a row seen from ``user_id``'s side.

    Bond rows store their pair in canonical ``(lower, higher)`` order
    (:func:`_pair`), so "the other person" is simply whichever column
    isn't the caller. Resolving it here rather than with a SQL ``CASE``
    keeps the one-sided queries shaped exactly like the leaderboard ones
    above — same two outer joins, same columns.
    """
    if u1_id == user_id:
        return u2_id, u2_name
    return u1_id, u1_name


def _on_the_board() -> ColumnElement[bool]:
    """Predicate for "this marriage opted into ``/marriages``" (#2021).

    ``/marry_top_off`` writes a 0 here and tells the couple they are off
    the rating; before this predicate existed nothing read the column
    back, so the two commands were pure theatre in the new bot exactly
    as they were in legacy (``bot.py:22740``/``:22758`` write it,
    ``bot.py:22991`` ignores it).

    NULL coalesces to *included*, not excluded, and the direction is the
    whole point: a NULL is a row nobody ever expressed a preference
    about, and this filter must never be able to empty a board by
    itself. The rows carrying a real 0 from before revision
    ``0012_marriages_in_top_backfill`` are lifted to 1 by that
    revision — an untouched 0 and a deliberate ``/marry_top_off`` are
    the same zero, and only one of the two ever had an effect to
    preserve.

    Scoped to the leaderboard: :meth:`MarriagesRepo.list_for_user`
    renders the couple's OWN ``/marriage`` card and is deliberately
    unfiltered, since hiding a marriage from its own owners would read
    as a divorce.
    """
    return func.coalesce(Marriage.in_top, 1) != 0


class ProposalAlreadyResolvedError(Exception):
    """Raised when an accept/decline targets a proposal that is no longer
    in ``status='pending'`` — i.e. another caller already claimed it.

    The atomic claim lives in :meth:`BondsWriteRepo.accept_proposal` and
    :meth:`BondsWriteRepo.decline_proposal`; both surface this exception
    so the handler can render a friendly "already handled" reply without
    writing a second marriage row.
    """

    def __init__(self, prop_id: int) -> None:
        super().__init__(f"proposal {prop_id} already resolved")
        self.prop_id = prop_id


class MarriagesRepo:
    """``users.marriages`` access."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_active(self, chat_id: int) -> int:
        """How many active marriages the chat has.

        The leaderboard shows only the top rows (Telegram's 4096-char
        ceiling), and "…and N more" needs the total. Kept as its own
        COUNT rather than ``len(list_active(...))`` so asking the
        question costs one indexed scan instead of two LEFT JOINs and a
        full row transfer.
        """
        stmt = (
            select(func.count())
            .select_from(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                legacy_status_active(Marriage.status),
                _on_the_board(),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_active(self, chat_id: int, *, limit: int) -> list[MarriagePair]:
        """The ``limit`` strongest pairs, ``experience DESC, created_at ASC``.

        ``status`` semantics from ``bot.py:22991``: ``NULL`` is treated
        the same as ``'active'`` (legacy migration left old rows with
        no status set). Anything else (``'divorced'``,
        ``'pending_restore'``) is hidden.

        ``limit`` is mandatory and applied in SQL, for the same reason
        :meth:`list_for_user` takes one: the caller renders one line per
        row into a single Telegram message, so an unbounded read is
        always either a 400 or a wasted transfer.
        """
        u1 = aliased(User)
        u2 = aliased(User)
        stmt = (
            select(
                Marriage.user1_id,
                Marriage.user2_id,
                Marriage.created_at,
                Marriage.experience,
                Marriage.duration_days,
                u1.first_name,
                u2.first_name,
            )
            .join(u1, u1.user_id == Marriage.user1_id, isouter=True)
            .join(u2, u2.user_id == Marriage.user2_id, isouter=True)
            .where(
                Marriage.chat_id == chat_id,
                legacy_status_active(Marriage.status),
                _on_the_board(),
            )
            .order_by(Marriage.experience.desc(), Marriage.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            MarriagePair(
                user1_id=r[0],
                user2_id=r[1],
                created_at=r[2],
                experience=r[3] or 0,
                extra_days=r[4] or 0,
                user1_name=r[5],
                user2_name=r[6],
            )
            for r in rows
        ]

    async def list_for_user(self, user_id: int, limit: int) -> list[UserBond]:
        """This user's active marriages in EVERY chat, strongest bond first.

        Ordering matches :meth:`list_active` (``experience DESC,
        created_at ASC``) so a couple's standing reads the same on the
        group leaderboard and on the owner's private card.

        The leaderboard is per-group; the profile hub is a DM, where the
        viewer has no "current chat" to scope by. ``limit`` is applied in
        SQL rather than in the handler so a user married in a hundred
        groups can't turn one card render into a hundred-row transfer.
        """
        u1 = aliased(User)
        u2 = aliased(User)
        stmt = (
            select(
                Marriage.chat_id,
                Marriage.user1_id,
                Marriage.user2_id,
                Marriage.created_at,
                Marriage.experience,
                u1.first_name,
                u2.first_name,
            )
            .join(u1, u1.user_id == Marriage.user1_id, isouter=True)
            .join(u2, u2.user_id == Marriage.user2_id, isouter=True)
            .where(
                or_(Marriage.user1_id == user_id, Marriage.user2_id == user_id),
                legacy_status_active(Marriage.status),
            )
            .order_by(Marriage.experience.desc(), Marriage.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        out: list[UserBond] = []
        for r in rows:
            partner_id, partner_name = _partner_of(user_id, r[1], r[2], r[5], r[6])
            out.append(
                UserBond(
                    chat_id=r[0],
                    partner_id=partner_id,
                    partner_name=partner_name,
                    experience=r[4] or 0,
                    created_at=r[3],
                )
            )
        return out


class RelationshipsRepo:
    """``users.relationships`` access."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_active(self, chat_id: int) -> int:
        """How many active relationships the chat has.

        Same role as :meth:`MarriagesRepo.count_active` — the "…and N
        more" line needs a total the capped list can't supply.
        """
        stmt = (
            select(func.count())
            .select_from(Relationship)
            .where(
                Relationship.chat_id == chat_id,
                legacy_status_active(Relationship.status),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_active(self, chat_id: int, *, limit: int) -> list[RelationshipPair]:
        """Same shape and ordering rules as :meth:`MarriagesRepo.list_active`.

        Mirrors ``bot.py:23486-23492``: ``status IS NULL`` ⇒ active for
        legacy-migrated rows.

        Unlike the marriage twin, ``limit`` is applied in Python rather
        than in SQL — for the reason spelled out in
        :meth:`list_for_user`: SQL can only order by the stored
        (pre-decay) XP, so truncating there could drop a couple that
        outranks a kept one once decay is applied.
        """
        u1 = aliased(User)
        u2 = aliased(User)
        stmt = (
            select(
                Relationship.user1_id,
                Relationship.user2_id,
                Relationship.created_at,
                Relationship.experience,
                u1.first_name,
                u2.first_name,
                Relationship.last_activity_at,
            )
            .join(u1, u1.user_id == Relationship.user1_id, isouter=True)
            .join(u2, u2.user_id == Relationship.user2_id, isouter=True)
            .where(
                Relationship.chat_id == chat_id,
                legacy_status_active(Relationship.status),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        # Apply inactivity decay to each pair's displayed XP, then sort by
        # the DECAYED value (the leaderboard must reflect current standings,
        # not all-time peaks). Persistence of the decayed value happens via
        # ``BondsWriteRepo.get_relationship`` on the next per-pair read.
        # Naive-LOCAL to match the writers' last_activity_at frame (REV-2).
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        pairs = [
            RelationshipPair(
                user1_id=r[0],
                user2_id=r[1],
                created_at=r[2],
                experience=_decayed_experience(r[3] or 0, r[6], now)[0],
                user1_name=r[4],
                user2_name=r[5],
            )
            for r in rows
        ]
        pairs.sort(key=lambda p: (-p.experience, p.created_at))
        return pairs[:limit]

    async def list_for_user(self, user_id: int, limit: int) -> list[UserBond]:
        """This user's active relationships in EVERY chat (RR-1 #2).

        Decay is applied on read exactly as in :meth:`list_active`, so the
        profile card and the group leaderboard never disagree about the
        same couple's XP. That forces the ``limit`` to be applied in Python
        after re-sorting: SQL orders by the stored (pre-decay) value, and
        truncating there could drop a couple that outranks a kept one once
        decay is accounted for.
        """
        u1 = aliased(User)
        u2 = aliased(User)
        stmt = (
            select(
                Relationship.chat_id,
                Relationship.user1_id,
                Relationship.user2_id,
                Relationship.created_at,
                Relationship.experience,
                u1.first_name,
                u2.first_name,
                Relationship.last_activity_at,
            )
            .join(u1, u1.user_id == Relationship.user1_id, isouter=True)
            .join(u2, u2.user_id == Relationship.user2_id, isouter=True)
            .where(
                or_(
                    Relationship.user1_id == user_id,
                    Relationship.user2_id == user_id,
                ),
                legacy_status_active(Relationship.status),
            )
            .order_by(Relationship.created_at.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        bonds: list[UserBond] = []
        for r in rows:
            partner_id, partner_name = _partner_of(user_id, r[1], r[2], r[5], r[6])
            bonds.append(
                UserBond(
                    chat_id=r[0],
                    partner_id=partner_id,
                    partner_name=partner_name,
                    experience=_decayed_experience(r[4] or 0, r[7], now)[0],
                    created_at=r[3],
                )
            )
        # Stable sort on the decayed value only: ``list.sort`` is stable
        # and the SQL ``ORDER BY created_at`` above already ordered the
        # rows, so equal-XP couples keep their oldest-first sequence
        # without a second key. NOT because ``created_at`` is unsafe to
        # compare in Python — it is a plain ``DateTime`` column
        # (db/models/users.py:319) that the SQLite dialect decodes for
        # legacy and fresh writes alike, which is exactly why
        # ``RelationshipsRepo.list_active`` compares it directly: that
        # statement carries no ``ORDER BY``, so its Python sort is the
        # only source of the age tiebreak.
        bonds.sort(key=lambda b: -b.experience)
        return bonds[:limit]


def _pair(u1: int, u2: int) -> tuple[int, int]:
    """Canonical (lower, higher) pair used for all bond writes.

    Mirrors ``bot.py:21576``. Storing the pair in canonical order lets
    both ``WHERE user1_id = ? AND user2_id = ?`` and the UNIQUE index work
    regardless of which user initiated the action.
    """
    return (min(u1, u2), max(u1, u2))


class BondsWriteRepo:
    """Write-side mutations for the marriage / relationship subsystem.

    Introduced in Stage T-019. The read-only leaderboard repos
    (``MarriagesRepo``, ``RelationshipsRepo``) stay unchanged; the write
    side lives here so the two concerns don't bleed.

    All public methods share the same :class:`AsyncSession` injected at
    construction, so the handler sees a single commit / rollback boundary.
    """

    # Minimum relationship level required to propose/accept marriage.
    # Mirrors ``bot.py:22099``.
    MARRIAGE_MIN_REL_LEVEL: int = 6
    # XP thresholds per relationship level (0-indexed, level N ≥ LEVEL_XP[N]).
    # Mirrors ``bot.py:22097``.
    RELATIONSHIP_LEVEL_XP: tuple[int, ...] = (
        0,
        150,
        1500,
        5000,
        10000,
        30000,
        60000,
        150000,
        300000,
        1_000_000,
        3_000_000,
        10_000_000,
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rel_xp_to_level(self, experience: int) -> int:
        """Level 0-11; mirrors ``bot.py:22197``."""
        level = 0
        for idx in range(1, len(self.RELATIONSHIP_LEVEL_XP)):
            if experience >= self.RELATIONSHIP_LEVEL_XP[idx]:
                level = idx
        return level

    async def get_first_name(self, user_id: int) -> str | None:
        """``users.first_name`` for one id, or ``None`` when unknown.

        Bond cards name the *other* party, who is usually not the sender
        of the update — so their name has to come from the DB rather than
        from ``message.from_user``. ``handlers/marriage`` and
        ``handlers/couple_activities`` each carried a private copy of this
        query that reached into ``repo._session`` and imported ``User``
        inside the function body; the read belongs on this side of the
        boundary, next to the bond queries that feed the same cards.

        ``None`` (no row) and ``""`` (row without a name) are both left
        for the caller to turn into a label — see
        :func:`~telegram_invite_bot.utils.names.display_name`.
        """
        result = await self._session.execute(select(User.first_name).where(User.user_id == user_id))
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Marriage queries
    # ------------------------------------------------------------------

    async def get_marriage(self, chat_id: int, user_id: int) -> Marriage | None:
        """Return the active marriage row for ``user_id`` in ``chat_id``, or ``None``.

        Mirrors ``_get_marriage`` at ``bot.py:21860`` — the pair is stored
        in normalised (lower, higher) order but the user can be either
        side, so we probe both columns.
        """
        stmt = (
            select(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                or_(Marriage.user1_id == user_id, Marriage.user2_id == user_id),
                legacy_status_active(Marriage.status),
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_relationship(
        self, chat_id: int, user_id: int, partner_id: int
    ) -> Relationship | None:
        """Return the active relationship row for the pair, or ``None``.

        Mirrors ``_get_relationship_with_partner`` at ``bot.py:21958``.
        """
        a, b = _pair(user_id, partner_id)
        stmt = (
            select(Relationship)
            .where(
                Relationship.chat_id == chat_id,
                Relationship.user1_id == a,
                Relationship.user2_id == b,
                legacy_status_active(Relationship.status),
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        rel = result.scalar_one_or_none()
        if rel is not None:
            await self._apply_decay(rel)
        return rel

    async def _apply_decay(self, rel: Relationship) -> None:
        """Lazily decay an inactive couple's XP on read and persist it.

        Mirrors legacy ``_apply_relationship_decay`` (bot.py:21884): when a
        full day or more has elapsed since ``last_activity_at``, drop the
        experience by 5/day (floored at 0) and reset ``last_activity_at`` to
        now. Idempotent — same-day re-reads consume 0 days and write
        nothing. The returned ORM row carries the decayed value either way,
        so gating/eligibility see the current level even on a read-only
        path that never commits.

        #1932: the write is a compare-and-set, not a plain ORM attribute
        assignment. ``experience`` is the one counter in this file read
        in Python and written back as an ABSOLUTE value; every other one
        (:meth:`add_relationship_xp`, :meth:`add_marriage_xp`) is an
        in-SQL ``experience = experience + xp``. The SELECT that loaded
        ``rel`` ran in autocommit — ``_promote_to_write_txn`` takes the
        write lock only on a write-headed statement — so two overlapping
        updates on one bond (a double-tapped couple activity; one partner
        on ``/rp`` while the other runs an activity) both read the same
        pre-decay row, and the second one's stale absolute UPDATE erased
        the XP the first had already granted. The user was charged for
        the activity and got nothing: no coins were lost, but a paid
        effect vanished with nothing to attribute it to.

        The guard is ``experience`` rather than ``last_activity_at``
        because it is an Integer and cannot be confused by a
        legacy-migrated row whose timestamp decodes as an ISO string
        (:func:`_decayed_experience` defends against exactly that). A
        lost race means somebody else already bumped the row, which also
        reset the activity clock — so skipping the decay is the correct
        outcome, not a fallback. ``days >= 1`` guarantees
        ``new_exp != experience`` unless the row already sits at 0, where
        re-running it is a no-op anyway.
        """
        # Naive-LOCAL to match the writers' last_activity_at frame (REV-2).
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        observed_exp = rel.experience or 0
        new_exp, days = _decayed_experience(observed_exp, rel.last_activity_at, now)
        if days <= 0:
            return
        stmt = (
            update(Relationship)
            .where(
                Relationship.id == rel.id,
                Relationship.experience == observed_exp,
            )
            .values(experience=new_exp, last_activity_at=now)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._session.execute(stmt)
        # ``Result`` does not declare ``rowcount``; the CursorResult the
        # DBAPI actually hands back does. Cast rather than ignore, the
        # way ``moderation_repo`` does — the attribute is the contract
        # this branch rests on, not an incidental one.
        if cast("CursorResult[Any]", result).rowcount == 0:
            # Somebody committed between our SELECT and this UPDATE. The
            # docstring's contract is that the caller reads a CURRENT
            # row, so re-read rather than hand back what we loaded.
            await self._session.refresh(rel)

    # ------------------------------------------------------------------
    # Marriage proposals
    # ------------------------------------------------------------------

    async def propose_marriage(self, chat_id: int, from_id: int, to_id: int) -> MarriageProposal:
        """Insert a new proposal row and return it with its PK set.

        Mirrors ``_insert_marriage_proposal`` at ``bot.py:22455``. The
        caller is responsible for checking that neither party is already
        married before calling this.
        """
        prop = MarriageProposal(
            chat_id=chat_id,
            from_id=from_id,
            to_id=to_id,
            created_at=datetime.now(),  # noqa: DTZ005  (mirrors legacy naive-local)
        )
        self._session.add(prop)
        await self._session.flush()  # assigns PK without full commit
        return prop

    async def get_latest_proposal_for(self, chat_id: int, to_id: int) -> MarriageProposal | None:
        """Return the most recent *pending* proposal addressed to ``to_id``.

        Mirrors ``_resolve_marriage_proposal_id`` (DB branch) at
        ``bot.py:22490``. R-FIX-010: scoped to ``status='pending'`` so
        already-resolved rows can't resurface to a second /marry_accept.
        """
        stmt = (
            select(MarriageProposal)
            .where(
                MarriageProposal.chat_id == chat_id,
                MarriageProposal.to_id == to_id,
                MarriageProposal.status == "pending",
            )
            .order_by(MarriageProposal.id.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_proposal_by_id(self, prop_id: int, chat_id: int) -> MarriageProposal | None:
        """Fetch a single proposal by PK + chat scope.

        Mirrors the inner SELECT at ``bot.py:22517``.
        """
        stmt = select(MarriageProposal).where(
            MarriageProposal.id == prop_id,
            MarriageProposal.chat_id == chat_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_proposal(self, prop_id: int) -> None:
        """Remove a proposal row after accept or decline."""
        await self._session.execute(delete(MarriageProposal).where(MarriageProposal.id == prop_id))

    async def _claim_proposal(self, prop_id: int, new_status: str) -> bool:
        """Atomically flip ``status='pending'`` → ``new_status``.

        Returns ``True`` when this caller won the claim and ``False`` if
        the row was already in some non-pending state (the row no longer
        exists, or another concurrent accept/decline got there first).
        This is the lock primitive that closes the double-accept race
        (R-FIX-010).
        """
        stmt = (
            update(MarriageProposal)
            .where(
                MarriageProposal.id == prop_id,
                MarriageProposal.status == "pending",
            )
            .values(status=new_status)
            .returning(MarriageProposal.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------
    # Marriage writes
    # ------------------------------------------------------------------

    async def accept_proposal(self, prop: MarriageProposal) -> tuple[bool, str]:
        """Accept a marriage proposal: insert or restore the marriage row.

        Returns ``(True, "")`` on success, ``(False, error_key)`` on
        a business-rule violation (already-married collision, integrity
        error). The caller is responsible for:

        * Verifying ``prop.to_id == acting_user_id`` *before* calling.
        * Re-checking the relationship level (delegated here so the repo
          holds the full invariant set).

        Concurrency: claims the proposal row via an atomic
        ``UPDATE ... WHERE status='pending'`` (R-FIX-010). If a parallel
        accept/decline already resolved it, raises
        :class:`ProposalAlreadyResolvedError` *before* any marriage
        write — only one caller can ever create the bond.

        Mirrors ``_process_marriage_proposal_response(accept=True)`` at
        ``bot.py:22500``.
        """
        from_id = prop.from_id
        to_id = prop.to_id
        chat_id = prop.chat_id

        # Atomic claim: only the caller that flips status pending→accepted
        # proceeds. A concurrent /marry_accept (slash + button) loses the
        # race here and surfaces ProposalAlreadyResolvedError without
        # ever touching the marriages table.
        if not await self._claim_proposal(prop.id, "accepted"):
            raise ProposalAlreadyResolvedError(prop.id)

        # Relationship-level gate
        rel = await self.get_relationship(chat_id, from_id, to_id)
        rel_level = 0 if rel is None else self._rel_xp_to_level(rel.experience or 0)
        if rel_level < self.MARRIAGE_MIN_REL_LEVEL:
            await self.delete_proposal(prop.id)
            return False, "marry_need_rel_level6_accept"

        # Both parties must be free (or the row belongs to this pair)
        a, b = _pair(from_id, to_id)
        for uid in (from_id, to_id):
            existing = await self._session.execute(
                select(Marriage).where(
                    Marriage.chat_id == chat_id,
                    or_(Marriage.user1_id == uid, Marriage.user2_id == uid),
                    legacy_status_active(Marriage.status),
                )
            )
            row = existing.scalar_one_or_none()
            if row is not None and not (row.user1_id == a and row.user2_id == b):
                await self.delete_proposal(prop.id)
                return False, "marry_already_married_accept"

        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)

        # Restore within the 3-day window
        restore_stmt = (
            update(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                Marriage.user1_id == a,
                Marriage.user2_id == b,
                Marriage.status == "divorced",
                Marriage.restore_until > now,
            )
            .values(status="active", divorced_at=None, restore_until=None)
        )
        # Same cast as above: ``rowcount`` lives on the CursorResult.
        result = await self._session.execute(restore_stmt)
        matched: int = cast("CursorResult[Any]", result).rowcount
        if matched == 0:
            # #2018: the same pair, past its restore window. Legacy fell
            # straight through to the INSERT below, which
            # ``UNIQUE(chat_id, user1_id, user2_id)`` rejects every time
            # — so a couple who divorced and waited four days were told
            # "couldn't save, try again" forever. Nothing could get them
            # out of it: no command deletes a marriage row (there is no
            # ``delete(Marriage)`` in this package at all), and waiting
            # only ages the row further past the window.
            #
            # So revive the row instead. ``experience`` is deliberately
            # left alone — the in-window restore above keeps it and so
            # does :meth:`_create_relationship` when it reactivates an
            # ``ended`` pair, and a third rule here would be a rule
            # nobody could predict. ``created_at`` IS reset, for the
            # same reason that method resets it: the longevity tier on
            # the ``/marriage`` card is computed from that date
            # (``utils/bonds.marriage_category``), and a pair who spent
            # a year apart did not spend it married.
            #
            # Scoped to ``divorced`` rather than "anything not restored"
            # on purpose: the gate above lets a pair who are ALREADY
            # married through to here, and matching their row would
            # silently reset the anniversary of a live marriage.
            revive_stmt = (
                update(Marriage)
                .where(
                    Marriage.chat_id == chat_id,
                    Marriage.user1_id == a,
                    Marriage.user2_id == b,
                    Marriage.status == "divorced",
                )
                .values(status="active", divorced_at=None, restore_until=None, created_at=now)
            )
            revived = await self._session.execute(revive_stmt)
            matched = revived.rowcount  # type: ignore[attr-defined]
        if matched == 0:
            # Fresh insert
            # R15: the insert goes inside a SAVEPOINT. Catching the flush
            # error is not enough — a failed flush deactivates the whole
            # transaction, so without the savepoint the ``delete_proposal``
            # below raises instead of running and ``marry_save_error``
            # (translated in both languages) is unreachable: the user gets
            # a crash where a "try again" was written for them.
            try:
                async with self._session.begin_nested():
                    self._session.add(
                        Marriage(
                            chat_id=chat_id,
                            user1_id=a,
                            user2_id=b,
                            created_at=now,
                            experience=0,
                            status="active",
                            in_top=1,
                        )
                    )
                    await self._session.flush()
            except IntegrityError:
                # #433: narrowed from a bare ``except Exception`` to match
                # legacy exactly — ``bot.py:22561`` catches
                # ``sqlite3.IntegrityError`` around this same INSERT and
                # nothing else. The only failure the path can honestly
                # absorb is UNIQUE(chat_id, user1_id, user2_id) firing:
                # the pair already has a row the restore UPDATE above
                # didn't match (still active, or divorced past its
                # ``restore_until``). "Try again" is the right answer to
                # that and to nothing else — a swallowed OperationalError
                # or a swallowed bug used to reach the user as a
                # transient hiccup and was logged nowhere, so it could
                # repeat forever unnoticed. Everything else now
                # propagates to the errors middleware. The savepoint has
                # already rolled back either way, so the outer
                # transaction stays usable.
                await self.delete_proposal(prop.id)
                return False, "marry_save_error"

        await self.delete_proposal(prop.id)
        return True, ""

    async def decline_proposal(self, prop_id: int) -> None:
        """Decline the proposal without inserting any marriage row.

        Claims the row atomically (status pending→declined) and then
        deletes it, mirroring legacy behaviour. If another caller has
        already accepted/declined the proposal, raises
        :class:`ProposalAlreadyResolvedError` so the handler can surface
        a friendly "already handled" reply (R-FIX-010).
        """
        if not await self._claim_proposal(prop_id, "declined"):
            raise ProposalAlreadyResolvedError(prop_id)
        await self.delete_proposal(prop_id)

    async def soft_divorce(self, chat_id: int, user_id: int) -> bool:
        """Soft-delete the active marriage: ``status='divorced'``, restore window 3 days.

        Returns ``True`` if a marriage was found and updated, ``False``
        if the user has no active marriage in this chat.

        Mirrors ``_delete_marriage`` at ``bot.py:22012``.
        """
        marriage = await self.get_marriage(chat_id, user_id)
        if marriage is None:
            return False
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        restore_end = now + _MARRIAGE_RESTORE_WINDOW
        await self._session.execute(
            update(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                Marriage.user1_id == marriage.user1_id,
                Marriage.user2_id == marriage.user2_id,
            )
            .values(status="divorced", divorced_at=now, restore_until=restore_end)
        )
        return True

    async def soft_divorce_all_in_chat(self, chat_id: int, user_id: int, *, now: datetime) -> int:
        """Soft-divorce EVERY active marriage ``user_id`` holds in ``chat_id``.

        The bulk sibling of :meth:`soft_divorce`, written for #482's
        absence sweep. Returns how many marriages were dissolved.

        Two reasons it is not a loop over :meth:`soft_divorce`:

        * **The clock.** :meth:`soft_divorce` reads ``datetime.now()``
          itself, which is right for a handler answering a live
          ``/divorce`` but leaves a background sweeper with nothing to
          pin in a test. ``now`` is a parameter here, in the same
          naive-LOCAL frame the column is written in.
        * **The count.** :meth:`soft_divorce` selects one row
          (``get_marriage`` is ``LIMIT 1``) and the ORM entity it returns
          would be stale after this statement's bulk UPDATE. Legacy swept
          *all* of a user's active marriages in the chat
          (``bot.py:21607-21612``); one statement matches that without
          the identity-map hazard of alternating a SELECT with a bulk
          UPDATE inside one session.

        Today the schema's ``UNIQUE(chat_id, user1_id, user2_id)`` makes
        two active marriages for the same pair impossible, but not two
        for the same *user* with different partners — legacy allowed for
        that and so does this.
        """
        stmt = (
            update(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                or_(Marriage.user1_id == user_id, Marriage.user2_id == user_id),
                legacy_status_active(Marriage.status),
            )
            .values(
                status="divorced",
                divorced_at=now,
                restore_until=now + _MARRIAGE_RESTORE_WINDOW,
            )
            .returning(Marriage.id)
        )
        return len((await self._session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------
    # Relationship writes
    # ------------------------------------------------------------------

    async def end_relationships_for(self, chat_id: int, user_id: int, *, now: datetime) -> int:
        """End every active relationship ``user_id`` holds in ``chat_id``.

        Returns how many rows were ended. Ports legacy's
        ``UPDATE relationships SET status='ended', ended_at=?``
        (``bot.py:21606``) — the absence-sweep dissolution, which is a
        SOFT end.

        DO NOT reach for :meth:`terminate_relationship` here. That method
        is a hard ``DELETE`` (legacy's ``/rel_break``,
        ``bot.py:22064``), and using it would erase the ``created_at``
        and accumulated ``experience`` of every couple whose member
        merely stopped being in the group — a much heavier outcome than
        the one legacy applies on absence, and one no restore window can
        walk back. The two paths look interchangeable from the outside
        and are not; this is why they are documented as a pair.

        ``ended_at`` is stamped from the caller's ``now`` in the
        naive-LOCAL frame the sibling columns use. ``status`` matches
        legacy's spelling exactly (``'ended'``, not ``'divorced'`` — that
        word belongs to marriages) because
        :func:`legacy_status_active` treats anything that is neither
        NULL nor ``'active'`` as inactive, so the value is what a human
        reading the table later sees, not what the code branches on.
        """
        stmt = (
            update(Relationship)
            .where(
                Relationship.chat_id == chat_id,
                or_(Relationship.user1_id == user_id, Relationship.user2_id == user_id),
                legacy_status_active(Relationship.status),
            )
            .values(status="ended", ended_at=now)
            .returning(Relationship.id)
        )
        return len((await self._session.execute(stmt)).scalars().all())

    async def terminate_relationship(self, chat_id: int, user_id: int, partner_id: int) -> bool:
        """Hard-delete the relationship row for the pair.

        Returns ``True`` if a row existed and was deleted, ``False``
        if the pair had no active relationship.

        Mirrors ``_delete_relationship_with_partner`` at ``bot.py:22064``.
        Unlike the marriage dissolution this is a hard DELETE — legacy
        does not implement a restore window for relationships.

        The pair's ``relationship_activity_log`` rows go with it (#2022).
        Legacy deletes the pair row and nothing else, and the log is
        addressed by ``(chat_id, user1_id, user2_id)`` alone — so the
        orphans reattach to whatever relationship those two form next,
        and the new couple's history card opens on the old couple's
        dinners, dated before the bond it hangs off existed and adding
        up to XP the pair's counter says they never earned.

        The rule is the one :meth:`accept_proposal` states for
        marriages: XP and history travel WITH the bond row. A bond that
        survives keeps both — the marriage revive keeps its experience,
        and so does :meth:`_create_relationship` when it reactivates a
        soft-ended pair, so both keep their log. A bond that is deleted
        keeps neither, and this is the only place in the package that
        deletes one. That is also why the soft
        :meth:`end_relationships_for` deliberately does NOT do this: it
        leaves the row standing precisely so the XP survives, and a log
        without the XP it explains would be worse than no log.

        Ordering: the log delete runs first, while
        :meth:`get_relationship` above has already proved the pair
        exists. A ``False`` return leaves both tables untouched.
        """
        rel = await self.get_relationship(chat_id, user_id, partner_id)
        if rel is None:
            return False
        a, b = _pair(user_id, partner_id)
        await self._session.execute(
            delete(RelationshipActivityLog).where(
                RelationshipActivityLog.chat_id == chat_id,
                RelationshipActivityLog.user1_id == a,
                RelationshipActivityLog.user2_id == b,
            )
        )
        await self._session.execute(
            delete(Relationship).where(
                Relationship.chat_id == chat_id,
                Relationship.user1_id == a,
                Relationship.user2_id == b,
            )
        )
        return True

    # ------------------------------------------------------------------
    # RP-action XP grants (FEAT-RP)
    # ------------------------------------------------------------------

    async def add_relationship_xp(
        self, chat_id: int, user_id: int, partner_id: int, xp: int
    ) -> int | None:
        """Add ``xp`` to the pair's active relationship; bump last-activity.

        In-SQL ``UPDATE ... RETURNING experience`` against the normalised
        (lower, higher) pair. Returns the NEW experience total, or ``None``
        when no active relationship row exists for the pair. ``xp`` may be
        0 (e.g. the sex verbs) — the row's ``last_activity_at`` is still
        bumped so a 0-XP action counts as activity.

        No activity-log row is written HERE. The log is a caller's
        concern: whoever wants one calls
        :meth:`log_relationship_activity` on the same session, so the row
        and the XP it records share a commit boundary. Legacy fused the
        two inside ``relationship_add_xp`` (bot.py:22382) and gated the
        INSERT on a truthy ``activity_key``; splitting them is what let
        the RP path silently forget to log at all (#231).
        """
        a, b = _pair(user_id, partner_id)
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        stmt = (
            update(Relationship)
            .where(
                Relationship.chat_id == chat_id,
                Relationship.user1_id == a,
                Relationship.user2_id == b,
                legacy_status_active(Relationship.status),
            )
            .values(
                experience=Relationship.experience + xp,
                last_activity_at=now,
            )
            .returning(Relationship.experience)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def add_marriage_xp(self, chat_id: int, user_id: int, xp: int) -> int | None:
        """Add ``xp`` to the user's active marriage; bump last-activity.

        Same contract as :meth:`add_relationship_xp` but scoped to the
        user's marriage (the user may be either party). Returns the new
        experience total or ``None`` when the user has no active marriage
        in this chat. ``xp`` may be 0.
        """
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        stmt = (
            update(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                or_(Marriage.user1_id == user_id, Marriage.user2_id == user_id),
                legacy_status_active(Marriage.status),
            )
            .values(
                experience=Marriage.experience + xp,
                last_activity_at=now,
            )
            .returning(Marriage.experience)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Marriage settings / extension (L-04/05/06)
    # ------------------------------------------------------------------

    async def set_marriage_in_top(self, chat_id: int, user_id: int, *, in_top: bool) -> bool:
        """Toggle the active marriage's ``in_top`` flag (1/0).

        Either spouse may own the toggle (the user may be either party).
        Returns ``True`` when a row matched, ``False`` when the user has
        no active marriage in this chat. Mirrors ``cmd_marry_top_on`` /
        ``cmd_marry_top_off`` at ``bot.py:22728``/``bot.py:22746``.
        """
        stmt = (
            update(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                or_(Marriage.user1_id == user_id, Marriage.user2_id == user_id),
                legacy_status_active(Marriage.status),
            )
            .values(in_top=1 if in_top else 0)
            .returning(Marriage.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def extend_marriage(self, chat_id: int, user_id: int, days: int) -> bool:
        """Increment ``duration_days`` by ``days`` and stamp ``last_extended``.

        Either spouse may extend (the user may be either party). Returns
        ``True`` when a row matched, ``False`` when the user has no active
        marriage in this chat. The coin debit is the caller's
        responsibility (see ``handlers/marriage.handle_marry_extend``);
        this method only mutates the bond row. Mirrors ``cmd_marry_extend``
        at ``bot.py:22764``.
        """
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        stmt = (
            update(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                or_(Marriage.user1_id == user_id, Marriage.user2_id == user_id),
                legacy_status_active(Marriage.status),
            )
            .values(
                duration_days=func.coalesce(Marriage.duration_days, 0) + days,
                last_extended=now,
            )
            .returning(Marriage.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def set_marriage_auto_divorce(self, chat_id: int, user_id: int, mode: str) -> bool:
        """Set the active marriage's ``auto_divorce`` mode (``off``/``one``/``two``).

        Either spouse may set it. The caller validates ``mode`` before
        calling. Returns ``True`` when a row matched, ``False`` when the
        user has no active marriage in this chat. Mirrors
        ``cmd_marry_auto_divorce`` at ``bot.py:22796``.
        """
        stmt = (
            update(Marriage)
            .where(
                Marriage.chat_id == chat_id,
                or_(Marriage.user1_id == user_id, Marriage.user2_id == user_id),
                legacy_status_active(Marriage.status),
            )
            .values(auto_divorce=mode)
            .returning(Marriage.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------
    # Relationship proposals (A-02 unbrick)
    # ------------------------------------------------------------------

    async def list_relationships_for(self, chat_id: int, user_id: int) -> list[RelationshipView]:
        """All active relationships where ``user_id`` is either party.

        Ports ``_get_relationships_list`` at ``bot.py:21916``. Three
        callers depend on the order, one of them for more than looks:
        ``marriage.handle_relationship`` truncates the rendered list
        to ``_REL_LIST_MAX`` and drops the tail,
        ``rp.handle_rp_commands`` folds it to a max level, and
        ``couple_activities.handle_activities`` takes ``rels[0]`` as
        *the* pair the activity menu will act on.

        #477: ordering is now by the DECAYED experience, computed per row
        here, and the rows come back as :class:`RelationshipView` rather
        than live ORM entities so that a read cannot persist decay. The
        previous version ordered by the STORED column in SQL and claimed
        to mirror legacy's ordering. Both halves were wrong: decay is
        applied lazily, so the stored value can be arbitrarily stale, and
        legacy's query (``bot.py:21918-21922``) has no ``ORDER BY`` at
        all — it returns rows in rowid order and never sorts them in
        Python either (``bot.py:21946-21955``). So there was no legacy
        ordering to mirror, and the one being applied could put a bond
        that has decayed to nothing above a live one. The decayed value
        is what every caller renders (legacy decayed per row too,
        ``bot.py:21949``), so sorting by it is the only self-consistent
        choice; matching legacy's *absent* order would mean showing the
        list in rowid order, which is worse for all three callers.

        Deliberate divergence from legacy: legacy's per-row
        ``_apply_relationship_decay`` WROTE the decayed value back
        (``bot.py:21912``) — one UPDATE per bond per list render. Here
        the write stays where it belongs, on the per-pair read in
        :meth:`get_relationship`; this method only displays. The numbers
        shown are identical either way.
        """
        stmt = (
            select(
                Relationship.id,
                Relationship.user1_id,
                Relationship.user2_id,
                Relationship.experience,
                Relationship.created_at,
                Relationship.last_activity_at,
            )
            .where(
                Relationship.chat_id == chat_id,
                or_(
                    Relationship.user1_id == user_id,
                    Relationship.user2_id == user_id,
                ),
                legacy_status_active(Relationship.status),
            )
            .order_by(Relationship.created_at.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        # Naive-LOCAL, matching the frame the bond writers stamp
        # ``last_activity_at`` in (see :func:`_decayed_experience`).
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        views = [
            RelationshipView(
                id=r[0],
                user1_id=r[1],
                user2_id=r[2],
                experience=_decayed_experience(r[3] or 0, r[5], now)[0],
                created_at=r[4],
            )
            for r in rows
        ]
        # Stable sort on the decayed value only, same reasoning as
        # :meth:`list_for_user`: the SQL ``ORDER BY created_at`` above
        # already supplies the oldest-first tiebreak and ``list.sort``
        # preserves it. ``created_at`` is a plain ``DateTime`` column
        # (db/models/users.py:365) and compares fine in Python — a
        # second key is simply redundant HERE, unlike in
        # ``RelationshipsRepo.list_active`` whose statement has no
        # ``ORDER BY``.
        views.sort(key=lambda v: -v.experience)
        return views

    async def propose_relationship(
        self, chat_id: int, from_id: int, to_id: int
    ) -> RelationshipProposal:
        """Insert a new relationship proposal and return it with its PK set.

        Mirrors ``_insert_relationship_proposal`` at ``bot.py:22071``. The
        caller checks self/bot/already-together before calling.
        """
        prop = RelationshipProposal(
            chat_id=chat_id,
            from_id=from_id,
            to_id=to_id,
            created_at=datetime.now(),  # noqa: DTZ005  (mirrors legacy naive-local)
        )
        self._session.add(prop)
        await self._session.flush()  # assigns PK without full commit
        return prop

    async def get_relationship_proposal_by_id(
        self, prop_id: int, chat_id: int
    ) -> RelationshipProposal | None:
        """Fetch a single relationship proposal by PK + chat scope.

        Mirrors the inner SELECT at ``bot.py:23222`` / ``bot.py:23265``.
        """
        stmt = select(RelationshipProposal).where(
            RelationshipProposal.id == prop_id,
            RelationshipProposal.chat_id == chat_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_relationship_proposal(self, prop_id: int) -> None:
        """Remove a relationship proposal row after accept or decline."""
        await self._session.execute(
            delete(RelationshipProposal).where(RelationshipProposal.id == prop_id)
        )

    async def _claim_relationship_proposal(self, prop_id: int, new_status: str) -> bool:
        """Atomically flip ``status='pending'`` → ``new_status``.

        Same lock primitive as :meth:`_claim_proposal` for marriages —
        closes the double-tap accept race on the inline ✅ button.
        """
        stmt = (
            update(RelationshipProposal)
            .where(
                RelationshipProposal.id == prop_id,
                RelationshipProposal.status == "pending",
            )
            .values(status=new_status)
            .returning(RelationshipProposal.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def _create_relationship(self, chat_id: int, u1: int, u2: int) -> bool:
        """Create or reactivate the relationship row for the pair.

        Mirrors ``_create_relationship`` at ``bot.py:22032``: insert a
        fresh row; if a pair already exists (incl. ``status='ended'``),
        reactivate it (``status='active'``, ``ended_at=NULL``, reset
        ``created_at``). Returns ``True`` on success.
        """
        a, b = _pair(u1, u2)
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        existing = await self._session.execute(
            select(Relationship).where(
                Relationship.chat_id == chat_id,
                Relationship.user1_id == a,
                Relationship.user2_id == b,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.status = "active"
            row.ended_at = None
            row.created_at = now
            await self._session.flush()
            return True
        # R15: same SAVEPOINT reasoning as ``_create_marriage`` — the
        # ``False`` here is meant to send the caller down its
        # delete-the-proposal cleanup path, which cannot run on a
        # transaction the failed flush has already deactivated.
        try:
            async with self._session.begin_nested():
                self._session.add(
                    Relationship(
                        chat_id=chat_id,
                        user1_id=a,
                        user2_id=b,
                        created_at=now,
                        experience=0,
                        status="active",
                    )
                )
                await self._session.flush()
        except IntegrityError:
            # #433: narrowed for the same reason as ``_create_marriage``
            # above — the UNIQUE(chat_id, user1_id, user2_id) firing is the
            # one failure a bare ``False`` describes honestly. Everything
            # else now reaches the errors middleware instead of being
            # reported to the user as an ordinary "couldn't save".
            return False
        return True

    async def accept_relationship_proposal(self, prop: RelationshipProposal) -> bool:
        """Accept a relationship proposal: create or reactivate the pair.

        Returns ``True`` on success, ``False`` on a save error. The
        caller verifies ``prop.to_id == acting_user_id`` *before* calling.

        Concurrency: claims the proposal row via an atomic
        ``UPDATE ... WHERE status='pending'``. If a parallel accept/decline
        already resolved it, raises :class:`ProposalAlreadyResolvedError`
        *before* any relationship write.

        Mirrors ``callback_rel_accept`` at ``bot.py:23209``.
        """
        if not await self._claim_relationship_proposal(prop.id, "accepted"):
            raise ProposalAlreadyResolvedError(prop.id)
        if not await self._create_relationship(prop.chat_id, prop.from_id, prop.to_id):
            await self.delete_relationship_proposal(prop.id)
            return False
        await self.delete_relationship_proposal(prop.id)
        return True

    async def decline_relationship_proposal(self, prop_id: int) -> None:
        """Decline the relationship proposal without creating any pair.

        Claims the row atomically (status pending→declined) then deletes
        it. Raises :class:`ProposalAlreadyResolvedError` when another
        caller already resolved it. Mirrors ``callback_rel_decline`` at
        ``bot.py:23252``.
        """
        if not await self._claim_relationship_proposal(prop_id, "declined"):
            raise ProposalAlreadyResolvedError(prop_id)
        await self.delete_relationship_proposal(prop_id)

    # ------------------------------------------------------------------
    # Couple-activity history log (L-34 / L-35)
    # ------------------------------------------------------------------
    #
    # Append-only logs that back the marriage/relationship card "history"
    # views. Writes happen from handlers/couple_activities right after a
    # successful XP grant (same users.db session, same commit boundary as
    # the bond XP bump, so a logged row and the XP it records can't
    # diverge). Reads return the most recent 15 rows for the pair.
    #
    # ``created_at`` is stored as an ISO-8601 string to match the legacy
    # TEXT column (bot.py:21658 inserts ``datetime.now().isoformat``-style
    # timestamps); format_db_date renders either str or datetime.

    async def log_marriage_activity(
        self,
        chat_id: int,
        user1_id: int,
        user2_id: int,
        activity_key: str,
        xp_gained: int,
        paid_by_user_id: int,
    ) -> None:
        """Append a marriage joint-activity row. Pair stored canonically.

        Mirrors the INSERT inside legacy ``marriage_add_xp`` at
        ``bot.py:21658``. ``paid_by_user_id`` is the clicker who footed
        the coin cost (either spouse may pay).
        """
        a, b = _pair(user1_id, user2_id)
        self._session.add(
            MarriageActivityLog(
                chat_id=chat_id,
                user1_id=a,
                user2_id=b,
                activity_key=activity_key,
                xp_gained=xp_gained,
                paid_by_user_id=paid_by_user_id,
                created_at=datetime.now().isoformat(sep=" "),  # noqa: DTZ005  (mirrors legacy naive-local)
            )
        )

    async def log_relationship_activity(
        self,
        chat_id: int,
        user1_id: int,
        user2_id: int,
        activity_key: str,
        xp_gained: int,
        paid_by_user_id: int,
    ) -> None:
        """Append a relationship joint-activity row. Pair stored canonically.

        Mirrors the INSERT inside legacy ``relationship_add_xp`` at
        ``bot.py:22395``.
        """
        a, b = _pair(user1_id, user2_id)
        self._session.add(
            RelationshipActivityLog(
                chat_id=chat_id,
                user1_id=a,
                user2_id=b,
                activity_key=activity_key,
                xp_gained=xp_gained,
                paid_by_user_id=paid_by_user_id,
                created_at=datetime.now().isoformat(sep=" "),  # noqa: DTZ005  (mirrors legacy naive-local)
            )
        )

    async def get_marriage_activity_log(
        self, chat_id: int, user1_id: int, user2_id: int, limit: int = 15
    ) -> list[BondActivityEntry]:
        """Most recent ``limit`` marriage-activity rows for the pair (newest first).

        Mirrors ``_get_marriage_activity_log`` at ``bot.py:22371``.
        """
        a, b = _pair(user1_id, user2_id)
        stmt = (
            select(
                MarriageActivityLog.activity_key,
                MarriageActivityLog.xp_gained,
                MarriageActivityLog.created_at,
            )
            .where(
                MarriageActivityLog.chat_id == chat_id,
                MarriageActivityLog.user1_id == a,
                MarriageActivityLog.user2_id == b,
            )
            .order_by(MarriageActivityLog.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [BondActivityEntry(r[0], r[1], r[2]) for r in rows]

    async def get_relationship_activity_log(
        self, chat_id: int, user1_id: int, user2_id: int, limit: int = 15
    ) -> list[BondActivityEntry]:
        """Most recent ``limit`` relationship-activity rows for the pair.

        Mirrors ``_get_relationship_activity_log`` at ``bot.py:22360``.
        """
        a, b = _pair(user1_id, user2_id)
        stmt = (
            select(
                RelationshipActivityLog.activity_key,
                RelationshipActivityLog.xp_gained,
                RelationshipActivityLog.created_at,
            )
            .where(
                RelationshipActivityLog.chat_id == chat_id,
                RelationshipActivityLog.user1_id == a,
                RelationshipActivityLog.user2_id == b,
            )
            .order_by(RelationshipActivityLog.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [BondActivityEntry(r[0], r[1], r[2]) for r in rows]
