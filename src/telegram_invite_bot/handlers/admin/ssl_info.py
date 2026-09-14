"""``/admin_ssl`` — bundled OpenSSL + trust-store snapshot.

Complements /admin_modules (Python-package versions) with the
C-binding side of the security surface: which OpenSSL the
interpreter is linked against, and which CA bundle the default
verification path is reading.

Why an operator wants this:

* Post-deploy OpenSSL audit. A package update on the host bumps
  libssl; Python sees the new version via :data:`ssl.OPENSSL_VERSION`
  on next process start. The card surfaces the version so the
  operator can verify the deploy actually picked up the security
  update (apt-listchanges output ≠ "the running process is using
  it"). After CVEs land in OpenSSL this is the single line that
  proves the bot is no longer exposed.
* CA-bundle drift. ``ssl.get_default_verify_paths()`` returns the
  cafile + capath the stdlib will check. On a slim Docker image
  with ``ca-certificates`` not installed, both are unset and every
  outbound HTTPS call (Telegram API, Open-Meteo, every AI provider)
  silently falls back to bundled certs — which on aiohttp / httpx
  means "use certifi" and on raw ``ssl`` means "fail closed". The
  card surfaces the path so an operator can see "yep, slim image
  has no CA bundle" before chasing a hour of "why is httpx
  intermittently failing".
* FIPS-mode verification. ``ssl.HAS_TLSv1_2`` / ``HAS_TLSv1_3``
  flags pin the protocol surface; a FIPS build that disables
  TLS 1.3 surfaces here as the missing flag rather than as
  "handshake fails to some peers" three days later.

Reads :mod:`ssl` once. No outbound calls — the trust-store query
is a stdlib metadata read, not a verification attempt against a
remote peer. Same posture as every other ``/admin_*``: silent-drop
for non-devs, private-only at the router level.
"""

from __future__ import annotations

import html
import ssl
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.ssl")


class _SSLSnapshot:
    """One-shot bundled-OpenSSL + trust-store fingerprint.

    ``library_version`` is the runtime-linked OpenSSL identity
    (e.g. ``OpenSSL 3.0.13 30 Jan 2024``). ``compile_version`` is
    what the Python interpreter was *built* against —
    disagreement is rare but real on hosts where the runtime got
    upgraded without rebuilding Python.

    ``cafile`` / ``capath`` are the trust-store endpoints the
    default verification context uses. Either being empty on a
    production host is the "ca-certificates not installed" failure
    mode the module docstring describes.

    ``protocols`` is a stable list of which TLS versions are
    compiled in. Rendered explicitly because absence of TLS 1.3
    is the FIPS-build trap, and an operator scanning a list reads
    "TLSv1_3: no" much faster than parsing a flag tuple.
    """

    __slots__ = (
        "cafile",
        "capath",
        "compile_version",
        "library_version",
        "protocols",
    )

    def __init__(
        self,
        *,
        library_version: str,
        compile_version: str,
        cafile: str,
        capath: str,
        protocols: tuple[tuple[str, bool], ...],
    ) -> None:
        self.library_version = library_version
        self.compile_version = compile_version
        self.cafile = cafile
        self.capath = capath
        self.protocols = protocols


# TLS-version flags that matter operationally. We deliberately do NOT
# render the deprecated TLS 1.0 / 1.1 / SSL flags — those are
# compiled-out in modern OpenSSL and rendering them clutters the
# card without adding diagnostic signal. If a future incident makes
# the deprecated flags relevant, the operator can read them via
# the Python REPL ad-hoc.
_TLS_FLAGS: tuple[str, ...] = ("HAS_TLSv1_2", "HAS_TLSv1_3")


def _capture() -> _SSLSnapshot:
    """Sample the OpenSSL + trust-store identity once.

    ``get_default_verify_paths`` raises only on a bizarrely broken
    stdlib; we don't catch it because if THAT is broken every
    outbound HTTPS call is already failing and the card surfacing
    a stack trace is a stronger signal than "<unavailable>".
    """
    paths = ssl.get_default_verify_paths()
    protocols = tuple((flag, bool(getattr(ssl, flag, False))) for flag in _TLS_FLAGS)
    return _SSLSnapshot(
        library_version=ssl.OPENSSL_VERSION,
        # ``OPENSSL_VERSION_INFO`` is a 5-tuple; render as the
        # dotted form so the operator can eyeball a CVE-fix
        # version comparison without parsing the tuple.
        compile_version=".".join(str(part) for part in ssl.OPENSSL_VERSION_INFO[:3]),
        cafile=paths.cafile or "<unset>",
        capath=paths.capath or "<unset>",
        protocols=protocols,
    )


def _render(snap: _SSLSnapshot) -> str:
    lines = ["🔐 <b>OpenSSL + trust store</b>", ""]
    lines.append(f"<b>runtime:</b> <code>{html.escape(snap.library_version)}</code>")
    lines.append(f"<b>compiled against:</b> <code>{html.escape(snap.compile_version)}</code>")
    lines.append("")
    # The two trust-store endpoints both being unset is the
    # "ca-certificates missing" failure mode. Surface the warning
    # marker explicitly so an operator can't read the unset state
    # as "fine, the defaults work".
    cafile_unset = snap.cafile == "<unset>"
    capath_unset = snap.capath == "<unset>"
    lines.append(f"<b>cafile:</b> <code>{html.escape(snap.cafile)}</code>")
    lines.append(f"<b>capath:</b> <code>{html.escape(snap.capath)}</code>")
    if cafile_unset and capath_unset:
        lines.append(
            "  <i>⚠ both unset — system trust store unavailable; "
            "outbound HTTPS leans on certifi/bundled certs.</i>"
        )
    lines.append("")
    lines.append("<b>Protocols:</b>")
    # Render every protocol flag we sampled, in declaration order.
    # An operator scanning for "is TLS 1.3 on?" should not have to
    # learn which flag the renderer chose to surface — we surface
    # all of ``_TLS_FLAGS`` unconditionally.
    for flag, enabled in snap.protocols:
        # ``HAS_TLSv1_3`` → "TLSv1_3" for readability.
        label = flag.removeprefix("HAS_")
        marker = "yes" if enabled else "no"
        # TLS 1.3 missing on a non-FIPS prod host is the "OpenSSL
        # is ancient" signal. Soft warning rather than a hard
        # alarm — FIPS builds legitimately disable it.
        warn = " ⚠" if (not enabled and flag == "HAS_TLSv1_3") else ""
        lines.append(f"  • <code>{html.escape(label)}</code>: <code>{marker}</code>{warn}")
    return "\n".join(lines)


async def handle_admin_ssl(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_ssl; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_ssl rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.ssl")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_ssl(message, settings)

    router.message.register(_entry, Command("admin_ssl", ignore_case=True))
    return router
