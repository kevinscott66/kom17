"""``/admin_envscan`` — credential-shaped env-var audit.

Complements /admin_settings (redacted pydantic-settings readout of the
*explicitly-modeled* config) and /admin_pythonpath (PYTHONPATH +
sys.path) by surfacing the **rest** of ``os.environ``: anything that
looks like a credential by name pattern, regardless of whether the
bot uses it.

Why an operator wants this:

* "What secrets are visible to this process?" — a leaked AWS_SECRET_-
  ACCESS_KEY or GITHUB_TOKEN baked into the systemd unit is invisible
  to /admin_settings (which only shows pydantic-modeled fields).
  This card pattern-matches the env var NAMES against the well-
  known credential shapes and surfaces presence + length + a tiny
  masked preview — enough to confirm the value is what you think
  it is without ever leaking the secret to the chat.
* "Did someone leave a debug env var on?" — ``PYTHONDEVMODE=1``,
  ``PYTHONFAULTHANDLER=1``, ``PYTHONASYNCIODEBUG=1`` change runtime
  behaviour silently. /admin_flags shows the sys.flags they
  produce, but ONLY the ones that map to flags; not all env vars
  surface there.
* "Are we shipping the right config to the right host?" — env vars
  matching the ``TG_`` / ``BOT_`` / ``DATABASE_`` prefixes the
  systemd unit uses are surfaced (key only, not value) so the
  operator can confirm the unit file is in fact loaded.

Posture:

* **Values are NEVER rendered in full.** Only a length and a
  first-2-/last-2-char preview. Telegram chat logs persist server-
  side and forwarding the card is a one-tap action; rendering a
  full credential here would be a real leak. Even the preview is
  suppressed for values shorter than 8 chars (too short to mask
  usefully).
* **Pattern-match on KEY names**, not values. We don't scan values
  for credential-shaped substrings — that's a separate, riskier
  exercise and false-positives badly. Names are stable, well-known,
  and grep-able.
* **Non-secret env vars are NOT listed by value either** — only the
  presence of operationally-relevant prefixes is reported, with a
  count. The card is an audit tool, not a debug printf.

Silent-drop for non-devs, private-only at the router level. Same
posture as every other ``/admin_*``.
"""

from __future__ import annotations

import html
import os
import re
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.envscan")


# Credential-shaped key patterns. Compiled once at import time
# (regex compile is cheap but not free; cards re-render on every
# invocation and there's no reason to recompile per-call). Patterns
# are deliberately broad — the false-positive cost is "a non-secret
# env var gets masked", which is fine. The false-negative cost is
# "a real credential goes unflagged", which we want to avoid.
_CREDENTIAL_KEY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r".*TOKEN$",
        r".*SECRET.*",
        r".*PASSWORD.*",
        r".*PASSWD.*",
        r".*API[_-]?KEY.*",
        r".*ACCESS[_-]?KEY.*",
        r".*PRIVATE[_-]?KEY.*",
        r".*CREDENTIAL.*",
        r".*AUTH.*",
        r".*DSN$",  # SENTRY_DSN, DATABASE_DSN — URL contains creds
    )
)


# Env-var prefixes whose PRESENCE is operationally interesting, even
# though the values are usually non-secret. We surface name + count
# but never values; the operator can grep the systemd unit if they
# need the value.
_OPERATIONAL_PREFIXES: tuple[str, ...] = (
    "TG_",
    "BOT_",
    "DATABASE_",
    "WEBHOOK_",
    "DEVELOPER_",
    "PYTHON",  # PYTHONDEVMODE, PYTHONFAULTHANDLER, PYTHONHASHSEED…
    "LC_",
    "LANG",
)


# Below this length we don't even show a masked preview — a 4-char
# token previewed as ``ab…cd`` IS the token. 8 is the line at which
# first-2 + last-2 leaves enough middle to keep the secret.
_MIN_PREVIEWABLE_LEN = 8


class _EnvSnapshot:
    """Captured env-var scan result.

    ``credential_keys`` is a sorted list of names matching the
    credential patterns. ``credential_values`` maps key → (length,
    preview) so render can format consistently without re-walking
    os.environ. ``operational_keys`` is the count-only view of
    keys matching ``_OPERATIONAL_PREFIXES`` — we keep the full
    names so the card can list them.
    """

    __slots__ = ("credential_keys", "credential_values", "operational_keys")

    def __init__(
        self,
        *,
        credential_keys: list[str],
        credential_values: dict[str, tuple[int, str]],
        operational_keys: list[str],
    ) -> None:
        self.credential_keys = credential_keys
        self.credential_values = credential_values
        self.operational_keys = operational_keys


