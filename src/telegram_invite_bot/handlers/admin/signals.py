"""``/admin_signals`` — installed POSIX signal-handler snapshot.

The graceful-shutdown contract for a long-running bot is "SIGTERM
arrives → finish in-flight handlers → close DB engines → exit".
That contract lives in :mod:`telegram_invite_bot.app` (or wherever
the loop install runs) and is exactly the kind of wiring that
breaks silently: an aiogram update, an asyncio default, or even a
third-party library can install its own handler over ours, and the
only symptom is "the deploy hung on shutdown last time".

Why an operator wants this:

* Pre-deploy verification. Before triggering a rolling restart on
  production, an operator wants to confirm SIGTERM has a
  Python-level handler (not ``SIG_DFL``) — otherwise the
  shutdown will kill the process mid-DB-write and a half-written
  WAL page corrupts ``economy.db``.
* Post-incident triage. After a hang on shutdown, the question is
  "did our handler get installed or did something overwrite it?".
  This card answers that question without a strace.
* Defence against ``SIG_IGN``. A library that calls
  :func:`signal.signal` with ``SIG_IGN`` makes the bot un-killable
  by the normal shutdown path; the operator has to ``kill -9``,
  which is exactly the corruption case above.

We render the load-bearing signals (SIGINT, SIGTERM, SIGHUP, SIGUSR1,
SIGUSR2) with the human-readable handler identity:

* ``SIG_DFL`` → "default (process death)"
* ``SIG_IGN`` → "ignored ⚠"
* anything else → the callable's qualname (so an operator sees
  "Application._shutdown" rather than ``<function at 0x...>``)

On a non-main thread :func:`signal.getsignal` raises ``ValueError``;
since the handler is called inside an aiogram coroutine which always
runs on the main thread we treat that as unreachable and surface
"unavailable" rather than crashing the card.

Same posture as every other ``/admin_*``: silent-drop for non-devs,
private-only at the router level.
"""

from __future__ import annotations

import html
import signal
from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.signals")


# The signals an operator cares about for a Telegram bot. SIGINT +
# SIGTERM are the graceful-shutdown pair (systemd sends SIGTERM,
# Ctrl-C sends SIGINT); SIGHUP is the conventional reload trigger;
# SIGUSR1/SIGUSR2 are the user-defined slots a future feature might
# wire to "rotate logs" / "dump state". Rendering them even when
# unset (defaults to SIG_DFL) lets an operator see at a glance which
# slots are available for new wiring without re-reading the source.
#
# Platform note: SIGUSR1/SIGUSR2 don't exist on Windows. Defensive
# getattr on the signal module covers that (this bot is Linux-only
# in prod but tests run on macOS where both exist).
_SIGNALS_OF_INTEREST: tuple[str, ...] = (
    "SIGINT",
    "SIGTERM",
    "SIGHUP",
    "SIGUSR1",
    "SIGUSR2",
)


class _SignalRow:
    """One signal's installed handler.

    ``handler_desc`` is the rendered identity — the raw handler
    callable goes through :func:`_describe_handler` first so the
    renderer only deals with strings. ``is_default`` and
    ``is_ignored`` get pre-computed for marker rendering rather
    than re-string-matching in :func:`_render` (re-matching would
    couple the renderer to the exact phrasing :func:`_describe_handler`
    produces).
    """

    __slots__ = ("handler_desc", "is_default", "is_ignored", "name")

    def __init__(
        self,
        *,
        name: str,
        handler_desc: str,
        is_default: bool,
        is_ignored: bool,
    ) -> None:
        self.name = name
        self.handler_desc = handler_desc
        self.is_default = is_default
        self.is_ignored = is_ignored


