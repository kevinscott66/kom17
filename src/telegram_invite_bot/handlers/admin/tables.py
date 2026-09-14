"""``/admin_tables`` — per-DB table list + row counts.

Complements /admin_botstats (which surfaces just users + groups
counts) and /admin_db_sizes (file/WAL bytes, no per-table
granularity). This card answers the broader question: **what tables
live in each engine, and how many rows does each one carry?**

Why an operator wants this:

* Schema audit after a migration. ``alembic upgrade head`` is
  supposed to leave the schema in a known state; this card is the
  one-shot check that no table was unexpectedly dropped, renamed,
  or left behind. Diff the row against the previous deploy's
  snapshot to catch silent loss.
* Capacity planning. Knowing that ``transactions`` is the largest
  table by row count tells the operator where to focus index work
  or partition discussion.
* Spotting a write-loop. If ``message_stats`` doubles between two
  invocations 5 minutes apart, something is hot-writing without
  rate limits. The row-count delta is the cheapest detection.

We pull tables from ``sqlite_master`` filtered to ``type='table'``
and exclude SQLite's internal bookkeeping tables (the
``sqlite_%`` prefix covers ``sqlite_master``, ``sqlite_sequence``,
``sqlite_stat1``, etc. — operator wants the application schema, not
SQLite's metadata). Per-table ``COUNT(*)`` is a full leaf-page scan
on the rowid index — sub-second for any table in this codebase
even at multi-million-row scale, but we cap the catalog at 30
tables per engine to keep the card sendable: 5 engines × 30 tables
× ~50 chars/row stays comfortably under Telegram's 4096-char limit
while still rendering every realistic schema.

Per-engine failures are isolated (#1645): an engine that cannot
be opened at all is reported as its exception class on its own
row instead of raising out of the per-DB loop. The operator
opens this card when something already looks wrong, and that is
the worst moment to answer with nothing.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not leak dev IDs), private-only at the router
level (table names are operator-only schema context).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import text

from telegram_invite_bot.db.names import ALL_DBS, DBName

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.tables")


# Per-engine cap on rendered tables. Five engines × 30 tables ×
# ~50 chars/row ≈ 7.5 KiB raw — comfortably under Telegram's 4096
# limit after the HTML envelope (each engine adds a header and the
# overall card adds a title + spacer), and well above any realistic
# application schema. A schema with >30 tables in one DB is itself
# a smell worth investigating; the truncation indicator surfaces it.
_MAX_TABLES_PER_DB = 30


class _TableRow:
    """One table's name + count.

    Plain attribute container — same rationale as other admin
    cards: a single struct with one read site doesn't earn the
    dataclass ceremony."""

    __slots__ = ("name", "row_count")

    def __init__(self, *, name: str, row_count: int) -> None:
        self.name = name
        self.row_count = row_count


class _TableSnapshot:
    """One DB's table inventory.

    ``error`` carries the exception class name when the engine
    could not be read at all (#1645) and ``None`` on a read that
    ran; ``rows`` means nothing in the former case.

    The table count on the header is ``len(rows)``. The read
    never truncates — the per-engine cap is applied by
    :func:`_render` — so a separate total field would only be a
    second spelling of the same number, free to drift from it.
    """

    __slots__ = ("db", "error", "rows")

    def __init__(self, *, db: DBName, rows: list[_TableRow], error: str | None = None) -> None:
        self.db = db
        self.rows = rows
        self.error = error


async def _read_tables(registry: EngineRegistry, db: DBName) -> _TableSnapshot:
    """List tables in a DB plus their row counts.

    Sorted by row_count descending so the largest table — usually
    the one the operator cares about — leads. Secondary sort by
    name for stability across reads with equal counts (otherwise
    diffs between snapshots flap on counter ties).

    Never raises: an engine that cannot be opened or read at all
    is reported as a snapshot carrying its exception class
    (#1645), so the caller's loop over :data:`ALL_DBS` survives a
    single broken engine. Same posture and the same row shape as
    :func:`admin.integrity._read`.
    """
    try:
        engine = registry.engine(db)
        rows: list[_TableRow] = []
        async with engine.connect() as conn:
            # ``ORDER BY name`` at the catalog level is just to keep the
            # COUNT loop deterministic — the final card sorts by count.
            table_names = (
                (
                    await conn.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                            "ORDER BY name"
                        )
                    )
                )
                .scalars()
                .all()
            )
            for name in table_names:
                # Identifier from sqlite_master is trusted (we filtered
                # by type='table'), but we still wrap in double quotes
                # to survive any reserved-keyword table name an
                # operator might add in a future migration.
                quoted = '"' + str(name).replace('"', '""') + '"'
                # noqa rationale: ``quoted`` is derived from sqlite_master
                # where we filtered ``type='table'``, then double-quoted
                # with quote-doubling for any embedded quote in the
                # identifier. There is no user-supplied input on this
                # path. The f-string is unavoidable because SQLAlchemy
                # bound parameters bind values, not identifiers.
                cnt = (
                    await conn.execute(text(f"SELECT COUNT(*) FROM {quoted}"))  # noqa: S608
                ).scalar_one()
                rows.append(_TableRow(name=str(name), row_count=int(cnt)))
    except Exception as exc:  # noqa: BLE001 - classify any engine-side failure
        log.bind(db=db.value, error=type(exc).__name__).warning(
            "table catalog read failed; engine reported as unreadable"
        )
        return _TableSnapshot(db=db, rows=[], error=type(exc).__name__)
    rows.sort(key=lambda r: (-r.row_count, r.name))
    return _TableSnapshot(db=db, rows=rows)


def _fmt_count(n: int) -> str:
    """Comma-grouped integer. Operator scanning row counts cares
    about order of magnitude — 1,234,567 reads instantly,
    1234567 doesn't."""
    return f"{n:,}"


def _render(snaps: list[_TableSnapshot]) -> str:
    lines = ["📋 <b>Tables per engine</b>", ""]
    for snap in snaps:
        db, rows = snap.db, snap.rows
        if snap.error is not None:
            # The read never ran, so there is no catalog to draw —
            # the exception class IS the finding (#1645). Same row
            # shape as /admin_dbprobe and /admin_integrity.
            lines.append(f"<b>{db.value}</b> ⚠ <code>{snap.error}</code>")
            lines.append("")
            continue
        total = len(rows)
        lines.append(f"<b>{db.value}</b> ({total} table(s))")
        if not rows:
            # Empty schema is itself a finding — usually means the
            # alembic baseline didn't run for this engine. Surface
            # it explicitly rather than rendering an empty section.
            lines.append("  <i>no application tables</i>")
            lines.append("")
            continue
        for row in rows[:_MAX_TABLES_PER_DB]:
            lines.append(f"  • <code>{row.name}</code>: <code>{_fmt_count(row.row_count)}</code>")
        if total > _MAX_TABLES_PER_DB:
            remaining = total - _MAX_TABLES_PER_DB
            lines.append(f"  <i>… and {remaining} more</i>")
        lines.append("")
    lines.append(
        "<i>Sorted by row count desc. Counts are COUNT(*) at read "
        "time — accurate snapshot, not a cached estimate.</i>"
    )
    return "\n".join(lines)


async def handle_admin_tables(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_tables; silently dropped"
        )
        return
    snaps = [await _read_tables(registry, db) for db in ALL_DBS]
    await message.answer(_render(snaps))
    log.bind(user_id=user.id).info("/admin_tables rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.tables")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_tables(message, settings, registry)

    router.message.register(_entry, Command("admin_tables", ignore_case=True))
    return router
