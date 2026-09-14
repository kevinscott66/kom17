"""``/admin_threads`` — live OS-thread snapshot.

Complements /admin_tasks (asyncio coroutine state) with the **OS-thread
side** of the runtime. aiogram + dishka + aiosqlite all model their
work as async tasks on a single event loop; OS-thread count should
stay tiny (main thread + maybe a logging sink, sometimes an
``asyncio`` default-executor worker). Anything else is a code smell
worth surfacing — most often a leftover ``threading.Thread`` from the
legacy ``bot.py`` codepath that wasn't migrated to a task.

Why an operator wants this:

* Migration audit — the reason this card was built, and the one
  reason it no longer has. Legacy spawned threads for periodic
  workers (heartbeats, GC sweeps, scheduled DMs) and the card let an
  operator watch that count drop as workers ported to
  ``asyncio.create_task``. T-011 removed that process, so every
  thread the card lists today belongs to this one. The two reasons
  below are why it is still worth having.
* Leak detection. ``concurrent.futures.ThreadPoolExecutor`` (or
  aiogram's default executor) lazily spawns workers up to ``max_
  workers`` on each thread-offloaded call. If those grow without
  bound, this card is the place the symptom surfaces first —
  before the OS-level ``nproc`` ulimit gets hit and the bot starts
  refusing connections.
* Deadlock triage. A thread stuck on a ``threading.Lock.acquire``
  inside legacy code will show up here as "alive, daemon, not the
  main thread, name=<lock-holder>". The card doesn't dump stacks
  (that's a py-spy job), but the *existence* of the thread is the
  first signal.

Reads :func:`threading.enumerate` once at message-handle time. Sorted
by name for stable diffs across snapshots (the alternative — id
order — flaps on every GC pass and makes the card useless for
spotting migration progress).

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not leak dev IDs), private-only at the router level
(thread names occasionally carry user-tagged context the operator
would not want to render in a shared admin group).
"""

from __future__ import annotations

import html
import threading
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.threads")


# Rendered-row cap. Healthy thread count is single-digit; anything
# past 30 is a thread-pool explosion the operator should chase with
# py-spy rather than scroll through here. Truncation tail makes the
# overflow case visible.
_MAX_SAMPLE = 30


class _ThreadRow:
    """One thread's identifying surface.

    ``name`` is :attr:`threading.Thread.name` (developer-set or
    ``Thread-<n>`` default). ``daemon`` flag distinguishes "won't
    block process exit" workers (executor pools, log sinks) from
    "must finish before shutdown" threads (legacy synchronous
    workers) — the migration story cares about which kind is still
    alive. ``is_main`` collapses the special-case for the main
    thread so the renderer can tag it visually rather than make
    the operator parse names.
    """

    __slots__ = ("daemon", "is_main", "name")

    def __init__(self, *, name: str, daemon: bool, is_main: bool) -> None:
        self.name = name
        self.daemon = daemon
        self.is_main = is_main


def _sample_threads() -> list[_ThreadRow]:
    """Snapshot every alive thread.

    ``threading.enumerate`` returns the live set — finished threads
    are not included, so the card never shows pending-GC thread
    objects (unlike /admin_tasks, where ``done`` tasks linger).
    """
    main = threading.main_thread()
    rows: list[_ThreadRow] = []
    for t in threading.enumerate():
        rows.append(
            _ThreadRow(
                name=t.name,
                daemon=t.daemon,
                is_main=(t is main),
            )
        )
    # Main thread first (it's the conceptual anchor), then everything
    # else alphabetically. Pure-alphabetical would bury the main
    # thread mid-list under any ``Thread-N`` entry, which is
    # disorienting on an incident scan.
    rows.sort(key=lambda r: (not r.is_main, r.name))
    return rows


def _render(rows: list[_ThreadRow]) -> str:
    lines = ["🧵 <b>OS threads</b>", ""]
    total = len(rows)
    lines.append(f"<i>Alive threads: <b>{total}</b></i>")
    lines.append("")
    if not rows:
        # Unreachable in practice — the running interpreter always
        # has at least the main thread — but the empty branch keeps
        # the renderer total over its input rather than assuming a
        # nonempty invariant the caller has to maintain.
        lines.append("<i>No threads enumerated.</i>")
        return "\n".join(lines)
    for r in rows[:_MAX_SAMPLE]:
        kind_parts: list[str] = []
        if r.is_main:
            # Main-thread tag is load-bearing: an operator scanning
            # the card needs to know which entry is "the bot itself"
            # vs. a worker. Without the tag the main thread is
            # indistinguishable from any other named thread.
            kind_parts.append("main")
        kind_parts.append("daemon" if r.daemon else "non-daemon")
        kind = ", ".join(kind_parts)
        # Thread names come from whatever created the thread —
        # our code, aiohttp, the stdlib executor, any third-party
        # library in the tree. Under the bot-wide parse_mode=HTML
        # a single stray angle bracket in one of them makes
        # Telegram reject the entire card with 400, so the
        # developer loses the whole thread list over one badly
        # named worker.
        lines.append(f"• <code>{html.escape(r.name)}</code> — <i>{kind}</i>")
    if total > _MAX_SAMPLE:
        remaining = total - _MAX_SAMPLE
        lines.append(f"<i>… and {remaining} more</i>")
    lines.append("")
    lines.append(
        "<i>Healthy state: main + a handful of daemon workers. "
        "Growing non-daemon counts during the legacy migration "
        "means a threading.Thread spawn site hasn't ported to "
        "asyncio.create_task yet.</i>"
    )
    return "\n".join(lines)


async def handle_admin_threads(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_threads; silently dropped"
        )
        return
    rows = _sample_threads()
    await message.answer(_render(rows))
    log.bind(user_id=user.id).info("/admin_threads rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.threads")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_threads(message, settings)

    router.message.register(_entry, Command("admin_threads", ignore_case=True))
    return router
