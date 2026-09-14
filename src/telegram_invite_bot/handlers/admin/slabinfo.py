"""``/admin_slabinfo`` — kernel slab cache from /proc/slabinfo.

The existing memory surface explains userspace consumption
(meminfo, smaps, memory, rusage) and the page-allocator state
(buddyinfo, zoneinfo). What none of them surfaces is **kernel-side
memory accounting** — the SLUB/SLAB caches that hold kernel
objects (dentries, inodes, task_structs, kmalloc-N pools, …).

Why this matters on a long-running host:

* **Dentry / inode-cache explosion.** A workload that recursively
  walks or creates many small files (npm install, ``find /``, a
  buggy log rotator, a container building images) blows the
  ``dentry`` and ``inode_cache`` slabs up to multi-GiB. The
  kernel keeps them in the slab cache as reclaimable memory, so
  ``MemFree`` looks normal, but slab pressure shows up as
  surprising direct-reclaim spikes that /admin_zoneinfo sees but
  can't explain. This card is the explanation.
* **kmalloc-256 / kmalloc-512 growth** — a kernel-side leak or a
  driver holding allocations. Operationally rare but the only
  surface that names it.
* **task_struct / files_cache totals** — count of in-flight
  kernel objects per process; spikes correlate with fork bombs
  or fd-leak storms that /admin_fds counts from userspace.

The card is curated by total memory footprint: we compute
``active_objs * objsize`` per cache and render the top
``_TOP_N`` rows sorted by that. Full parsed table remains on the
snapshot so a drill-down caller (a future "show cache X history"
card) can read it without re-parsing.

Important caveat the docstring documents and the test pins:
**/proc/slabinfo is typically root-readable only**. A non-root bot
on a hardened distro will see 0o400 permissions and the read
fails. We treat this identically to /proc/slabinfo being absent
(macOS dev / non-procfs container) — ``available=False`` + an
explicit note that says "Linux + non-root sees this too". The
operator then knows to check whether the bot is running as root
without us having to surface uid context.

Forward-compat: the format has been stable since the SLUB
allocator landed (kernel 2.6.22, ~2007). First line is
``slabinfo - version: 2.1`` (or 2.0 on older builds); second is
a ``# name <active_objs> …`` column-header comment. Data rows
have ≥6 leading fields (name, active, num, size, objperslab,
pagesperslab) and the ``: tunables …`` and ``: slabdata …``
suffixes carry tuning info we don't surface. We parse the
leading 6 fields and ignore the rest, so a future kernel that
extends the suffix doesn't break the parser.

⚠ predicate: zero by design. There's no universal "this cache is
too big" threshold — a 4 GiB dentry cache is normal on a build
host and pathological on a small bot VPS. Same posture as
/admin_sockstat / /admin_softirqs / /admin_zoneinfo's curated-
zone filter: we surface the data, operator policy decides.

Same posture as every other admin card — silent-drop, private-only,
pure stdlib, hermetic via keyword-only path injection.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.slabinfo")


_SLABINFO_PATH = Path("/proc/slabinfo")

# How many caches to render, sorted by descending memory footprint.
# Telegram message length is the real constraint; the top-20 captures
# the operationally interesting caches on every host we'd care to
# diagnose. Full parsed table remains on the snapshot.
_TOP_N = 20


class _SlabRow:
    """One slab cache.

    All sizes are in bytes. ``footprint`` = active_objs * objsize
    is precomputed — it's what we sort by and the operator's
    primary "is this cache big?" axis. Other fields preserved for
    drill-down callers.
    """

    __slots__ = (
        "active_objs",
        "footprint",
        "name",
        "num_objs",
        "objperslab",
        "objsize",
        "pagesperslab",
    )

    def __init__(
        self,
        *,
        name: str,
        active_objs: int,
        num_objs: int,
        objsize: int,
        objperslab: int,
        pagesperslab: int,
    ) -> None:
        self.name = name
        self.active_objs = active_objs
        self.num_objs = num_objs
        self.objsize = objsize
        self.objperslab = objperslab
        self.pagesperslab = pagesperslab
        self.footprint = active_objs * objsize


class _SlabinfoSnapshot:
    """Captured /proc/slabinfo.

    ``rows`` — every parsed cache, in file order.
    ``version`` — the ``slabinfo - version: X.Y`` string for
    forensics if a future kernel changes the format.
    ``available`` — False when read failed (macOS dev OR
    Linux + non-root + 0o400 perms; deliberately not distinguished
    here because the operator response is the same: check shell).
    """

    __slots__ = ("available", "rows", "version")

    def __init__(
        self,
        *,
        rows: tuple[_SlabRow, ...],
        version: str,
        available: bool,
    ) -> None:
        self.rows = rows
        self.version = version
        self.available = available


def _parse_slabinfo(text: str) -> tuple[tuple[_SlabRow, ...], str]:
    """Parse /proc/slabinfo. Returns (rows, version_string).

    Skip the ``# name …`` header comment and the
    ``slabinfo - version: X.Y`` line (we extract the version for
    forensics). Data row layout:

      <name> <active> <num> <size> <objperslab> <pagesperslab> :
        tunables <limit> <batch> <shared> :
        slabdata <active_slabs> <num_slabs> <sharedavail>

    Only the first 6 fields are parsed; the ``: tunables`` and
    ``: slabdata`` suffixes are ignored. Lines with fewer than 6
    parseable leading int fields are dropped.
    """
    rows: list[_SlabRow] = []
    version = "unknown"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("slabinfo"):
            # ``slabinfo - version: 2.1``
            parts = line.split(":", 1)
            if len(parts) == 2:
                version = parts[1].strip()
            continue
        if line.startswith("#"):
            # Header comment.
            continue
        tokens = line.split()
        if len(tokens) < 6:
            continue
        name = tokens[0]
        try:
            active_objs = int(tokens[1])
            num_objs = int(tokens[2])
            objsize = int(tokens[3])
            objperslab = int(tokens[4])
            pagesperslab = int(tokens[5])
        except ValueError:
            # Not an int-shaped row — kernel-emitted formatting
            # for the comment headers can have unexpected content
            # on some configs; degrade rather than crash.
            continue
        rows.append(
            _SlabRow(
                name=name,
                active_objs=active_objs,
                num_objs=num_objs,
                objsize=objsize,
                objperslab=objperslab,
                pagesperslab=pagesperslab,
            )
        )
    return (tuple(rows), version)


def _capture(*, path: Path = _SLABINFO_PATH) -> _SlabinfoSnapshot:
    """Read /proc/slabinfo + build a snapshot.

    OSError covers both ENOENT (macOS / non-procfs) and EACCES
    (Linux + non-root + default 0o400 perms). We don't distinguish
    them because the operator's response is identical — the
    rendered card mentions both.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _SlabinfoSnapshot(rows=(), version="unknown", available=False)
    rows, version = _parse_slabinfo(text)
    return _SlabinfoSnapshot(rows=rows, version=version, available=True)


