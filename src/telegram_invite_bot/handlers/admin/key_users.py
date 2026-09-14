"""``/admin_key_users`` — per-uid kernel keyring quota usage.

Companion to /admin_keys (Stage 128). /admin_keys lists the
visible-to-this-process keys themselves (id, type, flags,
permissions, description). What it doesn't surface is the
**per-uid quota** the kernel enforces against keyring use:
each uid has a kernel-imposed budget of (a) number of keys
and (b) total payload bytes, and crossing those caps causes
``EDQUOT`` from ``add_key(2)`` / ``keyctl(2)`` — which inside a
Telegram bot would manifest as TLS / session / kerberos /
fscrypt operations failing at the syscall boundary, hours
before anything in our application logs noticed.

/proc/key-users format (one line per uid that has ever held a
key)::

       0:     7 6/6 4/200 51/20000
    1000:     2 2/2 2/200 18/20000

Columns:

1. ``uid:`` — owning uid, trailing colon.
2. ``usage`` — refcount on the per-uid user_struct (informational).
3. ``nkeys/nikeys`` — total keys / "instantiated" keys. The
   difference is unfilled keys (request_key in-flight). We
   surface ``nkeys`` only — nikeys is a kernel-internal
   instantiation lag, not an operational lever.
4. ``qnkeys/maxkeys`` — quota *used* / quota *cap* for key count.
   The single ⚠ predicate fires when this ratio is high.
5. ``qnbytes/maxbytes`` — same for payload bytes. Second part
   of the ⚠ predicate.

⚠ predicate: any uid with ``qnkeys/maxkeys >= _QUOTA_WARN_RATIO``
OR ``qnbytes/maxbytes >= _QUOTA_WARN_RATIO``. Ratio-based
because the absolute defaults (``maxkeys=200``, ``maxbytes=
20000``) differ between kernels and can be raised via
``/proc/sys/kernel/keys/{maxkeys,maxbytes}``; a fixed-count
threshold would be wrong on either tuned-up or tuned-down
hosts. ``0.8`` chosen as the squeeze warning level — same
shape as disk-usage / fd / inode percentages.

Pinned with must-not-fire test on the canonical-healthy sample
(every uid well below quota).

The kernel only emits a /proc/key-users line for uids that have
ever held a key. On an empty container we'd legitimately see
just uid 0. Render is fine with that — the card is "list of uids
the kernel is accounting against keyring quota," not "every uid
on the box."

Same wiring as every other admin card — silent-drop, private-only,
pure stdlib, hermetic via keyword-only path injection.
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


log = logger.bind(component="handlers.admin.key_users")


_KEY_USERS_PATH = Path("/proc/key-users")


# Ratio of (used / cap) at which we surface ⚠. 0.8 matches the
# operator-intuitive "80% full" threshold used for disk / fd /
# inode warnings elsewhere — close enough to act, far enough
# from the cliff that the operator has a window to raise
# kernel.keys.maxkeys or audit who's leaking keys.
_QUOTA_WARN_RATIO = 0.8


class _KeyUserRow:
    """One parsed /proc/key-users line.

    All fields stored as ints. The ⚠ predicate is a property
    so render and tests can ask the same question without
    duplicating the ratio math.
    """

    __slots__ = ("maxbytes", "maxkeys", "nkeys", "qnbytes", "qnkeys", "uid", "usage")

    def __init__(
        self,
        *,
        uid: int,
        usage: int,
        nkeys: int,
        qnkeys: int,
        maxkeys: int,
        qnbytes: int,
        maxbytes: int,
    ) -> None:
        self.uid = uid
        self.usage = usage
        self.nkeys = nkeys
        self.qnkeys = qnkeys
        self.maxkeys = maxkeys
        self.qnbytes = qnbytes
        self.maxbytes = maxbytes

    @property
    def keys_ratio(self) -> float:
        # Defensive: a kernel emitting maxkeys=0 would be exotic
        # but division-by-zero would crash render. Treat as
        # "infinitely tight quota" — anything above zero used is
        # over the threshold. We return 1.0 (full) rather than
        # inf so the > _QUOTA_WARN_RATIO comparison stays sane.
        if self.maxkeys <= 0:
            return 1.0 if self.qnkeys > 0 else 0.0
        return self.qnkeys / self.maxkeys

    @property
    def bytes_ratio(self) -> float:
        if self.maxbytes <= 0:
            return 1.0 if self.qnbytes > 0 else 0.0
        return self.qnbytes / self.maxbytes

    @property
    def over_quota_warn(self) -> bool:
        return self.keys_ratio >= _QUOTA_WARN_RATIO or self.bytes_ratio >= _QUOTA_WARN_RATIO


class _KeyUsersSnapshot:
    __slots__ = ("available", "rows")

    def __init__(self, *, rows: list[_KeyUserRow], available: bool) -> None:
        self.rows = rows
        self.available = available

    @property
    def warning_rows(self) -> list[_KeyUserRow]:
        return [r for r in self.rows if r.over_quota_warn]


def _parse_pair(token: str) -> tuple[int, int] | None:
    """Parse ``a/b`` → (a, b); return None on any malformation.
    Kept tiny because the same shape appears in two columns and
    inlining it twice would obscure the "drop the line on any
    field failure" rule below.
    """
    if "/" not in token:
        return None
    left, _, right = token.partition("/")
    try:
        return int(left), int(right)
    except ValueError:
        return None


def _parse_key_users(text: str) -> list[_KeyUserRow]:
    """Parse /proc/key-users. Five space-separated tokens per
    line: ``uid:`` ``usage`` ``nkeys/nikeys`` ``qnkeys/maxkeys``
    ``qnbytes/maxbytes``. Drop lines that don't match — the
    file format is stable since 2.6, but defensive parsing
    means a future column addition shouldn't crash render.
    """
    rows: list[_KeyUserRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        uid_token = parts[0]
        if not uid_token.endswith(":"):
            continue
        try:
            uid = int(uid_token[:-1])
            usage = int(parts[1])
        except ValueError:
            continue
        nkeys_pair = _parse_pair(parts[2])
        qkeys_pair = _parse_pair(parts[3])
        qbytes_pair = _parse_pair(parts[4])
        if nkeys_pair is None or qkeys_pair is None or qbytes_pair is None:
            continue
        nkeys, _nikeys = nkeys_pair
        qnkeys, maxkeys = qkeys_pair
        qnbytes, maxbytes = qbytes_pair
        rows.append(
            _KeyUserRow(
                uid=uid,
                usage=usage,
                nkeys=nkeys,
                qnkeys=qnkeys,
                maxkeys=maxkeys,
                qnbytes=qnbytes,
                maxbytes=maxbytes,
            )
        )
    return rows


def _capture(*, path: Path = _KEY_USERS_PATH) -> _KeyUsersSnapshot:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _KeyUsersSnapshot(rows=[], available=False)
    return _KeyUsersSnapshot(rows=_parse_key_users(text), available=True)


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.0f}%"


def _render(snap: _KeyUsersSnapshot) -> str:
    lines = ["🔐 <b>Per-uid keyring quota (/proc/key-users)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/key-users unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append(
            "  <i>No uids accounted — empty container or no keyring "
            "operations have happened on this host yet.</i>"
        )
        return "\n".join(lines)

    warning_rows = snap.warning_rows
    warn = bool(warning_rows)

    lines.append(f"  <b>Accounted uids:</b> <code>{len(snap.rows)}</code>")
    lines.append("")
    for r in snap.rows:
        marker = " ⚠" if r.over_quota_warn else ""
        lines.append(
            f"  uid=<code>{r.uid}</code> "
            f"keys=<code>{r.qnkeys}/{r.maxkeys}</code> "
            f"(<code>{_fmt_pct(r.keys_ratio)}</code>) "
            f"bytes=<code>{r.qnbytes}/{r.maxbytes}</code> "
            f"(<code>{_fmt_pct(r.bytes_ratio)}</code>){marker}"
        )

    lines.append("")
    if warn:
        uids = ", ".join(f"<code>{r.uid}</code>" for r in warning_rows)
        lines.append(
            f"<i>⚠ uid {uids} above <code>{int(_QUOTA_WARN_RATIO * 100)}%</code> "
            "of kernel keyring quota — further add_key(2) / keyctl(2) "
            "calls from this uid risk EDQUOT, which would manifest as "
            "TLS session / kerberos / fscrypt operations failing at "
            "the syscall boundary. Raise via "
            "<code>/proc/sys/kernel/keys/maxkeys</code> + "
            "<code>maxbytes</code>, or audit /admin_keys for leaks "
            "(revoked-but-not-unlinked keys still count against quota).</i>"
        )
    else:
        lines.append(
            f"<i>No warnings — every accounted uid is below "
            f"<code>{int(_QUOTA_WARN_RATIO * 100)}%</code> of both "
            "keys-count and bytes-count quotas. Ratio-based threshold "
            "(not absolute) because kernel.keys.maxkeys / maxbytes "
            "differ per kernel and per host tuning.</i>"
        )
    return "\n".join(lines)


async def handle_admin_key_users(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_key_users; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        uids=len(snap.rows),
        warning_uids=len(snap.warning_rows),
    ).info("/admin_key_users rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.key_users")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_key_users(message, settings)

    router.message.register(_entry, Command("admin_key_users", ignore_case=True))
    return router
