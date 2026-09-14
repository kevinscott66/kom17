"""``/admin_loop`` — running asyncio loop configuration snapshot.

Complements /admin_tasks (which lists *what's running*) by surfacing
the loop config itself: implementation class, debug flag, the
slow-callback threshold, and whether the loop is currently closed
or running. Together they answer two diagnostic questions:

* "Is the loop the implementation we expect?" — selector vs proactor
  (Windows), the default selector class vs uvloop (which we don't
  use here, but a future dep bump could swap silently). A swap that
  changes Task lifecycle semantics is exactly the regression this
  card pins.
* "Is debug mode on in production?" — :meth:`asyncio.get_running_loop().get_debug`
  silently doubles wall-clock cost of every coroutine and is the
  one config flag an operator can flip from PYTHONASYNCIODEBUG=1
  without realising. The card surfaces it so a "the bot got slow
  after Tuesday's restart" investigation has a one-card answer.

The slow-callback threshold (default 0.1s) controls when the loop
logs a ``Executing <Task ...> took ... seconds`` warning to stderr.
Operators tuning that threshold via :meth:`loop.slow_callback_duration`
need this card to verify the value actually took — there's no
other Telegram-visible way to inspect it.

Pure stdlib, no DB, no IO. Same posture as every other ``/admin_*``:
silent-drop for non-devs, private-only at the router level.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.loop")


class _LoopSnapshot:
    """One read of the running loop's state.

    All four fields are read off the same loop object back-to-back
    so the surface is internally consistent — a debug-flag flip
    between two reads would leave the operator chasing a phantom.
    """

    __slots__ = (
        "debug",
        "impl_class",
        "is_closed",
        "is_running",
        "python_impl",
        "slow_callback_s",
    )

    def __init__(
        self,
        *,
        impl_class: str,
        debug: bool,
        slow_callback_s: float,
        is_running: bool,
        is_closed: bool,
        python_impl: str,
    ) -> None:
        self.impl_class = impl_class
        self.debug = debug
        self.slow_callback_s = slow_callback_s
        self.is_running = is_running
        self.is_closed = is_closed
        self.python_impl = python_impl


def _capture() -> _LoopSnapshot:
    """Capture the running loop's surface in one pass.

    Uses :func:`asyncio.get_running_loop` (not ``get_event_loop``)
    because we are demonstrably inside an aiogram handler — there
    IS a running loop. The deprecated fallback would silently
    create a fresh loop and lie to the operator. If somehow this
    runs without a loop the ``RuntimeError`` propagates as the
    error router's responsibility — masking it would hide a real
    pipeline bug.
    """
    loop = asyncio.get_running_loop()
    return _LoopSnapshot(
        impl_class=type(loop).__name__,
        debug=loop.get_debug(),
        slow_callback_s=float(loop.slow_callback_duration),
        is_running=loop.is_running(),
        is_closed=loop.is_closed(),
        python_impl=sys.implementation.name,
    )


def _render(snap: _LoopSnapshot) -> str:
    lines = ["🔁 <b>Event loop</b>", ""]
    lines.append(
        f"• Implementation: <code>{snap.impl_class}</code> (Python <code>{snap.python_impl}</code>)"
    )
    # Debug surfaces with an ⚠ when on — in production it should
    # be off. An operator scanning the card needs the cost flag
    # to stand out, not just sit in the column.
    if snap.debug:
        lines.append("• Debug: <code>on</code> ⚠ (slows every coroutine)")
    else:
        lines.append("• Debug: <code>off</code>")
    lines.append(f"• Slow-callback threshold: <code>{snap.slow_callback_s:.3f}s</code>")
    # Running + closed are both rendered because an operator who
    # reaches /admin_loop after a graceful-shutdown bug (handler
    # ran but the loop is mid-tearing-down) needs both bits to
    # diagnose; either alone is ambiguous.
    lines.append(
        f"• Running: <code>{'yes' if snap.is_running else 'no'}</code>"
        f"; closed: <code>{'yes' if snap.is_closed else 'no'}</code>"
    )
    lines.append("")
    lines.append(
        "<i>Debug=on doubles per-coroutine cost — check this on a "
        "post-restart slowness report. Slow-callback warnings hit "
        "stderr when any callback exceeds the threshold; tune via "
        "<code>loop.slow_callback_duration</code> at startup.</i>"
    )
    return "\n".join(lines)


async def handle_admin_loop(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_loop; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_loop rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.loop")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_loop(message, settings)

    router.message.register(_entry, Command("admin_loop", ignore_case=True))
    return router
