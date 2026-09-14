"""``/admin_indexes`` — per-engine index catalog grouped by table.

Complements /admin_tables (tables + row counts) and /admin_pragmas
(per-engine PRAGMA drift). This card answers: **which indexes exist
on each table in each engine, and where is index coverage missing?**

Why an operator wants this:

* Performance triage. /admin_tables surfaces the largest table by
  row count; if that table also shows up here with **zero**
  application indexes, a missing-index diagnosis is one card away
  instead of an `EXPLAIN QUERY PLAN` shell session.
* Schema audit. After a migration, the same paranoid question that
  drives /admin_tables ("is the schema still what we deployed?")
  applies to indexes — DROP INDEX is a quiet operation that
  /admin_tables won't catch, but this card will.
* Spot stale auto-indexes. SQLite auto-creates an index when a
  UNIQUE or PRIMARY KEY constraint isn't backed by an explicit
  one (prefix ``sqlite_autoindex_``). A surge in those after a
  migration usually means someone dropped explicit indexes and
  let SQLite invent replacements with different semantics — we
  surface them in a dedicated "implicit" tail per-table so the
  operator notices.

We pull rows from ``sqlite_master WHERE type='index'`` and group by
``tbl_name``. SQLite's per-table auto-indexes (``sqlite_autoindex_*``)
are surfaced as a separate count rather than listed by name — they
carry no operator-meaningful identifier, and printing every one
would push the card past the 4096-char limit on schemas with many
UNIQUE constraints. Application-named indexes render in full.

Per-engine failures are isolated (#1645): an engine that cannot
be opened at all is reported as its exception class on its own
row instead of raising out of the per-DB loop. Missing-index
triage starts here precisely when something is already slow or
broken, so answering with nothing is the worst outcome.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not leak dev IDs), private-only at the router level
(index names can encode column-level schema details the operator
would not want to render in a shared admin group).
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


log = logger.bind(component="handlers.admin.indexes")


# Per-engine cap on rendered tables-with-indexes. Five engines × 20
# tables × ~80 chars/row stays comfortably below Telegram's 4096
# limit. A DB exceeding 20 tables-with-indexes triggers a truncation
# tail — the operator should pivot to a schema dump rather than
# scroll this card.
_MAX_TABLES_PER_DB = 20

# Per-table cap on explicit index names rendered. Two-tier truncation
# (per-table, then per-engine) keeps a single index-heavy table from
# eating the whole budget for the engine it lives in.
_MAX_INDEXES_PER_TABLE = 6


class _IndexRow:
    """One application-defined index.

    ``name`` is the index name from ``sqlite_master``; ``tbl_name``
    is its owning table. We keep both so the renderer can group by
    table without re-querying. Auto-indexes (``sqlite_autoindex_*``)
    are tallied separately, not rendered as :class:`_IndexRow`."""

    __slots__ = ("name", "tbl_name")

    def __init__(self, *, name: str, tbl_name: str) -> None:
        self.name = name
        self.tbl_name = tbl_name


class _TableIndexes:
    """One table's index inventory.

    ``explicit`` is the list of operator-named indexes in
    sqlite_master order. ``implicit_count`` is the number of
    SQLite-generated ``sqlite_autoindex_*`` rows on this table — a
    bare count is enough because the names are opaque.
    """

    __slots__ = ("explicit", "implicit_count", "tbl_name")

    def __init__(self, *, tbl_name: str, explicit: list[_IndexRow], implicit_count: int) -> None:
        self.tbl_name = tbl_name
        self.explicit = explicit
        self.implicit_count = implicit_count

    @property
    def total(self) -> int:
        return len(self.explicit) + self.implicit_count


class _IndexSnapshot:
    """One DB's index catalog, grouped by table.

    ``error`` carries the exception class name when the engine
    could not be read at all (#1645) and ``None`` on a read that
    ran; ``tables`` means nothing in the former case. An empty
    ``tables`` on a successful read is itself a finding — see
    the zero-index branch in :func:`_render` — which is exactly
    why an unreadable engine must not spell itself the same way.
    """

    __slots__ = ("db", "error", "tables")

    def __init__(
        self, *, db: DBName, tables: list[_TableIndexes], error: str | None = None
    ) -> None:
        self.db = db
        self.tables = tables
        self.error = error


async def _read_indexes(registry: EngineRegistry, db: DBName) -> _IndexSnapshot:
    """Group every index in a DB by its owning table.

    Tables with zero indexes do not appear here — listing them
    would duplicate /admin_tables without adding information. The
    "missing-index on a hot table" signal is recovered by
    cross-referencing this card with /admin_tables's row-count
    sort, which is exactly the diagnostic workflow we're after.

    Sorted by ``-total`` desc (then name) so the most-indexed
    tables — usually the operationally important ones — lead.

    Never raises: an engine that cannot be opened or read at all
    is reported as a snapshot carrying its exception class
    (#1645), so the caller's loop over :data:`ALL_DBS` survives a
    single broken engine. Same posture and the same row shape as
    :func:`admin.integrity._read`.
    """
    try:
        engine = registry.engine(db)
        by_table: dict[str, _TableIndexes] = {}
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT name, tbl_name FROM sqlite_master "
                        "WHERE type='index' "
                        "ORDER BY tbl_name, name"
                    )
                )
            ).all()
        for name, tbl_name in rows:
            bucket = by_table.setdefault(
                str(tbl_name),
                _TableIndexes(tbl_name=str(tbl_name), explicit=[], implicit_count=0),
            )
            sname = str(name)
            if sname.startswith("sqlite_autoindex_"):
                bucket.implicit_count += 1
            else:
                bucket.explicit.append(_IndexRow(name=sname, tbl_name=str(tbl_name)))
    except Exception as exc:  # noqa: BLE001 - classify any engine-side failure
        log.bind(db=db.value, error=type(exc).__name__).warning(
            "index catalog read failed; engine reported as unreadable"
        )
        return _IndexSnapshot(db=db, tables=[], error=type(exc).__name__)
    out = list(by_table.values())
    out.sort(key=lambda t: (-t.total, t.tbl_name))
    return _IndexSnapshot(db=db, tables=out)


def _render(snaps: list[_IndexSnapshot]) -> str:
    lines = ["🗂 <b>Indexes per engine</b>", ""]
    for snap in snaps:
        db, tables = snap.db, snap.tables
        if snap.error is not None:
            # The read never ran, so there is no catalog to draw —
            # the exception class IS the finding (#1645). Drawn
            # before the zero-index branch on purpose: the two
            # must never be confused for one another.
            lines.append(f"<b>{db.value}</b> ⚠ <code>{snap.error}</code>")
            lines.append("")
            continue
        total_tables = len(tables)
        lines.append(f"<b>{db.value}</b> ({total_tables} table(s) with indexes)")
        if not tables:
            # An engine with zero indexes anywhere is a strong
            # smell — even a brand-new schema usually has PRIMARY
            # KEY auto-indexes. Surface explicitly rather than as
            # an empty section.
            lines.append("  <i>no indexes at all</i>")
            lines.append("")
            continue
        for tbl in tables[:_MAX_TABLES_PER_DB]:
            lines.append(
                f"  <b>{tbl.tbl_name}</b> "
                f"(<code>{tbl.total}</code>: "
                f"<code>{len(tbl.explicit)}</code> explicit, "
                f"<code>{tbl.implicit_count}</code> implicit)"
            )
            for idx in tbl.explicit[:_MAX_INDEXES_PER_TABLE]:
                lines.append(f"    • <code>{idx.name}</code>")
            extra = len(tbl.explicit) - _MAX_INDEXES_PER_TABLE
            if extra > 0:
                lines.append(f"    <i>… and {extra} more explicit</i>")
        if total_tables > _MAX_TABLES_PER_DB:
            remaining = total_tables - _MAX_TABLES_PER_DB
            lines.append(f"  <i>… and {remaining} more table(s)</i>")
        lines.append("")
    lines.append(
        "<i>Implicit = <code>sqlite_autoindex_*</code> (PK/UNIQUE backing). "
        "Missing-index triage: cross-reference with /admin_tables row "
        "counts to find heavy tables with no explicit coverage.</i>"
    )
    return "\n".join(lines)


async def handle_admin_indexes(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_indexes; silently dropped"
        )
        return
    snaps = [await _read_indexes(registry, db) for db in ALL_DBS]
    await message.answer(_render(snaps))
    log.bind(user_id=user.id).info("/admin_indexes rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.indexes")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_indexes(message, settings, registry)

    router.message.register(_entry, Command("admin_indexes", ignore_case=True))
    return router
