"""``/admin_integrity`` — corruption + FK-orphan check per engine.

Sibling to :mod:`admin.pragmas` (which verifies configuration) and
:mod:`admin.db_sizes` (which verifies disk footprint). This card
answers the harder question: **is the data on disk internally
consistent?**

SQLite gives us two cheap-ish checks:

* ``PRAGMA integrity_check`` — walks every page, verifies B-tree
  invariants, free-list, indexes vs underlying rows. Returns the
  single row ``ok`` on a clean database, otherwise one row per
  problem found (capped at 100 by SQLite). A non-``ok`` result is
  the canary for *physical* corruption — bit-rot on disk, a
  half-flushed WAL after a host crash, an out-of-band ``cp`` of a
  hot DB. Catch it here before it manifests as ``DatabaseError`` in
  a handler during a user-facing flow.
* ``PRAGMA foreign_key_check`` — scans every FK constraint and lists
  rows whose parent is missing. Returns *no rows* on a clean DB.
  Catches the *logical* corruption mode that ``foreign_keys=ON``
  was supposed to prevent — but FK enforcement is per-connection,
  so any historical write from a connection that forgot the pragma
  (legacy bot.py before the strangler, or an ad-hoc ``sqlite3 <file>``
  shell session) can leave orphans. ``foreign_key_check`` finds
  them after the fact; this card surfaces them.

We run **both** because they fail orthogonally: physical corruption
won't show up as an FK orphan and vice versa, and the operator
wants a single "is this engine healthy?" signal that covers both
classes.

Both pragmas are read-only — neither mutates the database, both
hold a SHARED lock for the duration. ``integrity_check`` is the
expensive one (full page scan), so the card runs serially per DB
rather than in parallel: bombarding five engines with full scans
at once during a real incident would compete with live traffic.

Cost: on a few-MiB DB it's milliseconds; on a multi-hundred-MiB DB
it's seconds. The operator invokes this on demand, not on a
schedule — there's no auto-poller — so the cost is acceptable.

Per-engine isolation: :func:`_read` captures any engine-side
exception instead of letting it escape (#1590). One unreadable
engine — a deleted file, a wedged lock, a header so damaged that
even ``integrity_check`` raises — must not blank the whole card,
because the operator opens this card precisely when something is
already wrong and the other four readouts are the diagnostic. Same
posture as ``/admin_dbprobe`` (:func:`admin.dbprobe._probe_one`):
the failed engine renders its exception class, the rest render
normally.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not leak dev IDs), private-only at the router
level (the readout names DB files and their integrity state, which
is operator-only context).
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


log = logger.bind(component="handlers.admin.integrity")


# Cap the number of issue rows we render per DB. SQLite's
# ``integrity_check`` itself caps at 100, but a 100-row dump from
# one engine would push the card past Telegram's 4096-char limit if
# multiple engines were dirty simultaneously. Three is enough for an
# operator to recognise the failure class ("malformed disk image",
# "row N missing from index Y") without rendering the whole forensic
# trace — for that, the operator runs ``sqlite3 <file> 'PRAGMA
# integrity_check'`` on the host.
_MAX_ISSUES_PER_DB = 3


class _IntegritySnapshot:
    """One DB's integrity readout.

    Plain attribute container — see :class:`admin.pragmas._PragmaSnapshot`
    for the rationale on skipping ``@dataclass`` here. Two failure
    classes are surfaced separately because they fail orthogonally
    and the operator's first question on a fail is "which class?".

    ``error`` carries the exception class name when the engine could
    not be read at all (#1590) and ``None`` on a check that ran. It
    defaults to ``None`` so the healthy construction — the common
    one — stays a three-argument call.
    """

    __slots__ = ("db", "error", "fk_orphans", "integrity_issues")

    def __init__(
        self,
        *,
        db: DBName,
        integrity_issues: list[str],
        fk_orphans: list[tuple[str, int, str, int]],
        error: str | None = None,
    ) -> None:
        self.db = db
        # Empty list ⇒ ``integrity_check`` returned just ``ok``.
        self.integrity_issues = integrity_issues
        # Empty list ⇒ no FK orphans. Each tuple is
        # ``(child_table, child_rowid, parent_table, fk_id)``.
        self.fk_orphans = fk_orphans
        # Non-``None`` ⇒ the engine could not be read at all, so
        # neither list above carries any meaning; see :func:`_read`.
        self.error = error

    @property
    def healthy(self) -> bool:
        return self.error is None and not self.integrity_issues and not self.fk_orphans


async def _read(registry: EngineRegistry, db: DBName) -> _IntegritySnapshot:
    """Run both pragmas on a fresh connection.

    Reads serially per-DB. ``integrity_check`` issues a full page
    walk holding a SHARED lock; running it concurrently across five
    engines during an incident would amplify the disk pressure that
    likely caused the incident in the first place.

    Never raises: an engine that cannot be opened or read at all is
    reported as a snapshot carrying its exception class (#1590), so
    the caller's list comprehension over :data:`ALL_DBS` survives a
    single broken engine. See the module docstring.
    """
    try:
        engine = registry.engine(db)
        async with engine.connect() as conn:
            ic_rows = (await conn.execute(text("PRAGMA integrity_check"))).scalars().all()
            # Filter out the no-op "ok" row so an empty list cleanly
            # encodes "clean DB". SQLite returns exactly one row ``ok``
            # when healthy and one row per problem otherwise.
            issues = [str(r) for r in ic_rows if str(r).strip().lower() != "ok"]
            fk_rows = (await conn.execute(text("PRAGMA foreign_key_check"))).all()
        orphans: list[tuple[str, int, str, int]] = []
        for row in fk_rows:
            # ``foreign_key_check`` row shape: (table, rowid, parent, fkid).
            # rowid may be NULL for a WITHOUT ROWID table; coerce defensively.
            table = str(row[0]) if row[0] is not None else "?"
            rowid = int(row[1]) if row[1] is not None else -1
            parent = str(row[2]) if row[2] is not None else "?"
            fkid = int(row[3]) if row[3] is not None else -1
            orphans.append((table, rowid, parent, fkid))
    except Exception as exc:  # noqa: BLE001 - classify any engine-side failure
        log.bind(db=db.value, error=type(exc).__name__).warning(
            "integrity probe failed; engine reported as unreadable"
        )
        return _IntegritySnapshot(
            db=db, integrity_issues=[], fk_orphans=[], error=type(exc).__name__
        )
    return _IntegritySnapshot(db=db, integrity_issues=issues, fk_orphans=orphans)


def _render(snaps: list[_IntegritySnapshot]) -> str:
    lines = ["🩺 <b>Engine integrity check</b>", ""]
    any_warn = False
    for s in snaps:
        if s.healthy:
            lines.append(f"<b>{s.db.value}</b> — <code>ok</code> ✅")
            continue
        any_warn = True
        if s.error is not None:
            # The check never ran, so there are no per-class bullets
            # to draw — the exception class IS the finding. Same row
            # shape as /admin_dbprobe (admin.dbprobe._render).
            lines.append(f"<b>{s.db.value}</b> ⚠ <code>{s.error}</code>")
            continue
        lines.append(f"<b>{s.db.value}</b> ⚠")
        if s.integrity_issues:
            lines.append(f"  • integrity_check: <code>{len(s.integrity_issues)}</code> issue(s)")
            for issue in s.integrity_issues[:_MAX_ISSUES_PER_DB]:
                lines.append(f"    – <code>{issue}</code>")
            if len(s.integrity_issues) > _MAX_ISSUES_PER_DB:
                remaining = len(s.integrity_issues) - _MAX_ISSUES_PER_DB
                lines.append(f"    – <i>… and {remaining} more</i>")
        if s.fk_orphans:
            lines.append(f"  • foreign_key_check: <code>{len(s.fk_orphans)}</code> orphan(s)")
            for table, rowid, parent, fkid in s.fk_orphans[:_MAX_ISSUES_PER_DB]:
                lines.append(
                    f"    – <code>{table}</code> rowid="
                    f"<code>{rowid}</code> → "
                    f"<code>{parent}</code> (fk #{fkid})"
                )
            if len(s.fk_orphans) > _MAX_ISSUES_PER_DB:
                remaining = len(s.fk_orphans) - _MAX_ISSUES_PER_DB
                lines.append(f"    – <i>… and {remaining} more</i>")
    lines.append("")
    if any_warn:
        lines.append(
            "<i>⚠ At least one engine is unhealthy. Physical corruption "
            "(integrity_check) needs a backup-restore; FK orphans can be "
            "patched in place once the offending parent row is identified; "
            "a bare error class instead of bullets means the check itself "
            "could not run — see /admin_dbprobe for liveness and "
            "/admin_disk for filesystem state.</i>"
        )
    else:
        lines.append("<i>All engines clean.</i>")
    return "\n".join(lines)


async def handle_admin_integrity(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_integrity; silently dropped"
        )
        return
    # Serial per-DB on purpose — see module docstring on disk pressure.
    snaps = [await _read(registry, db) for db in ALL_DBS]
    await message.answer(_render(snaps))
    log.bind(user_id=user.id).info("/admin_integrity rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.integrity")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_integrity(message, settings, registry)

    router.message.register(_entry, Command("admin_integrity", ignore_case=True))
    return router
