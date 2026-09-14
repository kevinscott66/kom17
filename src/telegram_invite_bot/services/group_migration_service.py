"""Carry a group's data across the id change of a supergroup upgrade.

When a Telegram basic group is upgraded to a supergroup its ``chat_id``
changes — ``-1234500011`` becomes ``-1002222222222`` — and every update
from that moment on carries the new one. Nothing about the group changes
for the people in it: same title, same members, same history. Everything
about it changes for us, because every table in this codebase keys group
state by that number.

Left unhandled, the upgrade reads as "the old group vanished and an
identical empty one appeared": settings, warnings, word filters, the
welcome text, staff roles, donation totals and the rating score are all
still on disk under an id that will never appear in an update again.
Worse than merely lost — the stale rows stay *live* in every list built
from a table scan, so ``/shop`` offers "buy for this group" against a
chat the bot can no longer post to, and the 15% group cut of that
purchase is credited to the owner of a dead id.

No actor check, on purpose. The remap rewrites group-keyed rows
wholesale and takes no ``user_id`` at all, because neither caller can be
driven by an untrusted actor: ``handlers/group_migration.py`` reads both
ids off ``migrate_to_chat_id`` / ``migrate_from_chat_id``, which are
Telegram service-message fields a client cannot set, and
``handlers/admin/group_migrate.py`` gates on ``settings.bot.is_developer``
before it parses anything. Adding a permission check here would have
nothing to check it against — the automatic path has no human actor.

The remap is deliberately schema-agnostic. It asks SQLite which columns
name a group rather than carrying a hand-written list, because the
hand-written list is the part that rots: the failure mode of this whole
module is a table nobody remembered, and a list that must be edited
whenever a migration adds a group-keyed table is a list that will one
day be out of date without anyone noticing. Discovery costs one cheap
query per database and cannot fall behind the schema.

Collision policy. Both ids can legitimately hold rows already: the bot
may have seen the supergroup (and written to it) before the migration
event was processed, and the same user can hold a row under each.
``UPDATE OR IGNORE`` moves everything that fits and leaves the losers
behind; the ``DELETE`` that follows drops exactly those leftovers.

For *configuration* — the rules text, the welcome message, the
moderation settings — keeping the newer row is the safe direction: it
is the one that matches what the group looks like now. Twenty of the
twenty-six collision-capable columns on prod are that shape.

For an *accumulator* it is not safe at all, and dropping the loser
destroys value that was really earned: a group's ``group_xp`` (the
column the whole donation rating is built on), a member's
``total_donated``, a day's message ``count``. For a column holding
*paid time* — ``vip_till``, ``expires_at`` — it can silently shorten
a privilege someone bought. Those tables are named in
:data:`_MERGE_RULES` and their losing row is folded into the winner
before the delete removes it: summed for a running total, kept at the
maximum for an expiry, kept at the *minimum* for a first sighting.
Anything not named there keeps new-wins, which
is why the map is an allowlist rather than a heuristic — the failure
mode of a guess here is silent arithmetic on someone's balance.

Deliberately left on new-wins, for the record: ``rating_history`` is a
per-date snapshot carrying a ``position``, not a running total, so
summing two groups' history for one date would be meaningless;
``groups_donations.members_count`` is likewise a snapshot; and
``groups_donations.total_donations`` is frozen at zero by design
(``repositories/donations_rating_repo.py`` and legacy ``bot.py:10915``
«Сейчас не пополняется») — the rating reads ``group_xp`` instead.

The five domain databases are the whole scope. Prod carries a sixth
file, ``fsm.db``, which ``ALL_DBS`` excludes: its only table keys on
aiogram's composite *string* keys, which embed the chat id in a form
the column-discovery pass cannot address. A user mid-flow when the
upgrade lands loses that flow. It is transient state and re-entering
the command rebuilds it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from loguru import logger
from sqlalchemy import text

from telegram_invite_bot.db.names import ALL_DBS
from telegram_invite_bot.db.session import session_for

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db.engines import EngineRegistry

log = logger.bind(component="services.group_migration")


# Every column that names a group: the bare forms plus the qualified
# ones (``transcription_log_chat_id``, ``current_group_id``). Anchored
# on ``_group_id`` / ``_chat_id`` so a column merely *containing* the
# substring can't sneak in.
#
# Over-matching is harmless here and under-matching is not, which is why
# the pattern leans wide: every statement below is additionally filtered
# by ``= :old_id``, and ``old_id`` is one specific negative number that
# only ever refers to the group being migrated. A column that happens to
# hold that value holds a reference to that group, whatever it is named.
_DISCOVER_COLUMNS = text(
    r"""
    SELECT m.name AS tbl, p.name AS col
    FROM sqlite_master m
    JOIN pragma_table_info(m.name) p
    WHERE m.type = 'table'
      AND m.name NOT LIKE 'sqlite\_%' ESCAPE '\'
      AND (
        p.name = 'group_id'
        OR p.name = 'chat_id'
        OR p.name LIKE '%\_group\_id' ESCAPE '\'
        OR p.name LIKE '%\_chat\_id' ESCAPE '\'
      )
    ORDER BY m.name, p.name
    """
)

# Table and column names come back from ``sqlite_master`` and go into
# the statements below by interpolation — binding cannot carry an
# identifier. They are the database's own names, not anything a user
# supplied, but the check is cheap and it is the one thing standing
# between a future schema and a quoting bug.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class _MergeRule:
    """How to fold a losing row into the row that already beat it.

    ``group_col`` pins which discovered column the rule answers to. A
    table can name a group twice — ``group_settings`` has both
    ``group_id`` and ``transcription_log_chat_id`` — and the fold has
    to run once, on the column the unique constraint is built from.

    ``keys`` is the rest of that constraint: the columns which, held
    equal, make two rows the same row under two different ids. Empty
    when the group column is the whole key, as in ``groups_donations``.
    """

    group_col: str
    keys: tuple[str, ...] = ()
    sums: tuple[str, ...] = ()
    """Running totals. The loser's value is added to the winner's."""
    maxes: tuple[str, ...] = ()
    """Expiries. The winner keeps whichever end date is later."""
    mins: tuple[str, ...] = ()
    """First sightings. The winner keeps whichever date is earlier.

    The mirror of :attr:`maxes`, and the only direction here that moves
    a value *backwards*. It exists for ``user_group_joins.joined_at``,
    which is the date the profile card renders as "in the group since"
    — a column whose whole job is to hold the earliest time we saw
    someone, so the newer of two rows is exactly the wrong one to keep
    (#2019).
    """

    def columns(self) -> tuple[str, ...]:
        """Every column the fold statement will name."""
        return (self.group_col, *self.keys, *self.sums, *self.maxes, *self.mins)


# Tables where "the new id wins" would destroy value rather than pick a
# side. Keyed by table name, deliberately hand-written: unlike column
# discovery — where a stale list is the whole failure mode — a wrong
# guess here does arithmetic on someone's balance, so the cost of the
# two failure directions is reversed. A table absent from this map
# keeps new-wins, which is correct for configuration.
#
# The keys mirror the PK/UNIQUE constraints as they stand on prod.
# ``_fold_losing_rows`` re-checks every named column against the real
# table before it writes, so a schema drift degrades to new-wins with a
# warning instead of raising or silently mis-summing.
_MERGE_RULES: dict[str, _MergeRule] = {
    # PK(group_id). The donation rating ranks on group_xp.
    "groups_donations": _MergeRule(group_col="group_id", sums=("group_xp",)),
    # PK(group_id, user_id).
    "group_top_donators": _MergeRule(
        group_col="group_id", keys=("user_id",), sums=("total_donated",)
    ),
    # UNIQUE(user_id, chat_id, date) — in both activity and message_stats.
    "message_counts": _MergeRule(group_col="chat_id", keys=("user_id", "date"), sums=("count",)),
    # PK(user_id, group_id) — paid time.
    "user_group_vip": _MergeRule(group_col="group_id", keys=("user_id",), maxes=("vip_till",)),
    # PK(user_id, privilege_type, group_id) — paid time.
    "user_privileges": _MergeRule(
        group_col="group_id", keys=("user_id", "privilege_type"), maxes=("expires_at",)
    ),
    # PK(user_id, chat_id) — group tenure. Both directions on one row:
    # ``joined_at`` is the first sighting and must not move forward,
    # ``last_seen`` is the last and must not move back. The rest of the
    # table (``left_at``, ``is_active``, ``source``, ``group_title``)
    # describes the membership as it stands now, so new-wins is right
    # for it and it is deliberately absent here.
    "user_group_joins": _MergeRule(
        group_col="chat_id", keys=("user_id",), maxes=("last_seen",), mins=("joined_at",)
    ),
}


async def _fold_losing_rows(
    session: AsyncSession, tbl: str, rule: _MergeRule, *, old_id: int, new_id: int
) -> int:
    """Move the losing row's totals onto the winner before it is dropped.

    Runs BEFORE the ``UPDATE OR IGNORE``/``DELETE`` pair, so by the time
    the delete removes the loser its value is already on the survivor.
    A row with no counterpart under the new id is untouched here and
    migrates normally, which is why this cannot double-count.

    Returns the number of surviving rows that absorbed one.
    """
    present = {
        name
        for (name,) in (
            await session.execute(text(f'SELECT name FROM pragma_table_info("{tbl}")'))  # noqa: S608
        ).all()
    }
    if not set(rule.columns()) <= present:
        # One table name, two schemas: ``message_counts`` lives in both
        # activity and message_stats. Refuse rather than guess, and say
        # so — new-wins is the pre-existing behaviour, not a silent one.
        log.warning("merge rule does not fit {t}; leaving it on new-wins", t=tbl)
        return 0

    match = "".join(f' AND loser."{k}" = winner."{k}"' for k in rule.keys)
    loser = f'FROM "{tbl}" loser WHERE loser."{rule.group_col}" = :old{match}'
    assignments = [
        f'"{c}" = COALESCE(winner."{c}", 0) + COALESCE((SELECT loser."{c}" {loser}), 0)'
        for c in rule.sums
    ]

    # The two-COALESCE shape, rather than the ``COALESCE(x, 0)`` the
    # sums use, because a nullable column must survive the fold as a
    # column and not as an identity element: ``user_group_joins.last_seen``
    # is a TIMESTAMP, and folding two NULLs to the integer ``0`` writes a
    # value the ORM cannot load back as a datetime. Each side falls back
    # to the *other* side, so both-present picks a winner, one-present
    # keeps the one there is, and both-absent stays NULL. The direction
    # matters twice over for ``mins``: SQLite orders INTEGER before TEXT,
    # so ``MIN('2021-03-04...', 0)`` is ``0`` — a zero identity here
    # would not merely mistype the column, it would win every time.
    def _pair(c: str, fn: str) -> str:
        pick = f'(SELECT loser."{c}" {loser})'
        return f'"{c}" = {fn}(COALESCE(winner."{c}", {pick}), COALESCE({pick}, winner."{c}"))'

    assignments += [_pair(c, "MAX") for c in rule.maxes]
    assignments += [_pair(c, "MIN") for c in rule.mins]
    folded = await session.execute(
        text(
            f'UPDATE "{tbl}" AS winner SET {", ".join(assignments)} '  # noqa: S608
            f'WHERE winner."{rule.group_col}" = :new AND EXISTS (SELECT 1 {loser})'
        ),
        {"old": old_id, "new": new_id},
    )
    return cast("CursorResult[Any]", folded).rowcount or 0


@dataclass(frozen=True, slots=True)
class GroupMigrationResult:
    """What the remap actually did, for the log line and the admin card."""

    moved: int = 0
    """Column values rewritten to the new id.

    Counted per (row, column), not per row: a table naming the group
    twice — ``group_settings`` has both ``group_id`` and
    ``transcription_log_chat_id`` — contributes twice for one row.
    """

    dropped: int = 0
    """Rows discarded because the new id already had their equivalent.

    Their accumulating columns are not discarded with them — see
    :data:`_MERGE_RULES` and ``merged`` below.
    """

    merged: int = 0
    """Surviving rows that absorbed a colliding row's totals.

    Counted per row, not per column: one collision on a table with two
    additive columns folds both in a single statement and counts once.
    """

    columns: int = 0
    """Group-naming columns visited across the five domain databases."""

    failed: tuple[str, ...] = field(default_factory=tuple)
    """Databases that raised. A retry finishes them — the remap is idempotent."""

    skipped: tuple[str, ...] = field(default_factory=tuple)
    """``db.table.column`` triples discovery found but could not quote.

    Rows under those columns keep the dead id — the exact failure this
    module exists to prevent — so they are reported rather than warned
    about once and forgotten.
    """


async def migrate_group_id(
    registry: EngineRegistry, *, old_id: int, new_id: int
) -> GroupMigrationResult:
    """Rewrite ``old_id`` to ``new_id`` across the five domain databases.

    Idempotent by construction: the second run matches no rows, because
    the first left none under the old id. That matters more than it
    looks — Telegram announces the upgrade twice (once in each chat) and
    both announcements land here.

    Failures are per-database and non-fatal. The five databases are five
    files with five independent transactions, so there is no way to make
    this atomic across them; aborting the rest on the first failure
    would only trade a partial remap for a smaller one. Each database
    commits or rolls back on its own, the failures are named in the
    result, and a rerun picks up whatever was missed.
    """
    if old_id >= 0 or new_id >= 0:
        # Group and supergroup ids are negative without exception. A
        # positive value here means a caller mixed up a user id with a
        # chat id, and the wide column match above would then rewrite
        # every column that stores that user id.
        raise ValueError(f"group ids must be negative: {old_id} -> {new_id}")
    if old_id == new_id:
        raise ValueError(f"nothing to migrate: {old_id} == {new_id}")

    moved = dropped = columns = merged = 0
    failed: list[str] = []
    skipped: list[str] = []

    for db in ALL_DBS:
        bound = log.bind(db=db.value, old_id=old_id, new_id=new_id)
        # Per-database tallies. They are folded into the totals below
        # only once the session has closed cleanly: ``session_for``
        # rolls back on the way out of a failure, so a count taken
        # before an exception describes writes that no longer exist.
        db_moved = db_dropped = db_columns = db_merged = 0
        db_skipped: list[str] = []
        try:
            async with session_for(registry, db) as session:
                for tbl, col in (await session.execute(_DISCOVER_COLUMNS)).all():
                    if not (_IDENTIFIER_RE.match(tbl) and _IDENTIFIER_RE.match(col)):
                        bound.warning("skipping unquotable identifier {t}.{c}", t=tbl, c=col)
                        db_skipped.append(f"{db.value}.{tbl}.{col}")
                        continue
                    db_columns += 1
                    rule = _MERGE_RULES.get(tbl)
                    if rule is not None and rule.group_col == col:
                        # Before the pair below, so the loser's totals
                        # are on the winner by the time it is dropped.
                        db_merged += await _fold_losing_rows(
                            session, tbl, rule, old_id=old_id, new_id=new_id
                        )
                    update = await session.execute(
                        text(
                            f'UPDATE OR IGNORE "{tbl}" SET "{col}" = :new '  # noqa: S608
                            f'WHERE "{col}" = :old'
                        ),
                        {"new": new_id, "old": old_id},
                    )
                    # Only rows the UPDATE refused to move are still
                    # here, and they are refused for exactly one reason:
                    # the new id already holds an equivalent row.
                    delete = await session.execute(
                        text(f'DELETE FROM "{tbl}" WHERE "{col}" = :old'),  # noqa: S608
                        {"old": old_id},
                    )
                    # ``execute(text(...))`` is typed ``Result``; a DML
                    # statement always yields a ``CursorResult``, which
                    # is where ``rowcount`` lives. Same cast the
                    # withdrawal repository uses for the same reason.
                    db_moved += cast("CursorResult[Any]", update).rowcount or 0
                    db_dropped += cast("CursorResult[Any]", delete).rowcount or 0
        except Exception as exc:  # noqa: BLE001 — one bad DB must not sink the rest
            failed.append(db.value)
            bound.opt(exception=exc).error("group migration failed for this database")
            continue

        moved += db_moved
        dropped += db_dropped
        columns += db_columns
        merged += db_merged
        skipped.extend(db_skipped)

    result = GroupMigrationResult(
        moved=moved,
        dropped=dropped,
        merged=merged,
        columns=columns,
        failed=tuple(failed),
        skipped=tuple(skipped),
    )
    log.bind(
        old_id=old_id,
        new_id=new_id,
        moved=result.moved,
        dropped=result.dropped,
        merged=result.merged,
        columns=result.columns,
        failed=result.failed,
        skipped=result.skipped,
    ).info("group id migrated")
    return result
