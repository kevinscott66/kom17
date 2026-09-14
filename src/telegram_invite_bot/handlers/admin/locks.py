"""``/admin_locks`` — kernel file-lock table from /proc/locks.

The existing process / IO surface (proc, tasks, fds, io, smaps,
diskstats) explains *what* a process is doing, but says nothing
about **why a process is stuck**. The most common reason a daemon
or a worker hangs without consuming CPU is that it's blocked
waiting on a file lock somebody else holds. That's what
/proc/locks exposes — every POSIX (fcntl), BSD (flock), and OFD
(open file description) lock currently held kernel-wide, plus the
queue of waiters behind each.

Why this matters operationally:

* **SQLite contention.** Every one of our 5 .db files is opened
  by the bot under POSIX locks. A stuck writer (long transaction,
  deadlock with a backup script, a `sqlite3` REPL the operator
  forgot about) shows up here as a WRITE lock on the matching
  major:minor:inode plus one or more BLOCKED waiters. `lsof` and
  `fuser` see the open fd, but only /proc/locks sees the *queue*.
* **systemd-journald / log-rotate fights** — flock contention on
  /var/log shows up here without any other surface explaining the
  symptom.
* **Stuck deploy scripts** — a flock-protected critical section
  abandoned by a crashed parent leaves a lock held by a dead pid;
  walking /proc/locks vs. /proc/<pid> identifies it.

The operationally interesting predicate is **blocked waiters**.
The kernel formats a waiter row with ``->`` between the pid and
the major:minor:inode triple: ``5: POSIX ADVISORY WRITE 1234 ->
08:01:1234567 0 EOF``. Holders have no arrow. We count waiters
separately and ⚠ above a modest threshold — a handful is normal
background contention (any active SQLite workload has brief
queues), a sustained pile means somebody is genuinely stuck.

Forward-compat: the format has been stable since the lock manager
was rewritten in 2.6.x. Lines are
``<id>: <type> <kind> <access> <pid> [->] <maj>:<min>:<inode>
<start> <end>``. The type set is small and bounded (POSIX, FLOCK,
OFDLCK, LEASE, DELEG); we don't enumerate it because new types
should pass through untouched. End is either an integer or the
literal ``EOF``; we preserve it verbatim.

⚠ predicate: single, on the blocked-waiter count. No per-lock
markers — a single long-held WRITE lock is normal SQLite
behaviour, not a problem. The queue is the signal.

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


log = logger.bind(component="handlers.admin.locks")


_LOCKS_PATH = Path("/proc/locks")

# Threshold above which the BLOCKED-waiter count gets ⚠. Picked
# low enough to catch real contention (deadlocks, abandoned
# critical sections) and high enough that brief SQLite queues
# during a busy moment don't trip it. Same shape as the ARP
# INCOMPLETE threshold — a handful is normal, a pile is the signal.
_BLOCKED_WARN_THRESHOLD = 5

# Cap rendered rows. /proc/locks on a busy host (NFS server,
# heavy DB workload) can hold hundreds of entries; Telegram
# message length is the real constraint. Full count remains on
# the snapshot for any drill-down caller.
_RENDER_ROW_CAP = 30


class _LockEntry:
    """One row of /proc/locks.

    All fields preserved verbatim. ``blocked`` is the
    holder-vs-waiter discriminator — the kernel encodes it
    positionally via ``->`` between the pid and the file triple,
    we lift it to a flag so the render path doesn't reparse.
    """

    __slots__ = (
        "access",
        "blocked",
        "end",
        "inode",
        "kind",
        "lock_id",
        "major_minor",
        "pid",
        "start",
        "type",
    )

    def __init__(
        self,
        *,
        lock_id: str,
        type: str,
        kind: str,
        access: str,
        pid: str,
        blocked: bool,
        major_minor: str,
        inode: str,
        start: str,
        end: str,
    ) -> None:
        self.lock_id = lock_id
        self.type = type
        self.kind = kind
        self.access = access
        self.pid = pid
        self.blocked = blocked
        self.major_minor = major_minor
        self.inode = inode
        self.start = start
        self.end = end


class _LocksSnapshot:
    """Captured /proc/locks.

    ``entries`` is every parsed row. ``available`` distinguishes
    Linux-with-empty (rare but possible — fresh boot, no IO yet)
    from macOS / non-procfs (file simply doesn't exist).
    """

    __slots__ = ("available", "entries")

    def __init__(self, *, entries: tuple[_LockEntry, ...], available: bool) -> None:
        self.entries = entries
        self.available = available

    @property
    def blocked_count(self) -> int:
        return sum(1 for e in self.entries if e.blocked)

    @property
    def holder_count(self) -> int:
        return sum(1 for e in self.entries if not e.blocked)


def _parse_locks(text: str) -> tuple[_LockEntry, ...]:
    """Parse /proc/locks. Returns the entry tuple.

    Each line is:

      <id>: <type> <kind> <access> <pid> <maj>:<min>:<inode>
            <start> <end>

    or, for a waiter:

      <id>: <type> <kind> <access> <pid> -> <maj>:<min>:<inode>
            <start> <end>

    The ``->`` is positional — when present, it sits between the
    pid and the major:minor:inode triple. We sniff that position
    and lift the result to a boolean rather than carrying the
    token around. Any line whose token count is outside the
    expected range (8 for holders, 9 for waiters) is dropped —
    a defensive degrade against mid-update reads or future kernel
    format extensions.
    """
    entries: list[_LockEntry] = []
    for raw_line in text.splitlines():
        tokens = raw_line.split()
        # Holder: id: type kind access pid maj:min:inode start end → 8
        # Waiter: id: type kind access pid -> maj:min:inode s e   → 9
        if len(tokens) not in (8, 9):
            continue
        # First token ends with ``:`` (the lock id).
        if not tokens[0].endswith(":"):
            continue
        blocked = len(tokens) == 9 and tokens[5] == "->"
        if blocked:
            file_triple_idx = 6
            tail_start = 7
        elif len(tokens) == 8 and tokens[5] != "->":
            file_triple_idx = 5
            tail_start = 6
        else:
            # 9 tokens without ``->`` at slot 5, or 8 tokens with
            # ``->`` somehow — neither shape matches the kernel's
            # lock-row grammar.
            continue
        triple = tokens[file_triple_idx]
        # ``maj:min:inode`` — exactly two colons. Reject otherwise.
        if triple.count(":") != 2:
            continue
        maj_min, _, inode = triple.rpartition(":")
        entries.append(
            _LockEntry(
                lock_id=tokens[0].rstrip(":"),
                type=tokens[1],
                kind=tokens[2],
                access=tokens[3],
                pid=tokens[4],
                blocked=blocked,
                major_minor=maj_min,
                inode=inode,
                start=tokens[tail_start],
                end=tokens[tail_start + 1],
            )
        )
    return tuple(entries)


def _capture(*, path: Path = _LOCKS_PATH) -> _LocksSnapshot:
    """Read /proc/locks + build a snapshot.

    OSError catches both ENOENT (macOS / non-procfs) and the
    rare EACCES case — though /proc/locks is world-readable on
    every Linux build we'd run, so EACCES is essentially
    unreachable. Symmetric handling with the rest of the surface.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _LocksSnapshot(entries=(), available=False)
    return _LocksSnapshot(entries=_parse_locks(text), available=True)


def _render(snap: _LocksSnapshot) -> str:
    lines = ["🔐 <b>Kernel file locks (/proc/locks)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/locks unavailable on this host — Linux-only "
            "surface (macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.entries:
        lines.append(
            "  <i>No file locks held — extremely quiet host or "
            "kernel built without lock-manager support.</i>"
        )
        return "\n".join(lines)

    total = len(snap.entries)
    holders = snap.holder_count
    blocked = snap.blocked_count
    blocked_marker = " ⚠" if blocked > _BLOCKED_WARN_THRESHOLD else ""

    lines.append(
        f"  <b>Total:</b> <code>{total}</code> "
        f"(<code>{holders}</code> held, "
        f"<code>{blocked}</code> blocked{blocked_marker})"
    )
    lines.append("")

    # Render waiters first — they're the operationally interesting
    # rows. Holders follow.
    ordered = sorted(snap.entries, key=lambda e: (not e.blocked, e.lock_id))
    rendered = ordered[:_RENDER_ROW_CAP]
    lines.append("  <b>Entries:</b>")
    for entry in rendered:
        state = "BLOCKED" if entry.blocked else "HELD"
        lines.append(
            f"  • <code>#{entry.lock_id}</code> "
            f"<i>{entry.type} {entry.kind} {entry.access}</i> "
            f"pid=<code>{entry.pid}</code> "
            f"file=<code>{entry.major_minor}:{entry.inode}</code> "
            f"[<i>{state}</i>]"
        )
    if total > _RENDER_ROW_CAP:
        lines.append(
            f"  <i>… {total - _RENDER_ROW_CAP} more entries not shown "
            f"(cap <code>{_RENDER_ROW_CAP}</code>).</i>"
        )

    lines.append("")
    if blocked > _BLOCKED_WARN_THRESHOLD:
        lines.append(
            f"<i>⚠ <code>{blocked}</code> blocked waiters — somebody "
            "is holding a lock somebody else needs. A few are normal "
            "(SQLite under load has brief queues), a sustained pile "
            "usually means a stuck writer, a deadlock, or an "
            "abandoned critical section with a dead holder. "
            "Cross-reference the pid column with /admin_tasks to see "
            "if the holder is still alive; the major:minor:inode "
            "identifies the file. Our 5 SQLite databases are the "
            "most likely culprits.</i>"
        )
    else:
        lines.append(
            "<i>No per-row markers — a single long-held WRITE lock is "
            "normal SQLite / journald behaviour. The queue depth is "
            "the signal, not the lock count.</i>"
        )
    return "\n".join(lines)


async def handle_admin_locks(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_locks; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        total=len(snap.entries),
        holders=snap.holder_count,
        blocked=snap.blocked_count,
    ).info("/admin_locks rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.locks")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_locks(message, settings)

    router.message.register(_entry, Command("admin_locks", ignore_case=True))
    return router
