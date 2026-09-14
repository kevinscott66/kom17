"""``/admin_tasks`` — live snapshot of asyncio tasks.

Complements /admin_engines (pool-level resource state) and /admin_uptime
(process clock). This card surfaces the **scheduler-level** state:
which coroutines are alive in the event loop right now, what they're
awaiting, and which of them have been pending long enough to be
suspicious.

Why an operator wants this:

* Suspected hang. The bot stops responding to a particular command;
  the operator's first question is "did a coroutine block?". A task
  that's been pending for minutes is a strong signal. /admin_engines
  shows the pool-side symptom (checked_out climbing); this card
  shows the coroutine-side cause.
* Leak detection. Background tasks fired with ``asyncio.create_task``
  and never awaited can pile up silently. Comparing the task count
  between two invocations a few minutes apart is the cheapest way
  to spot the leak before the loop slows.
* Post-deploy verification. After landing a change that adds a
  long-running background loop (e.g. a periodic GC sweep), the
  operator wants confirmation that exactly one instance is running
  — not zero (forgot to start it), not many (start logic raced).

We sample :func:`asyncio.all_tasks` once at message-handle time and
render the count plus a capped sample of task names + coroutine
qualnames. ``get_name()`` is the human-readable identifier set by
aiogram or the developer; the coroutine qualname is the fallback
that disambiguates anonymous tasks. Tasks are sorted by name for
stable diffs across snapshots — the alternative (creation order)
flaps on transient updates and makes the card useless for spotting
leaks.

The current task (the one handling /admin_tasks) is filtered out so
it doesn't pollute the sample with itself — without the filter the
card would always have a "self" entry that an operator new to the
codebase would have to learn to ignore.

Cost: O(N) over alive tasks. In healthy state N is single-digit;
even in a leak scenario the upper bound is millions before the
loop's task-table allocation pressure becomes the dominant symptom,
and we cap rendered rows at 20 so the card stays sendable.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not leak dev IDs), private-only at the router level
(task names can carry user IDs or chat IDs as context the operator
would not want to show in a shared admin group).
"""

from __future__ import annotations

import asyncio
import html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.tasks")


# Rendered-row cap. 20 names × ~100 chars ≈ 2 KiB raw — comfortable
# headroom under Telegram's 4096-char limit even with HTML envelope.
# Above 20 the operator should be looking at process-level tools
# (py-spy, gdb) rather than this card; the truncation tail surfaces
# that situation.
_MAX_SAMPLE = 20


class _TaskRow:
    """One task's identifying surface.

    ``name`` is :meth:`asyncio.Task.get_name`'s output — usually
    ``Task-<n>`` for unnamed tasks, or a developer-set name. ``coro``
    is the coroutine qualname, which disambiguates anonymous tasks
    by showing what function they're inside. Both render because
    either one alone leaves an operator guessing on a real incident.
    """

    __slots__ = ("coro", "done", "name")

    def __init__(self, *, name: str, coro: str, done: bool) -> None:
        self.name = name
        self.coro = coro
        self.done = done


def _describe_coro(task: asyncio.Task[object]) -> str:
    """Best-effort coroutine identifier.

    ``Task.get_coro()`` returns the coroutine object; its
    ``__qualname__`` is the function-level name (``handle_admin_tasks``
    rather than the ``<coroutine object at 0x...>`` repr). On a
    coroutine that has already finished and been awaited, ``get_coro``
    may return ``None`` — we surface ``<done>`` rather than crash,
    because the card has to keep rendering through any task state.
    """
    coro = task.get_coro()
    if coro is None:
        return "<done>"
    qualname = getattr(coro, "__qualname__", None)
    if qualname:
        return str(qualname)
    # Fallback for non-coroutine awaitables wrapped in a Task — rare,
    # but C-extension awaitables hit this path. ``repr`` is verbose
    # but at least non-empty.
    return repr(coro)


def _sample_tasks() -> list[_TaskRow]:
    """Snapshot all tasks except the one running this handler.

    Self-exclusion matters: without it the card always has a
    /admin_tasks entry in its own output, which is noise for an
    operator new to the codebase. ``current_task`` may raise outside
    a running loop — we're inside one by construction (the handler
    is async), so the call is safe here."""
    try:
        current = asyncio.current_task()
    except RuntimeError:
        # Defensive: ``current_task`` raises if no loop is running.
        # Inside an aiogram handler there always IS a loop, so this
        # branch is unreachable in practice; surface as no-filter
        # rather than crash if it ever fires.
        current = None
    rows: list[_TaskRow] = []
    for task in asyncio.all_tasks():
        if task is current:
            continue
        rows.append(
            _TaskRow(
                name=task.get_name(),
                coro=_describe_coro(task),
                done=task.done(),
            )
        )
    rows.sort(key=lambda r: (r.name, r.coro))
    return rows


def _render(rows: list[_TaskRow]) -> str:
    lines = ["⚙️ <b>Event-loop tasks</b>", ""]
    total = len(rows)
    lines.append(f"<i>Live tasks (excluding /admin_tasks itself): <b>{total}</b></i>")
    lines.append("")
    if not rows:
        # Empty is the genuinely-no-other-tasks state. Surface it
        # explicitly rather than rendering an empty bullet list.
        lines.append("<i>No other tasks alive in the loop.</i>")
        return "\n".join(lines)
    for r in rows[:_MAX_SAMPLE]:
        # ``done`` tasks are pending GC — surface separately so the
        # operator doesn't conflate them with stuck pending tasks.
        # In a healthy loop ``done`` tasks linger for milliseconds
        # at most; a card showing many done tasks means GC pressure.
        state = "done" if r.done else "pending"
        lines.append(
            f"• <code>{html.escape(r.name)}</code> "
            f"(<code>{html.escape(r.coro)}</code>) — <i>{state}</i>"
        )
    if total > _MAX_SAMPLE:
        remaining = total - _MAX_SAMPLE
        lines.append(f"<i>… and {remaining} more</i>")
    return "\n".join(lines)


async def handle_admin_tasks(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_tasks; silently dropped"
        )
        return
    rows = _sample_tasks()
    await message.answer(_render(rows))
    log.bind(user_id=user.id).info("/admin_tasks rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.tasks")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_tasks(message, settings)

    router.message.register(_entry, Command("admin_tasks", ignore_case=True))
    return router