def _fmt_bytes(n: int) -> str:
    """Compact human bytes — KiB / MiB / GiB. Pure-stdlib (no
    psutil) because everything else on this surface is too."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MiB"
    return f"{n / (1024 * 1024 * 1024):.2f} GiB"


def _render(snap: _SlabinfoSnapshot) -> str:
    lines = ["🧱 <b>Kernel slab caches (/proc/slabinfo)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/slabinfo unavailable — either Linux-only "
            "surface (macOS dev / non-procfs container sees this) "
            "OR Linux with non-root permissions (default 0o400). "
            "If on Linux: confirm the bot's effective uid.</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append("  <i>parse failed or empty file — extremely unusual; check kernel build.</i>")
        return "\n".join(lines)

    total_footprint = sum(r.footprint for r in snap.rows)
    lines.append(
        f"  <b>Caches reported:</b> <code>{len(snap.rows)}</code>  "
        f"<b>Format version:</b> <code>{snap.version}</code>"
    )
    lines.append(f"  <b>Total slab footprint:</b> <code>{_fmt_bytes(total_footprint)}</code>")
    lines.append("")
    lines.append(f"  <b>Top <code>{_TOP_N}</code> caches by footprint</b> (active × size):")

    # Sort by footprint desc; stable sort means ties preserve file
    # order, which is the kernel's own ordering and tends to be
    # somewhat meaningful (related caches grouped).
    top = sorted(snap.rows, key=lambda r: r.footprint, reverse=True)[:_TOP_N]
    for row in top:
        lines.append(
            f"  • <code>{row.name}</code>: "
            f"footprint=<code>{_fmt_bytes(row.footprint)}</code> "
            f"(active=<code>{row.active_objs:,}</code> / "
            f"num=<code>{row.num_objs:,}</code> × "
            f"size=<code>{row.objsize}</code> B)"
        )

    truncated = len(snap.rows) - len(top)
    if truncated > 0:
        lines.append(
            f"  <i>… {truncated} smaller caches not shown (cap <code>{_TOP_N}</code>).</i>"
        )

    lines.append("")
    lines.append(
        "<i>No warning markers on this card by design — &quot;big "
        "cache&quot; is workload-specific. Dentry/inode growth that "
        "looks pathological on a small bot VPS is routine on a build "
        "host. Sustained growth over time (not visible from a "
        "point-in-time snapshot) is the actionable signal. Compare "
        "with /admin_zoneinfo (reclaimable slab counts toward "
        "reclaim pressure) and /admin_meminfo for the SReclaimable / "
        "SUnreclaim split.</i>"
    )
    return "\n".join(lines)


async def handle_admin_slabinfo(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_slabinfo; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    top_row = max(snap.rows, key=lambda r: r.footprint, default=None)
    log.bind(
        user_id=user.id,
        available=snap.available,
        version=snap.version,
        cache_count=len(snap.rows),
        total_footprint=sum(r.footprint for r in snap.rows),
        top_cache=top_row.name if top_row else None,
        top_footprint=top_row.footprint if top_row else None,
    ).info("/admin_slabinfo rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.slabinfo")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_slabinfo(message, settings)

    router.message.register(_entry, Command("admin_slabinfo", ignore_case=True))
    return router
