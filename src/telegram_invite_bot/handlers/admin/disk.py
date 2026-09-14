"""``/admin_disk`` — filesystem free-space snapshot for configured dirs.

Sibling to /admin_db_sizes (logical + WAL bytes per file) and
/admin_engines (runtime pool state). This card answers the
*infrastructure* question: how much room is left on the volume each
load-bearing directory lives on?

Why an operator wants this:

* Before a ``PRAGMA wal_checkpoint(TRUNCATE)`` on a hot DB — the
  checkpoint briefly doubles the file's footprint while it merges
  the WAL back into the main file. Running it with <100 MiB free
  on a multi-GiB DB has fired ENOSPC mid-checkpoint in the wild,
  leaving the WAL stranded.
* Before a manual ``cp database/*.db backups/`` — the same
  doubling concern.
* When ``logs_dir`` is on a separate volume (common on bare-metal
  with a dedicated log partition), watching free space here is the
  only way to spot a runaway log emitter before logrotate trips.
* Post-deploy sanity check: if the build artifact landed on the
  bot's volume by accident, free space drops sharply — this card
  is the cheapest way to confirm the deploy didn't bloat the host.

We surface every configured path as its **resolved** dir so the
operator sees the actual mount that will fill up, not the symbolic
config field. The DB dir, message-stats dir (which may diverge —
legacy artefact, see ``PathsConfig.resolved_message_stats_dir``),
and logs dir each get a row. Duplicate dirs (most common: the
default config where ``message_stats_dir`` falls back to
``database_dir``) are deduplicated so the card doesn't double-
count the same mount.

We use :func:`shutil.disk_usage`, which is a single statvfs syscall
per dir — cheap to run on demand. The ``percent_free`` column is
computed from total/free; a ⚠ glyph fires below 10% free, which is
the standard "you have one bad day before this fills" threshold.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not enumerate dev IDs), private-only at the router
level (disk-mount layout is operator-only context).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.disk")


# Threshold for the "running out" warning. Ten percent free is the
# inflection point at which a single day of normal log/DB growth on
# a busy bot would cross into ENOSPC territory — chosen to give the
# operator a full sleep cycle of warning before things break, not
# to be a precise alarm. Tighter thresholds (5%) flap on small
# volumes; looser (20%) waste the operator's attention on cosmetic
# fullness.
_LOW_FREE_PERCENT = 10.0


# Same byte-size formatter as /admin_db_sizes — kept as a private
# duplicate rather than imported, because making this card depend
# on a handler module would invert the dependency direction (admin
# cards are siblings; pulling helpers across them would couple
# them in ways that hurt later teardown of any single card).
def _fmt_bytes(n: int) -> str:
    """Human-readable bytes. B → KiB → MiB → GiB → TiB.

    Two-decimal precision because operator scanning the card cares
    about the order of magnitude plus one digit ("4.2 GiB" reads
    instantly; "4530241024" doesn't)."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    f = float(n)
    for unit in units:
        if f < 1024.0 or unit == units[-1]:
            return f"{f:.2f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024.0
    # Unreachable; the loop covers TiB via the ``unit == units[-1]`` guard.
    return f"{f:.2f} TiB"


class _DiskSnapshot:
    """One resolved-dir's disk usage sample.

    ``label`` is the config-field name (``database_dir``, etc.) and
    ``path`` is the resolved Path on disk — the pair so the operator
    can map the symbolic config back to the physical mount when
    debugging."""

    __slots__ = ("free", "label", "path", "total", "used")

    def __init__(self, *, label: str, path: Path, total: int, used: int, free: int) -> None:
        self.label = label
        self.path = path
        self.total = total
        self.used = used
        self.free = free

    @property
    def percent_free(self) -> float:
        """Free / total as a percentage. Guards against zero-total
        (an unmounted volume reports 0 from statvfs)."""
        if self.total <= 0:
            return 0.0
        return 100.0 * self.free / self.total

    @property
    def low(self) -> bool:
        """True iff free space is below the warn threshold. Drives
        the ⚠ glyph on this row."""
        return self.percent_free < _LOW_FREE_PERCENT


def _sample(label: str, path: Path) -> _DiskSnapshot | None:
    """Run :func:`shutil.disk_usage` on the resolved path.

    Returns ``None`` if the path doesn't exist on disk yet — the
    only scenario where that's possible in practice is a fresh
    container where the dir hasn't been created. Rendering "missing"
    rather than fabricating zeros keeps the diagnostic honest.
    """
    if not path.exists():
        return None
    usage = shutil.disk_usage(path)
    return _DiskSnapshot(
        label=label,
        path=path,
        total=int(usage.total),
        used=int(usage.used),
        free=int(usage.free),
    )


def _gather(settings: Settings) -> tuple[list[_DiskSnapshot], list[str]]:
    """Sample every configured path, deduplicated by resolved-path.

    Dedup is critical: in the default config ``message_stats_dir``
    falls back to ``database_dir`` and both rows would show
    identical numbers — visual noise that would train the operator
    to scan past the row. Dedup is keyed on the *resolved* Path so
    "different config fields pointing at the same mount" collapses
    into a single row whose ``label`` is the first-encountered
    field name (the operator can correlate via /admin_settings).
    """
    paths = settings.paths
    # Order matters: database_dir is the primary disk concern, so it
    # leads. message_stats_dir is second because it MAY diverge from
    # database_dir (legacy artefact). logs_dir is last — log volume
    # is usually monitored separately by logrotate, but having it
    # surfaced here is the only signal when logrotate is misconfigured.
    candidates: list[tuple[str, Path]] = [
        ("database_dir", paths.database_dir),
        ("message_stats_dir", paths.resolved_message_stats_dir()),
        ("logs_dir", paths.logs_dir),
    ]
    seen: set[Path] = set()
    snaps: list[_DiskSnapshot] = []
    missing: list[str] = []
    for label, path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        snap = _sample(label, resolved)
        if snap is None:
            missing.append(f"{label} ({resolved})")
        else:
            snaps.append(snap)
    return snaps, missing


def _render(snaps: list[_DiskSnapshot], missing: list[str]) -> str:
    lines = ["💾 <b>Disk usage</b>", ""]
    any_low = False
    for s in snaps:
        if s.low:
            any_low = True
        flag = " ⚠" if s.low else ""
        lines.append(f"<b>{s.label}</b>{flag}")
        lines.append(f"  • path: <code>{s.path}</code>")
        lines.append(
            f"  • free / total: <code>{_fmt_bytes(s.free)}</code> / "
            f"<code>{_fmt_bytes(s.total)}</code> "
            f"(<code>{s.percent_free:.1f}%</code> free)"
        )
        lines.append(f"  • used: <code>{_fmt_bytes(s.used)}</code>")
        lines.append("")
    for entry in missing:
        lines.append(f"<i>missing: {entry}</i>")
    if missing:
        lines.append("")
    if any_low:
        lines.append(
            "<i>⚠ At least one volume is below "
            f"{_LOW_FREE_PERCENT:.0f}% free. A WAL checkpoint or "
            "DB backup can briefly double the footprint — do NOT "
            "run those until free space is recovered.</i>"
        )
    else:
        lines.append("<i>All configured dirs have headroom.</i>")
    return "\n".join(lines)


async def handle_admin_disk(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_disk; silently dropped"
        )
        return
    snaps, missing = _gather(settings)
    await message.answer(_render(snaps, missing))
    log.bind(user_id=user.id).info("/admin_disk rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.disk")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_disk(message, settings)

    router.message.register(_entry, Command("admin_disk", ignore_case=True))
    return router