def _describe_handler(handler: Any) -> tuple[str, bool, bool]:  # noqa: ANN401
    """Return (rendered, is_default, is_ignored).

    ``signal.getsignal`` returns:
    * :data:`signal.SIG_DFL` (the integer 0) for default disposition
    * :data:`signal.SIG_IGN` (the integer 1) for explicitly ignored
    * a callable for a Python-installed handler
    * ``None`` on a couple of CPython edge cases (handler installed
      outside Python's view, e.g. via the C API).

    We collapse those into a human-readable string + two booleans
    the renderer uses to decide which marker to print.
    """
    if handler is signal.SIG_DFL:
        return "default (process death)", True, False
    if handler is signal.SIG_IGN:
        return "ignored", False, True
    if handler is None:
        # CPython rarely returns this — only when the handler was
        # installed by C code outside Python's tracking. Surface it
        # so the operator knows the slot is touched but unreadable
        # from here, rather than confusing it with SIG_DFL.
        return "unknown (set outside Python)", False, False
    qualname = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", None)
    if qualname:
        module = getattr(handler, "__module__", None)
        if module and module != "builtins":
            return f"{module}.{qualname}", False, False
        return str(qualname), False, False
    # Fallback: a built-in or C-level callable without __qualname__.
    # ``repr`` is verbose but at least non-empty.
    return repr(handler), False, False


def _capture() -> list[_SignalRow]:
    """Sample the load-bearing signals' installed handlers.

    On a non-main thread :func:`signal.getsignal` raises
    :class:`ValueError`; an aiogram handler always runs on the main
    thread so the branch is unreachable in practice but we surface
    "unavailable" rather than crash if it ever fires (a future
    refactor that moves work to a worker thread would otherwise
    take this diagnostic down with it).
    """
    rows: list[_SignalRow] = []
    for name in _SIGNALS_OF_INTEREST:
        signum = getattr(signal, name, None)
        if signum is None:
            # SIGUSR1/SIGUSR2 don't exist on Windows. Tests + prod
            # are POSIX, so this is a defensive guard for the
            # cross-platform development case.
            rows.append(
                _SignalRow(
                    name=name,
                    handler_desc="unavailable on this platform",
                    is_default=False,
                    is_ignored=False,
                )
            )
            continue
        try:
            handler = signal.getsignal(signum)
        except ValueError:
            rows.append(
                _SignalRow(
                    name=name,
                    handler_desc="unavailable (non-main thread)",
                    is_default=False,
                    is_ignored=False,
                )
            )
            continue
        desc, is_default, is_ignored = _describe_handler(handler)
        rows.append(
            _SignalRow(
                name=name,
                handler_desc=desc,
                is_default=is_default,
                is_ignored=is_ignored,
            )
        )
    return rows


def _render(rows: list[_SignalRow]) -> str:
    lines = ["📡 <b>Signal handlers</b>", ""]
    for row in rows:
        # Markers carry the operationally-loaded interpretation.
        # SIG_IGN is the unambiguously-bad case (un-killable bot,
        # corruption risk on shutdown); SIG_DFL on SIGTERM is the
        # "you forgot to wire graceful shutdown" case — also a
        # warning, but a softer one (process dies cleanly, just no
        # in-flight cleanup). We render different glyphs so the
        # operator can triage by scanning.
        if row.is_ignored:
            marker = " ⚠ (un-killable; will require SIGKILL)"
        elif row.is_default and row.name in {"SIGINT", "SIGTERM"}:
            marker = " ⚠ (no graceful shutdown wired)"
        else:
            marker = ""
        lines.append(
            f"• <code>{html.escape(row.name)}</code> → "
            f"<code>{html.escape(row.handler_desc)}</code>{marker}"
        )
    lines.append("")
    lines.append(
        "<i>SIGINT/SIGTERM should resolve to a Python callable that "
        "drives graceful shutdown. SIG_DFL kills mid-write — risk "
        "of WAL corruption on a busy DB.</i>"
    )
    return "\n".join(lines)


async def handle_admin_signals(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_signals; silently dropped"
        )
        return
    rows = _capture()
    await message.answer(_render(rows))
    log.bind(user_id=user.id).info("/admin_signals rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.signals")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_signals(message, settings)

    router.message.register(_entry, Command("admin_signals", ignore_case=True))
    return router
