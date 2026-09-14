"""Write-side repository for the donations rating (L-38).

The donations-rating READ surface (``/rating``, ``/top_groups``,
``/rating_groups``, ``/mydonates``) landed in A-04 and reads
``economy.groups_donations`` directly. This repo owns the *admin write
toggles* that A-04 deliberately skipped:

* :meth:`save_group_identity` — backfill ``group_name`` / ``group_link``
  for a group the leaderboard could not render clickably (RR-1 #9).
* :meth:`set_in_rating` — include / exclude one group from the leaderboard.
  Backed by a NEW ``groups_donations.in_rating`` column (1 = ranked, the
  default; 0 = hidden). Legacy had no such flag — every group with
  ``group_xp > 0`` was always ranked — so the migration adds the column
  with a ``server_default`` of 1 to preserve that behaviour for existing
  rows.
* :meth:`recalc_positions` — recompute ``rating_position`` for every
  *included* group, densely ranked by ``group_xp`` DESC. Mirrors legacy
  ``recalc_donations_rating_positions`` (bot.py:10763), extended to skip
  excluded groups (which get ``rating_position = NULL``).
* :meth:`save_history_snapshot` — upsert today's ``(group_xp, position)``
  snapshot into ``rating_history`` for one group. Mirrors legacy
  ``_save_rating_history_for_group`` (bot.py:10784).
* :meth:`group_xp` — read back the score above, for a receipt that has
  to name what the board actually ranks on.
* :meth:`record_donation` (RR-2 #14) — the donation write path itself:
  ``donations`` row + ``group_top_donators`` upsert + ``group_xp`` bump.
  Mirrors the DB half of legacy ``donation_from_purchase``
  (bot.py:10715-10741), which is byte-identical to the DB half of
  legacy ``donations_do_donate`` (bot.py:10643-10711) — the two legacy
  writers differed only in where the coins came from. Both callers live
  in :class:`services.group_donation_service.GroupDonationService`
  (a group purchase's slice, #2007's ``/donate``), which orchestrates
  this method with the recalc + snapshot below.

Everything is raw ``text()`` SQL keyed on the NEW column / table. The
``rating_history`` table carries no ORM model — it is an append-only
daily snapshot nothing else reads through the session — so the repo
talks to the schema directly. The Alembic migration that adds the column
and table ships alongside (NOT applied here).

The session is shared with the caller; the repo flushes but does not
commit — the handler's ``session_for`` context owns the commit/rollback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, text

if TYPE_CHECKING:
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class DonationsRatingRepo:
    """``economy.groups_donations`` + ``economy.rating_history`` write ops."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_in_rating(self, group_id: int, *, included: bool) -> bool:
        """Toggle ``group_id``'s rating membership. Returns ``True`` iff a
        ``groups_donations`` row exists for the group (rowcount > 0).

        A group with no donations has no ``groups_donations`` row yet — the
        UPDATE matches nothing and the caller reports "this group has no
        donations to rank". Only the aggregate row is touched; positions
        are recomputed separately by :meth:`recalc_positions`.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                text("UPDATE groups_donations SET in_rating = :flag WHERE group_id = :gid"),
                {"flag": 1 if included else 0, "gid": group_id},
            ),
        )
        return result.rowcount > 0

    async def save_group_identity(
        self, group_id: int, *, link: str | None, title: str | None
    ) -> bool:
        """Backfill the leaderboard's two display columns for one group.

        Takes the place of legacy ``update_group_invite_link``
        (bot.py:10624), which the rating page called whenever it found a
        row it could not make clickable. Two deliberate differences: legacy
        wrote ``group_link`` unconditionally (so a failed lookup could blank
        a good stored link) and only COALESCE'd the name, and the name it
        passed was the one already in the row rather than the group's
        current title. Here *both* columns go through
        ``COALESCE(NULLIF(:x, ''), col)``, so a caller that learned only
        one of the two — a group whose title we can read but whose invite
        link the bot has no right to mint — updates that one and leaves the
        other alone rather than blanking a value someone else got right;
        and a non-empty title is a live one, so renames land.

        Returns ``True`` iff a ``groups_donations`` row exists for the
        group. Never creates one: a group with no donations has no place in
        the leaderboard, and inventing an aggregate row here would put it
        there with a zero total.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                text(
                    "UPDATE groups_donations SET "
                    "group_link = COALESCE(NULLIF(:link, ''), group_link), "
                    "group_name = COALESCE(NULLIF(:title, ''), group_name) "
                    "WHERE group_id = :gid"
                ),
                {
                    "link": (link or "").strip(),
                    "title": (title or "").strip(),
                    "gid": group_id,
                },
            ),
        )
        return result.rowcount > 0

    async def record_donation(
        self,
        *,
        group_id: int,
        user_id: int,
        amount: int,
        message: str,
        now: datetime,
    ) -> None:
        """Write the DB half of a donation to ``group_id``.

        Ports legacy ``donation_from_purchase`` (bot.py:10715-10741) —
        the group's cut of a shop purchase — and, since #2007, serves
        ``/donate`` as well: legacy's own ``donations_do_donate``
        (bot.py:10658-10690) writes the same four statements in the same
        order, so a second copy would only be a second place to drift.
        ``message`` is what tells the two apart in the ledger («С покупки
        в группе» vs the donor's comment, empty for a bare ``/donate`` —
        bot.py:10673 binds ``(comment or "")``).

        Four statements, in legacy's order:

        1. ``donations_ensure_group`` (bot.py:10584) — INSERT the aggregate
           row if the group has never received anything, with the same
           ``total_donations = 0`` / ``last_donation = NULL`` seed. Legacy's
           COALESCE-on-conflict branches for name/link/members are omitted:
           this caller passes none of them (bot.py:10723 calls it with the
           id alone), so every one of those clauses would be a no-op.
        2. append the ``donations`` ledger row (``message`` truncated to
           500 chars exactly as legacy does);
        3. upsert ``group_top_donators`` — the denormalised per-(group,
           user) lifetime counter ``/donaters`` reads;
        4. bump ``groups_donations.group_xp`` by the full slice and stamp
           ``last_donation``.

        Note what is deliberately NOT touched: ``total_donations``. Legacy
        leaves that column frozen (bot.py:10915 «Сейчас не пополняется»)
        and ranks purely on ``group_xp``; writing it here would make the
        two counters disagree and change what every donations surface
        shows. See ``services/treasury_service.py`` for the same rule.

        ``now`` is naive LOCAL time, not :func:`utils.time.db_now` — these
        are legacy-shared columns whose existing rows were written by
        ``datetime.now().isoformat(sep=" ", timespec="seconds")``
        (bot.py:10727), and mixing UTC into the same column would reorder
        history. Bound as that exact string rather than as a ``datetime``
        so the stored format matches legacy's byte-for-byte.
        """
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        await self._session.execute(
            text(
                "INSERT INTO groups_donations "
                "(group_id, total_donations, members_count, last_donation, created_at) "
                "VALUES (:gid, 0, 0, NULL, :now) "
                "ON CONFLICT(group_id) DO NOTHING"
            ),
            {"gid": group_id, "now": stamp},
        )
        await self._session.execute(
            text(
                "INSERT INTO donations (user_id, group_id, amount, message, created_at) "
                "VALUES (:uid, :gid, :amount, :message, :now)"
            ),
            {
                "uid": user_id,
                "gid": group_id,
                "amount": amount,
                "message": message[:500],
                "now": stamp,
            },
        )
        await self._session.execute(
            text(
                "INSERT INTO group_top_donators "
                "(group_id, user_id, total_donated, last_donate) "
                "VALUES (:gid, :uid, :amount, :now) "
                "ON CONFLICT(group_id, user_id) DO UPDATE SET "
                "total_donated = COALESCE(total_donated, 0) + excluded.total_donated, "
                "last_donate = excluded.last_donate"
            ),
            {"gid": group_id, "uid": user_id, "amount": amount, "now": stamp},
        )
        await self._session.execute(
            text(
                "UPDATE groups_donations SET "
                "group_xp = COALESCE(group_xp, 0) + :amount, last_donation = :now "
                "WHERE group_id = :gid"
            ),
            {"amount": amount, "now": stamp, "gid": group_id},
        )

    async def recalc_positions(self) -> int:
        """Recompute ``rating_position`` for every included group.

        Ranked groups are the ones ``/rating`` actually lists: included
        (``in_rating`` 1 or legacy-NULL) *and* holding some xp. They are
        densely ranked by ``COALESCE(group_xp, 0)`` DESC starting at 1;
        everyone else gets ``rating_position = NULL`` so a stale slot never
        lingers after an exclude — or after an xp reset, which used to leave
        a group parked on its old number forever. Returns the ranked count.

        Two statements rather than a window-function UPDATE: SQLite's
        ``UPDATE ... FROM (SELECT ROW_NUMBER() ...)`` support is version-
        dependent across the prod fleet, so the row numbering is done in
        Python over an ordered SELECT and written back one row at a time —
        the same shape legacy used (bot.py:10775). The group set is tiny
        (one row per group that ever received a donation), so the per-row
        UPDATE cost is negligible and runs inside the caller's single
        transaction.

        ``in_rating IS NULL OR in_rating != 0`` treats legacy rows (written
        before the column existed and thus NULL) as included, matching the
        migration's intent that the toggle is opt-OUT. The ``group_xp > 0``
        half mirrors ``handlers/rating.py``'s ``_ranked()``, which is what
        /rating, its counter and its drill-down all filter on: without it
        this method numbered groups the board does not render at all.
        """
        # Blank every position first, then hand numbers back only to the
        # ranked rows below. Clearing keeps the "who is ranked" test in
        # exactly one place — the SELECT — so the two can never disagree
        # about a group.
        #
        # The ``WHERE`` is not an optimisation, it is what makes this
        # statement runnable at all: ``db/safety.py`` installs a
        # ``before_cursor_execute`` listener on EVERY engine and raises
        # ``UnboundedWriteError`` for a WHERE-less UPDATE when
        # ``APP_ENV=prod``. Without it this line took down all three
        # callers in production — ``/rating_include`` and
        # ``/rating_exclude`` (``rating._apply_rating_toggle``, which
        # then never commits the flip it just made), ``/rating_recalc``
        # (``rating.handle_rating_recalc``), and the group-purchase
        # slice, whose SAVEPOINT in ``GroupDonationService.route``
        # unwound every write while ``shop._route_group_slice`` swallowed
        # the exception, so the buyer's group silently never climbed the
        # board. The predicate is a no-op semantically: a row already
        # holding NULL needs no clear.
        await self._session.execute(
            text(
                "UPDATE groups_donations SET rating_position = NULL "
                "WHERE rating_position IS NOT NULL"
            )
        )
        rows = (
            await self._session.execute(
                text(
                    "SELECT group_id FROM groups_donations "
                    "WHERE COALESCE(group_xp, 0) > 0 "
                    "AND (in_rating IS NULL OR in_rating != 0) "
                    "ORDER BY COALESCE(group_xp, 0) DESC, group_id ASC"
                )
            )
        ).all()
        for position, row in enumerate(rows, start=1):
            await self._session.execute(
                text("UPDATE groups_donations SET rating_position = :pos WHERE group_id = :gid"),
                {"pos": position, "gid": int(row[0])},
            )
        return len(rows)

    async def save_history_snapshot(self, group_id: int, *, today: date) -> bool:
        """Upsert today's ``(group_xp, rating_position)`` snapshot for one
        group into ``rating_history``. Returns ``True`` iff the group has a
        ``groups_donations`` row to snapshot.

        Mirrors legacy ``_save_rating_history_for_group`` (bot.py:10784):
        ``INSERT ... ON CONFLICT(group_id, date) DO UPDATE`` so re-running
        the recalc on the same day overwrites rather than duplicates the
        day's snapshot. ``date`` is bound as the ``YYYY-MM-DD`` string
        legacy stored (``rating_history.date`` is a TEXT column).
        """
        snapshot = (
            await self._session.execute(
                text(
                    "SELECT COALESCE(group_xp, 0), rating_position "
                    "FROM groups_donations WHERE group_id = :gid"
                ),
                {"gid": group_id},
            )
        ).first()
        if snapshot is None:
            return False
        total = int(snapshot[0] or 0)
        position = snapshot[1]
        await self._session.execute(
            text(
                "INSERT INTO rating_history (group_id, date, total_donations, position) "
                "VALUES (:gid, :date, :total, :pos) "
                "ON CONFLICT(group_id, date) DO UPDATE SET "
                "total_donations = :total, position = :pos"
            ),
            {
                "gid": group_id,
                "date": today.strftime("%Y-%m-%d"),
                "total": total,
                "pos": position,
            },
        )
        return True

    async def group_xp(self, group_id: int) -> int:
        """The group's current leaderboard score, ``0`` when it has none.

        The score is ``groups_donations.group_xp`` — the column
        :meth:`record_donation` bumps and :meth:`recalc_positions` ranks
        on — NOT ``total_donations``, which has been frozen since before
        the cutover (bot.py:10915). A group with no aggregate row has
        never received anything, so ``0`` is its true score rather than
        a missing value: the ``/donate`` receipt (#2007) reads this back
        right after a donation and would otherwise have to render "—"
        for the very first donation ever made to a group.
        """
        value = (
            await self._session.execute(
                text("SELECT COALESCE(group_xp, 0) FROM groups_donations WHERE group_id = :gid"),
                {"gid": group_id},
            )
        ).scalar_one_or_none()
        return int(value or 0)
