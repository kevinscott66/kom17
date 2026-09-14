"""``/admin_python`` — interpreter identity snapshot.

Complements /admin_modules (dep versions) and /admin_uptime (process
boot time) by surfacing the **interpreter itself**: version,
implementation, executable path, prefix. The diagnostic gap this
closes is the "wrong interpreter on the host" failure mode that
/admin_modules can't catch — both surfaces look identical when
the deploy somehow ran ``pip install`` into one venv but the
systemd unit boots from a different one. The card answers
"WHICH Python is this process?" in a single screen.

Why an operator wants this:

* Deploy parity. ``python3.12 -m telegram_invite_bot`` on the
  staging host vs the prod host should print identical X.Y.Z and
  prefix paths. Drift here predicts dep-resolution differences
  /admin_modules would surface later.
* Version-gated dep regression. A patch bump to CPython
  (3.12.3 → 3.12.4) can introduce a real behaviour change (the
  ``ssl`` module's certificate handling, asyncio task internals).
  When a bug correlates with "we restarted Tuesday" this card
  pins down whether the *interpreter* changed under the deploy.
* Verify a venv switch. After moving from system Python to a
  pinned venv (or vice versa), :data:`sys.prefix` is the
  ground-truth read for which interpreter is actually executing
  the imports.

Pure stdlib (``sys`` + ``platform``), no IO, no DB. Same posture
as every other ``/admin_*``: silent-drop for non-devs, private-only
at the router level (executable paths can encode usernames and
deploy-host layout).
"""

from __future__ import annotations

import platform
import sys
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.python")


class _PythonSnapshot:
    """One read of the interpreter's identity.

    Captured all at once because — unlike clocks — these fields
    don't change at runtime, but we still keep the read in one
    helper so the test path can substitute a snapshot without
    monkeypatching ``sys`` itself.
    """

    __slots__ = (
        "executable",
        "impl_name",
        "platform_str",
        "prefix",
        "version",
    )

    def __init__(
        self,
        *,
        version: str,
        impl_name: str,
        executable: str,
        prefix: str,
        platform_str: str,
    ) -> None:
        self.version = version
        self.impl_name = impl_name
        self.executable = executable
        self.prefix = prefix
        self.platform_str = platform_str


def _capture() -> _PythonSnapshot:
    """Snapshot interpreter identity.

    ``sys.version`` carries the build/compiler suffix which is
    useful for spotting an unexpected reinstall (a "3.12.3
    (main, Apr 9 2024, gcc 11.4.0)" vs the same X.Y.Z built with
    clang signals a different package source). ``platform.python_version``
    would strip that, so we keep the full ``sys.version`` and let
    the renderer trim if needed.
    """
    return _PythonSnapshot(
        version=sys.version,
        impl_name=platform.python_implementation(),
        executable=sys.executable,
        prefix=sys.prefix,
        platform_str=platform.platform(),
    )


def _render(snap: _PythonSnapshot) -> str:
    lines = ["🐍 <b>Python interpreter</b>", ""]
    # First line of sys.version is the X.Y.Z + build info; later
    # lines are the compiler banner that an operator skimming
    # doesn't need on first read.
    version_first_line = snap.version.splitlines()[0] if snap.version else ""
    lines.append(f"• Version: <code>{version_first_line}</code>")
    lines.append(f"• Implementation: <code>{snap.impl_name}</code>")
    lines.append(f"• Executable: <code>{snap.executable}</code>")
    lines.append(f"• Prefix: <code>{snap.prefix}</code>")
    lines.append(f"• Platform: <code>{snap.platform_str}</code>")
    lines.append("")
    lines.append(
        "<i>Use to verify the interpreter under this process matches "
        "the one your deploy installed deps into — /admin_modules "
        "can't catch a venv mismatch that resolves to a different "
        "site-packages tree.</i>"
    )
    return "\n".join(lines)


async def handle_admin_python(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_python; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_python rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.python")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_python(message, settings)

    router.message.register(_entry, Command("admin_python", ignore_case=True))
    return router
