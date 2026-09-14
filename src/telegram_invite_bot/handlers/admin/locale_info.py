"""``/admin_locale`` — locale + stream-encoding snapshot.

The bot serves Russian + English (and assorted Telegram emojis the
users pass through). Every text it writes — log lines, file paths,
DB inserts, outbound API calls — goes through the interpreter's
locale and the per-stream encoding of stdout/stderr. A process
started without a UTF-8 locale is a real prod trap: log lines
containing Cyrillic crash on a ``write`` with ``UnicodeEncodeError``,
and the bot keeps serving until the next operator-facing log call
trips the same path and kills the worker.

Why an operator wants this:

* Post-deploy charset audit. A new container running with
  ``LANG=C`` (the Docker-image default if the operator didn't set
  ``ENV LANG``) silently breaks every Russian log line. The card
  surfaces the locale so the regression is visible *before* the
  first Russian-language error blows up.
* "Why does this print render as ``?????``" — stdout's
  :attr:`encoding` tells the operator what the stream is willing
  to encode without the ``errors`` handler kicking in. ASCII
  stdout on a host serving Russian users is the most common
  underlying cause; the card pins both stdout and stderr because
  a logging handler can target either.
* :func:`sys.getfilesystemencoding` — the encoding the
  interpreter uses for filesystem paths. On Linux this is
  derived from the locale; on macOS it's always utf-8. A
  mismatch between fs-encoding and the locale is the root cause
  of "file with Russian name in path can't be opened" classes
  of bugs the bot hits on user-supplied avatar uploads.

Reads :mod:`locale`, :mod:`sys`. No outbound calls, no allocator
pressure beyond the call frame. Same posture as every other
``/admin_*``: silent-drop for non-devs, private-only at the router
level.
"""

from __future__ import annotations

import html
import locale
import os
import sys
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.locale")


class _LocaleSnapshot:
    """One-shot locale + stream-encoding bundle.

    ``lc_all`` is the global :func:`locale.setlocale` query result
    — the umbrella value that wins over per-category overrides.
    ``preferred`` is :func:`locale.getpreferredencoding` — the
    encoding the stdlib uses for newly-opened text files; this is
    the one most operationally consequential to get wrong.

    ``stdout_enc`` / ``stderr_enc`` are the per-stream encodings.
    A logging handler that targets stderr inherits this; ASCII
    stderr on a host serving Cyrillic is the single most common
    cause of ``UnicodeEncodeError`` traces in this codebase's
    history.
    """

    __slots__ = (
        "fs_encoding",
        "lang_env",
        "lc_all",
        "preferred",
        "stderr_enc",
        "stdout_enc",
    )

    def __init__(
        self,
        *,
        lc_all: str,
        preferred: str,
        fs_encoding: str,
        stdout_enc: str,
        stderr_enc: str,
        lang_env: str,
    ) -> None:
        self.lc_all = lc_all
        self.preferred = preferred
        self.fs_encoding = fs_encoding
        self.stdout_enc = stdout_enc
        self.stderr_enc = stderr_enc
        self.lang_env = lang_env


def _capture() -> _LocaleSnapshot:
    """Sample locale + stream encodings once.

    ``locale.setlocale(LC_ALL, None)`` reads the current value
    without mutating it — the ``None`` argument is the documented
    read-only form. Calling without the ``None`` would set the
    locale, which is exactly what we must NOT do inside a
    diagnostic handler.
    """
    # ``setlocale(LC_ALL, None)`` returns the current LC_ALL string
    # (e.g. ``en_US.UTF-8`` or ``C``). Read-only call.
    lc_all = locale.setlocale(locale.LC_ALL, None) or "<unset>"
    return _LocaleSnapshot(
        lc_all=lc_all,
        preferred=locale.getpreferredencoding(False),
        fs_encoding=sys.getfilesystemencoding(),
        # ``encoding`` is set on every TextIOWrapper at construction
        # time. A non-text stream (rare in our setup) doesn't have
        # one — surface as "<binary>" rather than crash.
        stdout_enc=getattr(sys.stdout, "encoding", None) or "<binary>",
        stderr_enc=getattr(sys.stderr, "encoding", None) or "<binary>",
        # ``LANG`` is the most common locale-determining env var.
        # Render it raw so the operator can match the locale to
        # what the container was actually started with — the
        # interpreter's effective value can be massaged by
        # ``locale.setlocale`` later, so seeing the raw env helps
        # bisect "did the env arrive correctly?" vs "did Python
        # interpret it correctly?".
        lang_env=os.environ.get("LANG") or "<unset>",
    )


def _is_utf8(value: str) -> bool:
    """Lower-cased substring match. The ecosystem spells the
    encoding inconsistently: ``utf-8``, ``UTF-8``, ``utf8``,
    ``UTF8`` all mean the same thing, and matching too narrowly
    would flag a healthy host. Substring on ``utf`` catches them
    all without false positives — no other encoding names contain
    ``utf``."""
    return "utf" in value.lower()


def _render(snap: _LocaleSnapshot) -> str:
    lines = ["🔤 <b>Locale + encodings</b>", ""]
    lines.append(f"<b>LC_ALL:</b> <code>{html.escape(snap.lc_all)}</code>")
    lines.append(f"<b>LANG env:</b> <code>{html.escape(snap.lang_env)}</code>")
    lines.append("")
    # The four encoding rows are the load-bearing diagnostic. Each
    # gets the marker computed independently — a single global "is
    # everything utf-8?" check would lose the per-stream signal
    # that lets an operator triage "stdout broken, stderr fine"
    # vs "the whole interpreter is in C locale".
    for label, value in (
        ("preferred", snap.preferred),
        ("filesystem", snap.fs_encoding),
        ("stdout", snap.stdout_enc),
        ("stderr", snap.stderr_enc),
    ):
        marker = "" if _is_utf8(value) else " ⚠ (non-UTF-8)"
        lines.append(f"<b>{label}:</b> <code>{html.escape(value)}</code>{marker}")
    lines.append("")
    lines.append(
        "<i>Non-UTF-8 on any row means a Cyrillic log line, file "
        "path, or DB string will raise UnicodeEncodeError when "
        "it touches that surface. Container should set "
        "<code>LANG=C.UTF-8</code> or <code>LANG=en_US.UTF-8</code>"
        ".</i>"
    )
    return "\n".join(lines)


async def handle_admin_locale(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_locale; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_locale rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.locale")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_locale(message, settings)

    router.message.register(_entry, Command("admin_locale", ignore_case=True))
    return router
