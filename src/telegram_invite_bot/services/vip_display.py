"""Resolve a user's owned VIP *cosmetic* effects into a render-ready bundle.

L-36 — the cosmetic VIP effects (``color_nick``, ``custom_title``,
``legend``) are *bought* (the purchase/redeem path writes their rows
through :class:`PrivilegesRepo.grant_with_value`) but were never
*shown*: the /profile + mention renderers ignored them. This module closes the display gap. It is
strictly read-only over existing data — no purchase flow, no schema
change.

The three effects live in ``economy.user_privileges`` as
``privilege_type``-discriminated JSON rows (legacy
``ItemEffects.apply_*`` at ``bot.py:13406/13594/13681``):

* ``color_nick`` → ``{"color": "rainbow" | "#rrggbb" | "rrggbb"}``.
  Telegram messages can't tint text, so legacy renders a *marker
  emoji* in front of the name (``get_color_nick_emoji`` at
  ``bot.py:6885``): 🌈 for rainbow, 🎨 for a concrete colour. We mirror
  that exactly — the "colored nick" is a leading emoji, not real
  colour.
* ``legend`` → ``{"color": ..., "badge": "👑"}``. Legacy renders a
  fixed "💎 Легенда" line in the privileges block
  (``bot.py:6844``). We expose both the badge emoji (from the row, so
  a future per-user badge shows through) and a localised label.
* ``custom_title`` → ``{"title": "<free text>"}``. Legacy renders
  ``📝 <title>`` (``bot.py:6846``).

Why a new module rather than extending :class:`EffectsService`
==============================================================
``EffectsService`` produces *economy* bundles (daily/transfer
multipliers) consumed by service-layer money math. These are
*presentation* concerns consumed by handlers. Keeping them apart
means the profile/mention renderers don't drag the daily/transfer
resolvers into their import graph, and the money bundles don't grow
HTML-shaped fields. Same split rationale the codebase already uses
for ``EmojiBadgeService`` vs ``EffectsService``.

Expiry: ``color_nick`` and ``custom_title`` are timed (7/30-day
grants); ``legend`` is permanent (``expires_at=0``). The
:class:`PrivilegesRepo.get_active` read already filters expired rows
(``expires_at > 0 AND expires_at <= now``), so an aged-out colour or
title resolves to ``None`` here and simply isn't rendered — the user
falls back to a plain name, matching legacy where the getters return
``None`` past expiry.

HTML safety: the only free-form value is ``custom_title`` (the user
types it). The renderer escapes it; the rest are bot-controlled
emoji/labels. We do NOT escape inside the resolver — escaping is the
renderer's job so the bundle stays presentation-agnostic (a future
plain-text surface wouldn't want HTML entities baked in).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from telegram_invite_bot.i18n import t

log = logger.bind(component="services.vip_display")

if TYPE_CHECKING:
    from datetime import datetime

    from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo


# Legacy marker emojis (``bot.py:6885`` / ``6823``). Centralised so a
# future "emoji theme" change touches one place.
_RAINBOW_MARKER = "🌈"
_CUSTOM_COLOR_MARKER = "🎨"
_DEFAULT_LEGEND_BADGE = "👑"
_TITLE_MARKER = "📝"


def _color_marker(color: str) -> str:
    """Map a stored colour token → its leading marker emoji.

    ``rainbow`` → 🌈; a concrete colour (``#rrggbb`` or a bare 6-hex
    string) → 🎨; anything else falls back to 🌈, matching legacy's
    default-to-rainbow branch (``bot.py:6895``). Telegram can't tint
    message text, so the "colour" is communicated purely by which
    marker precedes the name.
    """
    normalized = color.strip().lower()
    if normalized == "rainbow":
        return _RAINBOW_MARKER
    if normalized.startswith("#") or len(normalized) == 6:
        return _CUSTOM_COLOR_MARKER
    return _RAINBOW_MARKER


@dataclass(frozen=True, slots=True)
class VipDisplayEffects:
    """The cosmetic VIP effects a user *currently* owns, render-ready.

    All three fields are independently optional — a user may own any
    subset. Empty/``None`` means "not owned (or expired)"; the
    renderer skips it. Frozen so a handler can't accidentally mutate a
    shared bundle.
    """

    color_marker: str = ""
    """Leading emoji for a colored-nick owner (🌈 / 🎨), else ``""``.
    Prepended (with a trailing space) to the display name."""

    custom_title: str | None = None
    """Raw user-supplied title text (NOT HTML-escaped — the renderer
    escapes). ``None`` when not owned."""

    legend_badge: str | None = None
    """Badge emoji for a legend (default 👑, but read from the row so a
    per-user badge shows through). ``None`` when not a legend."""

    @property
    def has_any(self) -> bool:
        """True if at least one effect is owned — lets a renderer skip
        building the whole privileges block for a plain user."""
        return bool(self.color_marker or self.custom_title or self.legend_badge)

    def decorate_name(self, name: str) -> str:
        """Prepend the colored-nick marker to ``name`` if owned.

        Mirrors legacy ``get_user_display_name_in_bot``
        (``bot.py:6903``): ``f"{emoji}{name}"`` where ``emoji`` already
        carries its trailing space. ``name`` is passed through
        untouched (caller is responsible for any HTML-escaping of the
        name itself) — this only adds a bot-controlled emoji prefix.
        """
        if not self.color_marker:
            return name
        return f"{self.color_marker} {name}"

    def title_label(self, lang: str) -> str | None:
        """Localised ``📝 <title>`` line, or ``None`` if no title owned.

        The title is the only free-form value, so the caller MUST
        HTML-escape the returned string before embedding it in an HTML
        message. We don't escape here so the bundle stays usable from a
        future plain-text surface."""
        if not self.custom_title:
            return None
        return f"{_TITLE_MARKER} {self.custom_title}"

    def legend_label(self, lang: str) -> str | None:
        """Localised ``💎 Легенда`` / ``👑 Legend``-style line, or ``None``.

        ``legend_badge`` (👑 by default) leads; the localised word
        follows. All bot-controlled — safe to embed without escaping."""
        if not self.legend_badge:
            return None
        return f"{self.legend_badge} {t('h_vip_legend_label', lang)}"


def _decode_value(raw: str | None) -> dict[str, object]:
    """Decode a ``user_privileges.value`` JSON blob into a dict.

    The column is a polymorphic TEXT store (legacy ``json.dumps``).
    A malformed / non-object payload (corruption, a manual DB edit)
    yields an empty dict so a single bad row degrades to "effect not
    shown" instead of raising mid-render. Read-only path — we never
    rewrite the row.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class VipDisplayService:
    """Read-side resolver: privilege rows → :class:`VipDisplayEffects`.

    Constructed per request from the same :class:`PrivilegesRepo` the
    :class:`EconomyMiddleware` already builds — the profile handler
    instantiates this from its injected ``privileges_repo`` kwarg, so
    no new middleware wiring is needed.

    Global scope only (``group_id=0``): these cosmetics are bought as
    global grants (legacy ``apply_color_nick`` / ``apply_legend_status``
    / ``apply_custom_title`` all write with no ``group_id``, defaulting
    to 0). A per-chat cosmetic isn't a thing in the legacy data, so we
    don't read the group slot.
    """

    def __init__(self, privileges_repo: PrivilegesRepo) -> None:
        self._privileges = privileges_repo

    async def resolve(self, user_id: int, *, now: datetime) -> VipDisplayEffects:
        """Read the three cosmetic privilege rows and bundle the owned ones.

        ``now`` is injected (not wall-clock) so the expiry filter uses
        the same instant the caller already computed for the rest of
        the profile render — no drift between "VIP shows" and the
        card's other time-based fields. Three independent reads against
        the request-scoped session; each is a PK point-lookup so the
        cost is three cheap indexed selects, not a scan.

        Cosmetic effects are a *non-critical display enhancement*: the
        /profile card must render whether or not the lookup succeeds. A
        SQLAlchemy error here (e.g. the economy schema absent in a
        minimal deployment / test, or a transient lock) degrades to an
        empty bundle — the user sees their plain card instead of a
        500-equivalent "something broke" message. Read-only path, so a
        swallowed read can never corrupt state; the worst case is a VIP
        momentarily not seeing their badge, which a retry fixes.
        """
        try:
            color_row = await self._privileges.get_active(user_id, "color_nick", now=now)
            legend_row = await self._privileges.get_active(user_id, "legend", now=now)
            title_row = await self._privileges.get_active(user_id, "custom_title", now=now)
        except SQLAlchemyError:
            log.bind(uid=user_id).warning(
                "vip_display: privilege lookup failed; rendering without effects"
            )
            return VipDisplayEffects()

        color_marker = ""
        if color_row is not None:
            color = str(_decode_value(color_row.value).get("color") or "rainbow")
            color_marker = _color_marker(color)

        custom_title: str | None = None
        if title_row is not None:
            raw_title = _decode_value(title_row.value).get("title")
            # Trim + guard empty so a ``{"title": ""}`` row (or one
            # with only whitespace) renders nothing rather than a bare
            # 📝 marker.
            if isinstance(raw_title, str) and raw_title.strip():
                custom_title = raw_title.strip()

        legend_badge: str | None = None
        if legend_row is not None:
            badge = _decode_value(legend_row.value).get("badge")
            legend_badge = (
                badge.strip() if isinstance(badge, str) and badge.strip() else _DEFAULT_LEGEND_BADGE
            )

        return VipDisplayEffects(
            color_marker=color_marker,
            custom_title=custom_title,
            legend_badge=legend_badge,
        )
