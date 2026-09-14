"""``/admin_codecs`` — codec registry + default-encoding posture.

Complements /admin_locale (LC_*/LANG environment view) and
/admin_hashlib (cryptographic-algorithm availability) by surfacing
the **text-codec** side of the runtime: which encoding the process
defaults to for str ↔ bytes conversions, what the std-stream
encodings are, and whether the load-bearing codecs are actually
registered.

Why an operator wants this:

* Mojibake diagnosis. The classic "I sent a Russian name and it
  came back as ?????" symptom is almost always a stream-encoding
  mismatch — stdin/stdout encoding set to ``ascii`` instead of
  ``utf-8`` (a misconfigured systemd unit without ``LC_ALL=C.UTF-8``
  is the typical cause). This card surfaces the four encoding
  axes in one place: default str encoding, filesystem encoding,
  stdin/stdout/stderr encoding, locale preferred.
* IDN / punycode failures. ``api.telegram.org`` resolves fine, but
  the moment a non-ASCII hostname enters the pipeline (a custom
  webhook URL with a Cyrillic TLD, say), missing ``idna`` /
  ``punycode`` codecs surface as cryptic encode errors. We probe
  ``codecs.lookup`` on the load-bearing codecs so the operator
  catches a stripped-down docker image before the runtime does.
* JSON / Telegram-API parity. The aiogram → aiohttp → JSON path
  assumes UTF-8 throughout. A default encoding of anything else
  is a smoking gun for "why are emoji breaking".

Posture: silent-drop for non-devs, private-only at the router
level. Cheap — every value is a stdlib attribute or a
``codecs.lookup`` call (constant-time cache hit on registered
codecs).
"""

from __future__ import annotations

import codecs
import locale
import sys
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.codecs")


# The default-encoding axes we expect to all be UTF-8 on a healthy
# Linux deployment. Anything else on any axis is a smoking gun for
# mojibake bugs (Cyrillic / emoji in user-supplied names is the
# usual trigger). The expected value is conservative — "utf-8" is
# the only acceptable answer on the systemd-managed prod host.
_EXPECTED_ENCODING = "utf-8"


# Codecs the bot pipeline depends on. ``utf-8`` is the default; the
# others are needed when a non-ASCII string enters the API path
# (idna/punycode for hostnames, latin-1 as HTTP-header fallback,
# utf-16 / utf-32 for occasional Telegram interop edge cases).
# Missing any of these from a slim docker image is the kind of
# silent regression this card catches.
_LOAD_BEARING_CODECS: tuple[str, ...] = (
    "utf-8",
    "ascii",
    "latin-1",
    "utf-16",
    "utf-32",
    "idna",
    "punycode",
)


class _CodecSnapshot:
    """Captured codec / stream-encoding posture.

    Plain dataclass-by-hand (mirrors the rest of admin/* — keeps
    every snapshot in one consistent style so the diagnostic cards
    are uniform from an operator's point of view).
    """

    __slots__ = (
        "default_encoding",
        "filesystem_encoding",
        "locale_preferred",
        "missing_codecs",
        "stderr_encoding",
        "stdin_encoding",
        "stdout_encoding",
    )

    def __init__(
        self,
        *,
        default_encoding: str,
        filesystem_encoding: str,
        stdin_encoding: str | None,
        stdout_encoding: str | None,
        stderr_encoding: str | None,
        locale_preferred: str,
        missing_codecs: list[str],
    ) -> None:
        self.default_encoding = default_encoding
        self.filesystem_encoding = filesystem_encoding
        self.stdin_encoding = stdin_encoding
        self.stdout_encoding = stdout_encoding
        self.stderr_encoding = stderr_encoding
        self.locale_preferred = locale_preferred
        self.missing_codecs = missing_codecs


