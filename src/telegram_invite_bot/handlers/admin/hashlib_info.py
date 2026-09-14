"""``/admin_hashlib`` — hashlib algorithm availability + FIPS posture.

Complements /admin_ssl (OpenSSL runtime + trust store + TLS flags) by
surfacing the **hashlib side** of the same OpenSSL binding: which hash
algorithms are actually constructible at runtime, and whether the
host is enforcing FIPS — which silently makes md5 / sha1 / blake2 /
sha3 raise :class:`ValueError` on construction.

Why an operator wants this:

* On RHEL/Rocky/Alma hosts with ``crypto-policies`` set to
  ``FIPS`` (or the kernel booted with ``fips=1`` — visible on
  /admin_kernel), :func:`hashlib.new("md5")` raises ``ValueError:
  unsupported hash type``. Code paths that compute md5 for cache
  keys / etag-style ids (legacy stats-image hash, telegraph
  media-id hashing) break at first call with a noisy traceback.
  This is the card that catches "we shipped to a FIPS host" before
  the first user hits the broken endpoint.
* :data:`hashlib.algorithms_guaranteed` is the Python-level
  *promise* (md5, sha1, sha224, sha256, sha384, sha512, blake2b,
  blake2s, sha3_*, shake_*). :data:`hashlib.algorithms_available`
  is what OpenSSL actually exposes on this build. The DIFF between
  the two is the operationally interesting set: a guaranteed hash
  missing from available → FIPS / hardened-policy host; an extra
  hash in available → host-specific OpenSSL build (e.g.
  ``sm3`` on Chinese-locale builds, ``ripemd160`` on older
  OpenSSL). Either way the operator wants to see the delta without
  shelling in.
* ``hashlib.pbkdf2_hmac`` / ``hashlib.scrypt`` availability is
  separately gated (scrypt requires OpenSSL ≥ 1.1) and a sudden
  drop in availability after an OS upgrade is exactly the silent
  regression this card surfaces.

Cross-references: pair with /admin_ssl for the OpenSSL version that
backs hashlib; pair with /admin_kernel for the ``fips=1`` cmdline
token if md5 is missing.

Silent-drop for non-devs, private-only at the router level. Same
posture as every other ``/admin_*``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.hashlib_info")


# The "we definitely use these" set. md5 is on this list because the
# AI answer cache keys on it (``AiResponseCache._key``, porting
# legacy bot.py:37103 and bot.py:37244) — on a FIPS build that call
# raises ``ValueError`` rather than degrading to a cache miss.
# sha256 because Telegram's webhook-secret rotation flow uses it.
# A drop in availability for either of these is an immediate-action
# item — hence the explicit ⚠ rather than just listing the diff.
_REQUIRED_ALGORITHMS: tuple[str, ...] = ("md5", "sha1", "sha256", "sha512")


# Whether scrypt + pbkdf2_hmac are usable. Both are OpenSSL-backed; a
# build without them is rare but possible (FIPS-constrained, static
# OpenSSL without scrypt). Probe by attempting a 1-byte derivation
# rather than by attribute existence — the attribute is present even
# when the underlying primitive isn't.
_PBKDF2_PROBE_PASSWORD = b"x"
_PBKDF2_PROBE_SALT = b"y"


class _HashlibSnapshot:
    """Captured hashlib runtime view.

    Sets rather than tuples for ``guaranteed`` / ``available`` so
    the render can compute differences cheaply. ``missing_required``
    is precomputed at capture time — the render layer is a pure
    function of the snapshot, same posture as every other admin card.
    """

    __slots__ = (
        "available",
        "extra",
        "guaranteed",
        "missing_guaranteed",
        "missing_required",
        "pbkdf2_ok",
        "scrypt_ok",
    )

    def __init__(
        self,
        *,
        guaranteed: set[str],
        available: set[str],
        missing_guaranteed: set[str],
        extra: set[str],
        missing_required: set[str],
        pbkdf2_ok: bool,
        scrypt_ok: bool,
    ) -> None:
        self.guaranteed = guaranteed
        self.available = available
        self.missing_guaranteed = missing_guaranteed
        self.extra = extra
        self.missing_required = missing_required
        self.pbkdf2_ok = pbkdf2_ok
        self.scrypt_ok = scrypt_ok


def _probe_pbkdf2() -> bool:
    """``True`` if ``hashlib.pbkdf2_hmac("sha256", ...)`` returns a digest.

    A FIPS-policy host that has disabled sha1 may still allow sha256
    via pbkdf2 — we probe with sha256 (which the policy keeps
    available even under strict FIPS) so a false negative on this
    specific probe is unambiguous: the underlying primitive is gone.
    """
    try:
        hashlib.pbkdf2_hmac("sha256", _PBKDF2_PROBE_PASSWORD, _PBKDF2_PROBE_SALT, 1)
    except (ValueError, TypeError):
        return False
    return True


def _probe_scrypt() -> bool:
    """``True`` if :func:`hashlib.scrypt` is callable with minimal params.

    Scrypt needs OpenSSL ≥ 1.1 and is the first thing to fall off on
    a static-OpenSSL-without-scrypt build. ``n=2, r=1, p=1`` is the
    smallest legal parameter set; we don't care about the digest,
    only that the call doesn't raise.
    """
    try:
        hashlib.scrypt(_PBKDF2_PROBE_PASSWORD, salt=_PBKDF2_PROBE_SALT, n=2, r=1, p=1, dklen=1)
    except (ValueError, TypeError):
        return False
    return True


def _capture() -> _HashlibSnapshot:
    """Sample hashlib state.

    No params — hashlib is process-global and there's no fixture
    surface to inject. Tests exercise the render layer directly
    via constructed _HashlibSnapshot instances.
    """
    guaranteed = set(hashlib.algorithms_guaranteed)
    available = set(hashlib.algorithms_available)
    missing_guaranteed = guaranteed - available
    extra = available - guaranteed
    missing_required = {a for a in _REQUIRED_ALGORITHMS if a not in available}
    return _HashlibSnapshot(
        guaranteed=guaranteed,
        available=available,
        missing_guaranteed=missing_guaranteed,
        extra=extra,
        missing_required=missing_required,
        pbkdf2_ok=_probe_pbkdf2(),
        scrypt_ok=_probe_scrypt(),
    )


def _fmt_set(names: set[str], *, limit: int = 20) -> str:
    """Render a set of algorithm names as a sorted comma list.

    Sorted because ``hashlib.algorithms_available`` is a Python
    ``set`` with no guaranteed iteration order — render stability
    across runs matters when an operator diffs two cards. Capped at
    ``limit`` items because ``algorithms_available`` can include
    20+ name aliases (``SHA256`` vs ``sha256`` vs ``sha-256``) on
    some OpenSSL builds.
    """
    if not names:
        return "<i>none</i>"
    items = sorted(names)
    if len(items) > limit:
        head = ", ".join(f"<code>{n}</code>" for n in items[:limit])
        return f"{head}, <i>… +{len(items) - limit} more</i>"
    return ", ".join(f"<code>{n}</code>" for n in items)


def _render(snap: _HashlibSnapshot) -> str:
    lines = ["🔐 <b>hashlib</b>", ""]

    required_warn = bool(snap.missing_required)
    required_marker = " ⚠" if required_warn else ""
    lines.append(
        f"  • <b>required available:</b> "
        f"<code>{len(_REQUIRED_ALGORITHMS) - len(snap.missing_required)}"
        f"/{len(_REQUIRED_ALGORITHMS)}</code>{required_marker}"
    )
    if snap.missing_required:
        lines.append(f"    missing: {_fmt_set(snap.missing_required)}")

    lines.append(f"  • <b>guaranteed:</b> <code>{len(snap.guaranteed)}</code>")
    lines.append(f"  • <b>available:</b> <code>{len(snap.available)}</code>")

    if snap.missing_guaranteed:
        # A guaranteed-by-CPython hash that isn't in available is the
        # FIPS smoking gun — surface it explicitly rather than make
        # the operator diff the two counts.
        lines.append(f"  • <b>missing-from-guaranteed ⚠:</b> {_fmt_set(snap.missing_guaranteed)}")

    if snap.extra:
        # Extras are informational — host-specific OpenSSL builds
        # surface sm3, ripemd160, whirlpool etc. Not a warning,
        # just useful context.
        lines.append(f"  • <b>host extras:</b> {_fmt_set(snap.extra)}")

    lines.append("")
    pbkdf2_marker = "" if snap.pbkdf2_ok else " ⚠"
    scrypt_marker = "" if snap.scrypt_ok else " ⚠"
    lines.append(
        f"  • <b>pbkdf2_hmac(sha256):</b> "
        f"<code>{'ok' if snap.pbkdf2_ok else 'unavailable'}</code>{pbkdf2_marker}"
    )
    lines.append(
        f"  • <b>scrypt:</b> "
        f"<code>{'ok' if snap.scrypt_ok else 'unavailable'}</code>{scrypt_marker}"
    )

    lines.append("")
    lines.append(
        "<i>⚠ markers: a required hash (md5/sha1/sha256/sha512) is "
        "unavailable — FIPS-enforced host? Cross-check /admin_kernel "
        "for fips=1 cmdline and /admin_ssl for the OpenSSL build. "
        "pbkdf2/scrypt unavailability typically means a stripped-down "
        "OpenSSL or strict crypto-policies.</i>"
    )
    return "\n".join(lines)


async def handle_admin_hashlib(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_hashlib; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_hashlib rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.hashlib")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_hashlib(message, settings)

    router.message.register(_entry, Command("admin_hashlib", ignore_case=True))
    return router
