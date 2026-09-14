"""``/admin_pragmas`` — verify per-engine PRAGMA settings.

Every other admin card answers "what's in the data?". This one
answers "is the engine configured the way we think?". Specifically:

* ``journal_mode`` should be ``wal`` on every file — legacy and new
  code share each ``.db``, and journal-mode is a file-level setting
  baked at open-time. A single connection opening without the WAL
  pragma silently downgrades the file to rollback-journal mode for
  *every* subsequent connection, with no error — the symptom is
  writer-blocks-reader stalls under load that look like deadlocks.
  This card is the cheapest way to confirm WAL is sticky after a
  process restart.
* ``foreign_keys`` should be ``1`` everywhere — SQLite default is
  off, and a connection without the pragma will silently let
  orphaned rows through.
* ``synchronous`` should match :func:`pragma.synchronous_level` —
  ``FULL`` for users/economy (money + identity), ``NORMAL`` elsewhere.
  Mismatch ⇒ either the pragma listener didn't fire OR per-DB
  tuning has drifted from the documented intent at
  :mod:`telegram_invite_bot.db.pragma`.

The card flags any drift with a ``⚠`` glyph so an operator can
spot anomalies without having to remember what the expected values
are. Pragma values are read via a fresh ``connect()`` per DB so we
exercise the same code path a real handler would — checking a
cached value would defeat the point.

Per-engine failures are isolated (#1645): an engine that cannot
be opened at all is reported as its exception class on its own
row instead of raising out of the comprehension over
``ALL_DBS``. The operator opens this card when a connection is
already misbehaving, which is the worst moment to answer with
nothing — the other four readouts are still the diagnosis.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(no enumeration of dev IDs via existence), private-only at the
router level (the card lists DB file roles, which would be
operator-only context in a shared admin group).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import text

from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.db.pragma import synchronous_level

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.pragmas")


# SQLite encodes ``synchronous`` as an integer at the pragma layer:
# 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA. We compare against the string
# tuning in :mod:`pragma`, so map back to the same alphabet.
_SYNCHRONOUS_LABELS: dict[int, str] = {
    0: "OFF",
    1: "NORMAL",
    2: "FULL",
    3: "EXTRA",
}


class _PragmaSnapshot:
    """One DB's pragma readout. Plain attribute container — using a
    dataclass would force an ``__init__`` signature that's noisier
    than this struct deserves (six pragmas, all read in one place).

    ``error`` carries the exception class name when the engine
    could not be read at all (#1645) and ``None`` on a readout
    that ran. It defaults to ``None`` so the healthy construction
    — the common one — still has to name every pragma it read.
    """

    __slots__ = (
        "busy_timeout",
        "cache_size",
        "db",
        "error",
        "foreign_keys",
        "journal_mode",
        "synchronous",
    )

    def __init__(
        self,
        *,
        db: DBName,
        journal_mode: str,
        foreign_keys: int,
        synchronous: int,
        cache_size: int,
        busy_timeout: int,
        error: str | None = None,
    ) -> None:
        self.db = db
        self.journal_mode = journal_mode
        self.foreign_keys = foreign_keys
        self.synchronous = synchronous
        self.cache_size = cache_size
        self.busy_timeout = busy_timeout
        # Non-``None`` ⇒ nothing above was read; see :func:`_read`.
        self.error = error

    @classmethod
    def failed(cls, *, db: DBName, error: str) -> _PragmaSnapshot:
        """Snapshot for an engine that could not be read at all.

        The six pragma fields keep their declared types but carry
        no meaning here; the renderer draws the error row and
        never touches them. The placeholders live in this one
        place rather than as ``__init__`` defaults, so a healthy
        construction that forgets a pragma is still a type error.
        """
        return cls(
            db=db,
            journal_mode="",
            foreign_keys=0,
            synchronous=0,
            cache_size=0,
            busy_timeout=0,
            error=error,
        )


async def _read(registry: EngineRegistry, db: DBName) -> _PragmaSnapshot:
    """Read the six pragmas via a fresh connection.

    Reads in one ``connect()`` block — pragma values are connection-
    scoped (well, file-scoped for journal_mode, connection-scoped for
    the rest), so batching them on one connection avoids paying the
    pool-acquire cost six times for a diagnostic that runs at human
    rate.

    Never raises: an engine that cannot be opened or read at all
    is reported as a snapshot carrying its exception class
    (#1645), so the caller's comprehension over :data:`ALL_DBS`
    survives a single broken engine. Same posture and the same
    row shape as :func:`admin.integrity._read`.
    """
    try:
        engine = registry.engine(db)
        async with engine.connect() as conn:
            jm = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
            fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
            sync = (await conn.execute(text("PRAGMA synchronous"))).scalar_one()
            cs = (await conn.execute(text("PRAGMA cache_size"))).scalar_one()
            bt = (await conn.execute(text("PRAGMA busy_timeout"))).scalar_one()
    except Exception as exc:  # noqa: BLE001 - classify any engine-side failure
        log.bind(db=db.value, error=type(exc).__name__).warning(
            "pragma readout failed; engine reported as unreadable"
        )
        return _PragmaSnapshot.failed(db=db, error=type(exc).__name__)
    return _PragmaSnapshot(
        db=db,
        journal_mode=str(jm).lower(),
        foreign_keys=int(fk),
        synchronous=int(sync),
        cache_size=int(cs),
        busy_timeout=int(bt),
    )


def _flag(ok: bool) -> str:
    """Single-glyph indicator so an operator scanning the card finds
    drift without reading every line. ✅/⚠ — same glyphs other admin
    cards use for spotcheck-style output."""
    return "✅" if ok else "⚠"


def _render(snaps: list[_PragmaSnapshot]) -> str:
    lines = ["🧪 <b>Engine PRAGMA readout</b>", ""]
    any_drift = False
    any_error = False
    for s in snaps:
        if s.error is not None:
            # The readout never ran, so there are no pragma rows to
            # draw — the exception class IS the finding (#1645).
            # Same row shape as /admin_dbprobe and /admin_integrity.
            any_error = True
            lines.append(f"<b>{s.db.value}</b> ⚠ <code>{s.error}</code>")
            lines.append("")
            continue
        sync_label = _SYNCHRONOUS_LABELS.get(s.synchronous, str(s.synchronous))
        expected_sync = synchronous_level(s.db)
        wal_ok = s.journal_mode == "wal"
        fk_ok = s.foreign_keys == 1
        sync_ok = sync_label == expected_sync
        if not (wal_ok and fk_ok and sync_ok):
            any_drift = True
        lines.append(f"<b>{s.db.value}</b>")
        lines.append(f"  • journal_mode: <code>{s.journal_mode}</code> {_flag(wal_ok)}")
        lines.append(f"  • foreign_keys: <code>{s.foreign_keys}</code> {_flag(fk_ok)}")
        lines.append(
            f"  • synchronous: <code>{sync_label}</code> "
            f"(expected <code>{expected_sync}</code>) {_flag(sync_ok)}"
        )
        lines.append(f"  • cache_size: <code>{s.cache_size}</code>")
        lines.append(f"  • busy_timeout: <code>{s.busy_timeout}</code> ms")
        lines.append("")
    if any_drift:
        lines.append(
            "<i>⚠ One or more PRAGMAs drifted from expected. "
            "Pragma listener at db.engines may not be firing on "
            "every connect — check the event hook.</i>"
        )
    if any_error:
        # Deliberately a second line rather than an ``elif``: an
        # unreadable engine and a drifted pragma on a different
        # engine are independent findings, and hiding one behind
        # the other is exactly the failure #1645 is about.
        lines.append(
            "<i>⚠ One or more engines could not be read at all; the "
            "exception class is on the row. Nothing can be said about "
            "their pragmas — start from /admin_dbprobe.</i>"
        )
    if not (any_drift or any_error):
        lines.append("<i>All engines configured as expected.</i>")
    return "\n".join(lines)


async def handle_admin_pragmas(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_pragmas; silently dropped"
        )
        return
    snaps = [await _read(registry, db) for db in ALL_DBS]
    text_out = _render(snaps)
    await message.answer(text_out)
    log.bind(user_id=user.id).info("/admin_pragmas rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.pragmas")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_pragmas(message, settings, registry)

    router.message.register(_entry, Command("admin_pragmas", ignore_case=True))
    return router