def _probe_missing_codecs(
    names: tuple[str, ...] = _LOAD_BEARING_CODECS,
) -> list[str]:
    """Return names from ``names`` that ``codecs.lookup`` cannot find.

    Pure function of the input (the codecs registry is a process-
    global, but for the purposes of this probe we treat it as a
    read-only oracle). Used directly by ``_capture`` and exposed
    for tests.
    """
    missing: list[str] = []
    for name in names:
        try:
            codecs.lookup(name)
        except LookupError:
            missing.append(name)
    return missing


def _normalize(name: str | None) -> str:
    """Lower-case + ``None``-safe normalization for case-insensitive
    encoding comparison.

    Python reports ``utf-8`` / ``UTF-8`` / ``utf8`` depending on the
    OS and Python version; the codec registry treats them as the
    same alias. We normalize for display + comparison so the ⚠
    logic doesn't fire on cosmetic capitalization differences.
    """
    if name is None:
        return "unknown"
    return name.lower().replace("_", "-")


def _capture() -> _CodecSnapshot:
    return _CodecSnapshot(
        default_encoding=_normalize(sys.getdefaultencoding()),
        filesystem_encoding=_normalize(sys.getfilesystemencoding()),
        stdin_encoding=_normalize(getattr(sys.stdin, "encoding", None)),
        stdout_encoding=_normalize(getattr(sys.stdout, "encoding", None)),
        stderr_encoding=_normalize(getattr(sys.stderr, "encoding", None)),
        # ``getpreferredencoding(False)`` skips the locale-setlocale
        # side effect — important because we don't want a diagnostic
        # to mutate process state.
        locale_preferred=_normalize(locale.getpreferredencoding(False)),
        missing_codecs=_probe_missing_codecs(),
    )


def _is_utf8(value: str) -> bool:
    """Treat all common UTF-8 aliases as the same value.

    ``utf-8`` is canonical, ``utf8`` is the alias the codec registry
    accepts but some platforms report. Both are fine — only a
    genuinely non-UTF-8 value (``ascii``, ``latin-1``, ``cp1252``)
    should ⚠.
    """
    return value in ("utf-8", "utf8")


def _render(snap: _CodecSnapshot) -> str:
    lines = ["🔤 <b>Codec registry</b>", ""]

    lines.append("  <b>default encodings:</b>")
    for label, value in (
        ("sys.getdefaultencoding", snap.default_encoding),
        ("sys.getfilesystemencoding", snap.filesystem_encoding),
        ("stdin.encoding", snap.stdin_encoding or "unknown"),
        ("stdout.encoding", snap.stdout_encoding or "unknown"),
        ("stderr.encoding", snap.stderr_encoding or "unknown"),
        ("locale.getpreferredencoding", snap.locale_preferred),
    ):
        warn = "" if _is_utf8(value) else " ⚠"
        lines.append(f"    • <b>{label}:</b> <code>{value}</code>{warn}")

    lines.append("")
    if snap.missing_codecs:
        lines.append(f"  <b>load-bearing codecs missing ({len(snap.missing_codecs)}):</b>")
        for name in snap.missing_codecs:
            lines.append(f"    • <code>{name}</code> ⚠")
    else:
        lines.append("  <b>load-bearing codecs:</b> <i>all present</i>")

    lines.append("")
    lines.append(
        f"<i>⚠ markers: encoding != {_EXPECTED_ENCODING} (mojibake risk "
        f"— typical cause is a systemd unit missing "
        f"<code>LC_ALL=C.UTF-8</code>; cross-check /admin_locale + "
        f"/admin_envscan), or a load-bearing codec is unregistered "
        f"(typical cause is a stripped-down docker image — the codec "
        f"module exists in CPython core but the registration is via "
        f"<code>encodings/__init__.py</code> which a build pruner can "
        f"omit).</i>"
    )
    return "\n".join(lines)


async def handle_admin_codecs(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_codecs; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        default_encoding=snap.default_encoding,
        missing_codecs=snap.missing_codecs,
    ).info("/admin_codecs rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.codecs")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_codecs(message, settings)

    router.message.register(_entry, Command("admin_codecs", ignore_case=True))
    return router
