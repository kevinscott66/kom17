"""``/admin_db_sizes`` — disk + logical size per engine.

Operator question: "is any of the SQLite files growing unexpectedly?".
Three answers in one card:

1. **File size on disk** of ``<db>.db``. Steady growth is normal —
   inserts add pages. A *sudden* jump (or, conversely, a flatline
   during heavy activity) is the diagnostic signal.
2. **Logical size** = ``page_count * page_size``. Equals the file
   size in steady state. A meaningful gap (logical < file)
   suggests deleted pages haven't been ``VACUUM``-ed back; not an
   error, but useful context if disk pressure is climbing.
3. **WAL-file size** (``<db>.db-wal``). Steady state is "small or
   absent" because checkpoints fold WAL back into the main file
   on a schedule. A WAL file that's larger than the main DB is the
   classic "checkpoint is stuck" symptom — typically a long-running
   read transaction holding the WAL open. Surfacing the bytes here
   means the operator can spot the symptom in one place rather than
   shelling into the host to ``ls -la database/``.

Why not just ``ls``: the legacy command surface is Telegram-only by
design (operators run the bot in Telegram, not via SSH). Surfacing the
read-only filesystem view inside the admin tree means an operator who
has DM access but no SSH can still answer the "is anything growing?"
question without escalating.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(no enumeration via existence), private-only at the router level —
file sizes aren't PII, but the existence of per-DB files leaks the
deployment's storage shape and is operator-only context anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import text

from telegram_invite_bot.db.engines import resolve_db_path
from telegram_invite_bot.db.names import ALL_DBS, DBName

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.db_sizes")


class _SizeSnapshot:
    __slots__ = ("db", "file_bytes", "logical_bytes", "wal_bytes")

    def __init__(
        self,
        *,
        db: DBName,
        file_bytes: int | None,
        logical_bytes: int,
        wal_bytes: int | None,
    ) -> None:
        self.db = db
        # ``None`` when the file is missing on disk (fresh test fixtures
        # use in-memory engines that never write a file; we still want
        # a row in the card rather than silently dropping it).
        self.file_bytes = file_bytes
        self.logical_bytes = logical_bytes
        self.wal_bytes = wal_bytes


def _stat_or_none(path: Path) -> int | None:
    """Return file size or ``None`` if the path doesn't exist.

    ``Path.stat()`` raises ``FileNotFoundError`` on missing files. We
    swallow it because a missing file is itself the signal: tests use
    in-memory engines (no .db on disk) and the card must still render
    instead of 500-ing the dispatcher.
    """
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


async def _read(registry: EngineRegistry, settings: Settings, db: DBName) -> _SizeSnapshot:
    engine = registry.engine(db)
    async with engine.connect() as conn:
        page_count = int((await conn.execute(text("PRAGMA page_count"))).scalar_one())
        page_size = int((await conn.execute(text("PRAGMA page_size"))).scalar_one())
    logical_bytes = page_count * page_size

    db_path = resolve_db_path(settings.paths, db)
    file_bytes = _stat_or_none(db_path)
    # WAL file is co-located with the DB file, suffix "-wal". SQLite's
    # docs spell out this naming as the contract — relying on the
    # suffix is fine and avoids parsing the engine URL.
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_bytes = _stat_or_none(wal_path)
    return _SizeSnapshot(
        db=db,
        file_bytes=file_bytes,
        logical_bytes=logical_bytes,
        wal_bytes=wal_bytes,
    )


# Order of magnitude is what the operator needs; full byte precision
# is noise at the human-read scale of this card. KiB granularity keeps
# tiny test files visible (< 1 KiB rounds to "0 KiB") while letting
# real prod files render as 4-digit MiB without overflow.
def _fmt(n: int | None) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    kib = n / 1024
    if kib < 1024:
        return f"{kib:.1f} KiB"
    mib = kib / 1024
    if mib < 1024:
        return f"{mib:.1f} MiB"
    return f"{mib / 1024:.2f} GiB"


def _render(snaps: list[_SizeSnapshot]) -> str:
    lines = ["💾 <b>DB sizes</b>", ""]
    for s in snaps:
        lines.append(f"<b>{s.db.value}</b>")
        lines.append(f"  • file: <code>{_fmt(s.file_bytes)}</code>")
        lines.append(f"  • logical: <code>{_fmt(s.logical_bytes)}</code>")
        # WAL is only meaningful if it exists — we still show the row
        # when it doesn't, with em-dash, so the operator sees the file
        # is absent rather than wondering whether the card forgot it.
        lines.append(f"  • wal: <code>{_fmt(s.wal_bytes)}</code>")
        # Stuck-checkpoint hint: WAL > file is the textbook symptom.
        # Threshold on raw bytes (not formatted strings) so the check
        # is unaffected by formatting choices.
        if (
            s.wal_bytes is not None
            and s.file_bytes is not None
            and s.wal_bytes > s.file_bytes
            and s.wal_bytes > 1024 * 1024  # ignore noise on tiny files
        ):
            lines.append(
                "  ⚠ WAL larger than main file — checkpoint may be "
                "stuck (long-running read holding the WAL open)."
            )
        lines.append("")
    return "\n".join(lines).rstrip()


async def handle_admin_db_sizes(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_db_sizes; silently dropped"
        )
        return
    snaps = [await _read(registry, settings, db) for db in ALL_DBS]
    await message.answer(_render(snaps))
    log.bind(user_id=user.id).info("/admin_db_sizes rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.db_sizes")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_db_sizes(message, settings, registry)

    router.message.register(_entry, Command("admin_db_sizes", ignore_case=True))
    return router
