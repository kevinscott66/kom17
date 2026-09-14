"""``/admin_resolver`` — DNS resolver configuration.

Surfaces ``/etc/resolv.conf`` (nameservers, search domains,
options) plus the ``hosts:`` line from ``/etc/nsswitch.conf``
(file vs DNS vs systemd-resolved lookup order). Complements
/admin_dns (which probes ``api.telegram.org`` end-to-end) with
the *configuration that drives those probes* — when /admin_dns
shows weird latency or a wrong A record, /admin_resolver is the
first place to look.

Why an operator wants this:

* "Why does my DNS lookup take 5 seconds?" Almost always a stale
  /etc/resolv.conf pointing at a nameserver that no longer
  answers, with a default ``timeout:5 attempts:2`` retry policy
  multiplying the wait. The card surfaces both the nameserver
  list and any explicit options that override the retry policy.
* ``/etc/resolv.conf`` is the one config file an SSH session can
  silently shadow: a Docker container's mount can override the
  host's, and a systemd-resolved stub at 127.0.0.53 can replace
  the real upstream — the file content matters per-namespace.
* The ``hosts:`` line from /etc/nsswitch.conf is the lookup-order
  switch — ``files dns`` vs ``files mdns4_minimal [NOTFOUND=return]
  dns`` vs ``files resolve [!UNAVAIL=return] dns`` is the
  difference between "/etc/hosts wins, then DNS" and "mDNS or
  systemd-resolved get a turn first". A surprise here is one of
  the harder bugs to track down because the resolver's behaviour
  diverges from naïve nslookup output.
* ``search`` domains affect short-name lookups; ``options
  ndots:N`` controls when those kick in. Both are footguns —
  ``ndots:5`` (the Docker default) means anything with fewer
  than 5 dots tries every search domain before falling through.

Cry-wolf posture: no ⚠ on absolute content (a stub resolver at
127.0.0.53 is the systemd default and not a problem). The single
⚠ is on a completely-empty nameserver list — that's the only
unambiguous misconfiguration this card can detect from a static
read. Anything more subtle (wrong nameserver, slow nameserver)
shows up in /admin_dns latency.

Linux-ish only (BSDs ship /etc/resolv.conf too but vary on
nsswitch). macOS lacks both files — render an informational
note in that case rather than ⚠.
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


log = logger.bind(component="handlers.admin.resolver")


_RESOLV_CONF = Path("/etc/resolv.conf")
_NSSWITCH_CONF = Path("/etc/nsswitch.conf")


class _ResolvSnapshot:
    """Parsed /etc/resolv.conf.

    ``status_present`` distinguishes "no resolv.conf on this host"
    (macOS dev, container with hostNetwork disabled) from "file
    exists but is empty/malformed".
    """

    __slots__ = ("nameservers", "options", "search", "status_present")

    def __init__(
        self,
        *,
        nameservers: tuple[str, ...],
        search: tuple[str, ...],
        options: tuple[str, ...],
        status_present: bool,
    ) -> None:
        self.nameservers = nameservers
        self.search = search
        self.options = options
        self.status_present = status_present


class _NsswitchSnapshot:
    """Parsed /etc/nsswitch.conf hosts: line.

    ``sources`` is the parsed lookup-order list. ``status_present``
    is False on hosts without the file (BSDs / containers).
    """

    __slots__ = ("sources", "status_present")

    def __init__(
        self,
        *,
        sources: tuple[str, ...],
        status_present: bool,
    ) -> None:
        self.sources = sources
        self.status_present = status_present


def _parse_resolv_conf(text: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Parse /etc/resolv.conf into (nameservers, search, options).

    Strips comments (``;`` and ``#`` per resolv.conf(5)), splits
    on whitespace. Multiple ``search`` lines append (the spec says
    last wins for ``domain`` but cumulative for ``search`` is what
    glibc actually does on most distros).
    """
    nameservers: list[str] = []
    search: list[str] = []
    options: list[str] = []
    for raw_line in text.splitlines():
        # Strip comments — either char per resolv.conf(5).
        line = raw_line
        for marker in ("#", ";"):
            idx = line.find(marker)
            if idx != -1:
                line = line[:idx]
        tokens = line.split()
        if not tokens:
            continue
        keyword, args = tokens[0].lower(), tokens[1:]
        if keyword == "nameserver" and args:
            nameservers.append(args[0])
        elif keyword in ("search", "domain"):
            # ``domain`` is the legacy single-arg form; treat as
            # search with one entry so the operator's mental model
            # stays uniform.
            search.extend(args)
        elif keyword == "options":
            options.extend(args)
    return tuple(nameservers), tuple(search), tuple(options)


