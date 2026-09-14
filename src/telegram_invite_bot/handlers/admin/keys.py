"""``/admin_keys`` — kernel keyring visible to this process from /proc/keys.

Linux has a kernel-resident key management subsystem (``man 7
keyrings``) — separate from the userspace crypto (/admin_certfp,
/admin_ssl, /admin_hashlib) and separate from the kernel
crypto-algorithm registry (/admin_crypto). It holds:

* **dm-crypt / fscrypt master keys** — LUKS unlock keys live
  here while volumes are open. A volume with an evicted /
  revoked key is one ``cryptsetup luksClose`` away from being
  inaccessible.
* **NFSv4 / Kerberos ticket caches.**
* **DNS-resolver cached keys** (``dns_resolver`` type) — used
  by in-kernel callers like CIFS / NFS / AFS.
* **Container runtime keys** (Docker / podman registry creds
  for the host-side image-pull paths).

What /proc/keys shows is the keyring visible *to the calling
process* — i.e. our bot's session / user / persistent keyring
hierarchy. On a non-root daemon with a minimal session this is
often empty or near-empty, and that's not a bug — it's exactly
what `man 7 keyrings` describes.

Format per row (kernel ``security/keys/proc.c`` ``proc_keys_show``)::

    0a2b3c4d I--Q---     1 perm     0     0 user      docker_hub: 21
    1b3c4d5e I------    25  3w      0     0 keyring   _ses: 1

Fields: id, flags, usage, timeout, perm, uid, gid, type,
``description: payload_size``. The flags column is the
operationally-critical one — single chars at fixed positions:

* ``I`` instantiated (normal)
* ``R`` revoked  ← ⚠ predicate
* ``D`` dead
* ``Q`` quota (counts against the user's keyring quota)
* ``U`` under-construction
* ``N`` negative (lookup miss cache)
* ``i`` invalidated

The ``timeout`` column tells us if the key expired in place but
wasn't garbage-collected — ``expd`` literal. ⚠ predicate
combines: any key with flags containing ``R`` (revoked) OR
timeout == ``expd``.

⚠ predicate: one. revoked-or-expired count > 0. Pinned with
must-not-fire test on healthy-keyring sample.

Same wiring as every other admin card — silent-drop, private-only,
pure stdlib, hermetic via keyword-only path injection.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.keys")


_KEYS_PATH = Path("/proc/keys")


class _Key:
    """One kernel-keyring entry.

    We precompute the two operational booleans (``revoked``,
    ``expired``) so render and predicate evaluation don't
    re-parse the flags column. ``flags`` itself is kept
    verbatim beside them and printed on the card — forward-compat
    if the kernel adds a flag letter this parser doesn't know,
    the operator still sees it (#1596: this paragraph used to
    promise a ``raw`` field that never existed)."""

    __slots__ = (
        "description",
        "expired",
        "flags",
        "id",
        "revoked",
        "timeout",
        "type",
    )

    def __init__(
        self,
        *,
        id: str,
        flags: str,
        timeout: str,
        type: str,
        description: str,
    ) -> None:
        self.id = id
        self.flags = flags
        self.timeout = timeout
        self.type = type
        self.description = description
        self.revoked = "R" in flags or "D" in flags or "i" in flags
        self.expired = timeout == "expd"


class _KeysSnapshot:
    """Captured /proc/keys.

    ``keys`` — every visible entry.
    ``available`` — False on macOS / non-procfs / EACCES.
    """

    __slots__ = ("available", "keys")

    def __init__(self, *, keys: tuple[_Key, ...], available: bool) -> None:
        self.keys = keys
        self.available = available

    @property
    def unhealthy_count(self) -> int:
        return sum(1 for k in self.keys if k.revoked or k.expired)


def _parse_keys(text: str) -> tuple[_Key, ...]:
    """Parse /proc/keys.

    The first 8 whitespace-separated tokens are id, flags,
    usage, timeout, perm, uid, gid, type. Everything after is
    the description (which itself ends with ``: payload_size``).
    We split with maxsplit=8 to preserve the description's
    internal whitespace. Lines with fewer than 9 fields drop
    defensively — a partial / corrupt read shouldn't synthesise
    a key with empty type and break the type-based filtering an
    operator does mentally.
    """
    keys: list[_Key] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Peel off the first 8 fields (id, flags, usage, timeout,
        # perm, uid, gid, type) with maxsplit=8 → 9 parts. The
        # last part is the describe()-callback output, which has
        # the form "<description>: <size>" and may contain
        # internal whitespace — preserved by split's remainder
        # semantics.
        parts = line.split(maxsplit=8)
        if len(parts) != 9:
            continue
        key_id, flags, _usage, timeout, _perm, _uid, _gid, type_, description = parts
        keys.append(
            _Key(
                id=key_id,
                flags=flags,
                timeout=timeout,
                type=type_,
                description=description,
            )
        )
    return tuple(keys)


def _capture(*, path: Path = _KEYS_PATH) -> _KeysSnapshot:
    """Read /proc/keys + build snapshot.

    PermissionError (EACCES) is treated as ``available=False``,
    not as "no keys" — surfacing zero keys when we couldn't read
    would be a worse outcome than admitting the read failed.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _KeysSnapshot(keys=(), available=False)
    return _KeysSnapshot(keys=_parse_keys(text), available=True)