def _is_credential_key(name: str) -> bool:
    return any(pat.match(name) for pat in _CREDENTIAL_KEY_PATTERNS)


def _is_operational_key(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _OPERATIONAL_PREFIXES)


def _mask_value(value: str) -> tuple[int, str]:
    """Return ``(length, preview)``. Preview is empty for very short
    values to avoid leaking the secret.

    The 8-char floor is conservative: a 7-char token previewed as
    ``ab…cd`` exposes 4 of the 7 chars, leaving only 3 to brute-
    force. ≥ 8 chars leaves at least 4 hidden middle bytes, which
    for any credential of reasonable entropy is enough.
    """
    length = len(value)
    if length < _MIN_PREVIEWABLE_LEN:
        return length, ""
    return length, f"{value[:2]}…{value[-2:]}"


def _capture(env: dict[str, str] | None = None) -> _EnvSnapshot:
    """Scan ``env`` (default ``os.environ``). Pure function of the env."""
    source = dict(env) if env is not None else dict(os.environ)
    cred_keys: list[str] = []
    cred_values: dict[str, tuple[int, str]] = {}
    op_keys: list[str] = []
    for name in sorted(source):
        if _is_credential_key(name):
            cred_keys.append(name)
            cred_values[name] = _mask_value(source[name])
        elif _is_operational_key(name):
            op_keys.append(name)
    return _EnvSnapshot(
        credential_keys=cred_keys,
        credential_values=cred_values,
        operational_keys=op_keys,
    )


# Cap rendered list lengths to keep the card under Telegram's
# 4096-char ceiling. A host with many env vars (CI runners often
# carry 50+ AUTH_* keys) would otherwise blow the budget on the
# credential section alone.
_MAX_CREDENTIAL_ROWS = 30
_MAX_OPERATIONAL_ROWS = 30


def _render(snap: _EnvSnapshot) -> str:
    lines = ["🗝 <b>Environment scan</b>", ""]

    if not snap.credential_keys:
        lines.append("  <b>credential-shaped:</b> <i>none</i>")
    else:
        lines.append(f"  <b>credential-shaped ({len(snap.credential_keys)}):</b>")
        for name in snap.credential_keys[:_MAX_CREDENTIAL_ROWS]:
            length, preview = snap.credential_values[name]
            # #191: BOTH halves are attacker-shaped. The preview is two
            # raw bytes off each end of a real secret, and the name is
            # whatever the host env happens to carry — a single ``<``
            # or ``&`` in either turns the whole card into a Telegram
            # 400 and the admin sees nothing at all. Escaping here (not
            # in ``_mask_value``) keeps the capture layer a pure
            # function of the env, which is what its tests assert.
            safe_name = html.escape(name)
            if preview:
                lines.append(
                    f"    • <code>{safe_name}</code> <i>(len={length}, {html.escape(preview)})</i>"
                )
            else:
                # Short value — render length but no preview.
                lines.append(
                    f"    • <code>{safe_name}</code> "
                    f"<i>(len={length}, &lt;{_MIN_PREVIEWABLE_LEN}-char, "
                    f"preview suppressed)</i>"
                )
        if len(snap.credential_keys) > _MAX_CREDENTIAL_ROWS:
            remaining = len(snap.credential_keys) - _MAX_CREDENTIAL_ROWS
            lines.append(f"    • <i>… and {remaining} more</i>")

    lines.append("")
    if not snap.operational_keys:
        lines.append("  <b>operational prefixes:</b> <i>none</i>")
    else:
        lines.append(f"  <b>operational prefixes ({len(snap.operational_keys)}):</b>")
        for name in snap.operational_keys[:_MAX_OPERATIONAL_ROWS]:
            lines.append(f"    • <code>{html.escape(name)}</code>")
        if len(snap.operational_keys) > _MAX_OPERATIONAL_ROWS:
            remaining = len(snap.operational_keys) - _MAX_OPERATIONAL_ROWS
            lines.append(f"    • <i>… and {remaining} more</i>")

    lines.append("")
    lines.append(
        f"<i>Values are NEVER rendered in full — only length + "
        f"first-2/last-2 preview, and even that is suppressed for "
        f"values under {_MIN_PREVIEWABLE_LEN} chars. Pattern match is "
        f"on KEY names only; this card cannot detect credential-"
        f"shaped values stored under non-credential-named keys. "
        f"Cross-check /admin_settings for the pydantic-modeled "
        f"config view.</i>"
    )
    return "\n".join(lines)


async def handle_admin_envscan(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_envscan; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        credential_count=len(snap.credential_keys),
        operational_count=len(snap.operational_keys),
    ).info("/admin_envscan rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.envscan")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_envscan(message, settings)

    router.message.register(_entry, Command("admin_envscan", ignore_case=True))
    return router