def _parse_nsswitch_hosts(text: str) -> tuple[str, ...]:
    """Extract the ``hosts:`` line sources from nsswitch.conf.

    Format: ``hosts:    files dns mdns4_minimal [NOTFOUND=return]``.
    We split tokens and drop bracketed action specifiers so the
    rendered list is the human-readable lookup order. Multiple
    ``hosts:`` lines (rare but legal) — last one wins, matching
    glibc.
    """
    last_sources: tuple[str, ...] = ()
    for raw_line in text.splitlines():
        # Comments allowed.
        line = raw_line
        idx = line.find("#")
        if idx != -1:
            line = line[:idx]
        if not line.strip().lower().startswith("hosts:"):
            continue
        _, _, rest = line.partition(":")
        # Drop bracketed action specifiers like [NOTFOUND=return]
        # — they're semantically important but visually noisy and
        # the operator usually wants the source list first.
        tokens: list[str] = []
        for tok in rest.split():
            if tok.startswith("["):
                continue
            tokens.append(tok)
        last_sources = tuple(tokens)
    return last_sources


def _capture_resolv(*, path: Path = _RESOLV_CONF) -> _ResolvSnapshot:
    try:
        text = path.read_text()
    except OSError:
        return _ResolvSnapshot(
            nameservers=(),
            search=(),
            options=(),
            status_present=False,
        )
    nameservers, search, options = _parse_resolv_conf(text)
    return _ResolvSnapshot(
        nameservers=nameservers,
        search=search,
        options=options,
        status_present=True,
    )


def _capture_nsswitch(*, path: Path = _NSSWITCH_CONF) -> _NsswitchSnapshot:
    try:
        text = path.read_text()
    except OSError:
        return _NsswitchSnapshot(sources=(), status_present=False)
    return _NsswitchSnapshot(
        sources=_parse_nsswitch_hosts(text),
        status_present=True,
    )


def _empty_nameservers_concerning(resolv: _ResolvSnapshot) -> bool:
    """⚠ predicate. We ONLY cry on the unambiguous misconfig: a
    resolv.conf that exists but lists zero nameservers. Anything
    more subtle is /admin_dns territory."""
    return resolv.status_present and not resolv.nameservers


def _render(resolv: _ResolvSnapshot, nsswitch: _NsswitchSnapshot) -> str:
    lines = ["🧭 <b>DNS resolver configuration</b>", ""]

    lines.append("  <b>/etc/resolv.conf:</b>")
    if not resolv.status_present:
        lines.append(
            "    • <i>file not present — macOS host, BSD, or "
            "container without resolv.conf mount</i>"
        )
    else:
        if resolv.nameservers:
            for ns in resolv.nameservers:
                lines.append(f"    • nameserver: <code>{ns}</code>")
        else:
            lines.append("    • <i>no nameservers configured</i> ⚠")
        if resolv.search:
            lines.append(f"    • search: <code>{' '.join(resolv.search)}</code>")
        if resolv.options:
            lines.append(f"    • options: <code>{' '.join(resolv.options)}</code>")
        if not resolv.search and not resolv.options:
            lines.append("    • <i>(no search domains or options set — defaults apply)</i>")

    lines.append("")
    lines.append("  <b>/etc/nsswitch.conf hosts: line:</b>")
    if not nsswitch.status_present:
        lines.append(
            "    • <i>file not present — macOS host or BSD; lookup order is libc-default</i>"
        )
    elif not nsswitch.sources:
        lines.append("    • <i>hosts: line not found — glibc fallback to files+dns applies</i>")
    else:
        lines.append(
            "    • sources (in order): " + " → ".join(f"<code>{s}</code>" for s in nsswitch.sources)
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: only emitted when "
        "<code>/etc/resolv.conf</code> exists but lists zero "
        "nameservers — the unambiguous misconfig this card can "
        "detect statically. Wrong-but-present nameservers and "
        "slow-but-present nameservers surface as latency in "
        "/admin_dns instead.</i>"
    )
    return "\n".join(lines)


async def handle_admin_resolver(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_resolver; silently dropped"
        )
        return
    resolv = _capture_resolv()
    nsswitch = _capture_nsswitch()
    await message.answer(_render(resolv, nsswitch))
    log.bind(
        user_id=user.id,
        resolv_present=resolv.status_present,
        nameserver_count=len(resolv.nameservers),
        search_count=len(resolv.search),
        nsswitch_present=nsswitch.status_present,
        empty_nameservers=_empty_nameservers_concerning(resolv),
    ).info("/admin_resolver rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.resolver")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_resolver(message, settings)

    router.message.register(_entry, Command("admin_resolver", ignore_case=True))
    return router