def _render(snap: _KeysSnapshot) -> str:
    lines = ["🔑 <b>Kernel keyring (/proc/keys)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/keys unavailable — Linux-only surface "
            "or the bot lacks read permission on this kernel's "
            "keyring (EACCES is normal for unprivileged daemons "
            "outside their own session keyring).</i>"
        )
        return "\n".join(lines)

    if not snap.keys:
        lines.append(
            "  <i>Empty keyring visible — bot's session / user "
            "keyring has no entries. Normal for a minimal daemon "
            "that doesn't use dm-crypt, NFSv4, or in-kernel DNS "
            "resolver caching.</i>"
        )
        return "\n".join(lines)

    # Aggregate by type — operator's mental model groups keys by
    # subsystem (user vs keyring vs dns_resolver vs encrypted).
    by_type: dict[str, int] = {}
    for k in snap.keys:
        by_type[k.type] = by_type.get(k.type, 0) + 1

    unhealthy = [k for k in snap.keys if k.revoked or k.expired]
    warn_marker = " ⚠" if unhealthy else ""

    lines.append(
        f"  <b>Visible keys:</b> <code>{len(snap.keys)}</code>  "
        f"<b>revoked/expired:</b> <code>{len(unhealthy)}</code>"
        f"{warn_marker}"
    )
    lines.append("")
    lines.append("  <b>By type:</b>")
    # Parse mode is HTML and every string below comes out of
    # /proc/keys, so all of it goes through html.escape (#1596).
    # The description column is the load-bearing one: it is the
    # output of each key type's describe() callback, and for a
    # ``user`` key that is whatever the process which created the
    # key passed in. The other columns are whitespace-delimited
    # tokens that have never carried markup, but they come off the
    # same line and are escaped with it rather than left as a
    # per-column judgement call the next reader has to re-derive.
    for type_name, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"    • <code>{html.escape(type_name)}</code>: <code>{count}</code>")

    if unhealthy:
        lines.append("")
        lines.append("  <b>Revoked / expired:</b>")
        for key in unhealthy[:20]:
            state = "revoked" if key.revoked else "expired"
            # Escaped for the same reason as the by-type rows (#1596).
            lines.append(
                f"    • <code>{html.escape(key.id)}</code> "
                f"(<code>{html.escape(key.type)}</code>, "
                f"flags=<code>{html.escape(key.flags)}</code>, "
                f"timeout=<code>{html.escape(key.timeout)}</code>) — {state}: "
                f"<code>{html.escape(key.description)}</code>"
            )
        if len(unhealthy) > 20:
            lines.append(f"    <i>… {len(unhealthy) - 20} more.</i>")

    lines.append("")
    if unhealthy:
        lines.append(
            "<i>⚠ Revoked / expired keys remain visible in the keyring "
            "until garbage-collected. Most common cause: a dm-crypt "
            "LUKS key revoked but volume still mounted (next "
            "<code>luksClose</code> will fail), or an NFSv4 / Kerberos "
            "ticket cache that didn't roll over cleanly. Use "
            "<code>keyctl unlink &lt;id&gt; &lt;keyring&gt;</code> to "
            "remove specific entries.</i>"
        )
    else:
        lines.append(
            "<i>No warnings — every visible key is instantiated and "
            "unexpired. The keyring's contents themselves don't have "
            "a 'right answer' across hosts (dm-crypt vs NFS vs "
            "Kerberos vs container runtime all populate different "
            "subsets).</i>"
        )
    return "\n".join(lines)


async def handle_admin_keys(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_keys; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        key_count=len(snap.keys),
        unhealthy=snap.unhealthy_count,
    ).info("/admin_keys rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.keys")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_keys(message, settings)

    router.message.register(_entry, Command("admin_keys", ignore_case=True))
    return router
