"""Couple joint-activities catalog + pure availability logic (FEAT-COUPLE-ACT).

Ports the legacy paid inline-button "joint activities" data verbatim from
``bot.py``: a fixed table of MARRIAGE activities (no level gate) and a
fixed table of RELATIONSHIP activities (each with a ``min_level`` gate, a
coin cost, an XP reward, and a cosmetic ``effect_hours`` flavour
duration).

This module is *pure* — no DB, no aiogram, no i18n. The handler
(:mod:`telegram_invite_bot.handlers.couple_activities`) composes these
specs with the bond repo (XP grant) and the economy repo (debit/credit)
and renders them through ``t(...)``. Keeping the spec + the gating
arithmetic here lets the unit suite exercise the availability rules
without spinning up a dispatcher.

The "effect" is PURELY COSMETIC: legacy rendered a "the warm afterglow
lasts ~N hours" flavour line from ``effect_hours``; there is no effect
store, no buff, no expiry tracking. Marriage activities carry no effect
line at all.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarriageActivity:
    """One marriage joint-activity (no level gate).

    ``key`` is the stable wire/i18n identifier; ``icon`` the
    language-neutral glyph that leads every render of the row; ``cost``
    the coin price debited from the clicker; ``xp`` the experience
    granted to the pair.
    """

    key: str
    icon: str
    cost: int
    xp: int


@dataclass(frozen=True, slots=True)
class RelationshipActivity:
    """One relationship joint-activity.

    ``icon`` is the language-neutral glyph legacy stored alongside the
    row and led the confirmation story with; ``min_level`` is the
    minimum couple level required to perform it; ``effect_hours`` is the
    cosmetic flavour duration rendered in the confirmation (no real
    effect is stored).
    """

    key: str
    icon: str
    cost: int
    xp: int
    min_level: int
    effect_hours: int


# Ported verbatim from legacy bot.py. Order is the legacy display order
# (most → least premium for relationship; legacy marriage order for
# marriage).
#
# ``icon`` is data, not copy: it is identical in both locales, so it
# stays here rather than being duplicated into ru.yaml + en.yaml. The
# TITLES and the confirmation STORY LINES do differ per locale and live
# in the ``h_couple_act_name_*`` / ``h_couple_act_done_*`` i18n keys, so
# the ru/en convergence and no-Cyrillic-in-en guards can see them (RR-5
# #49/#51). Marriage icons are the emoji legacy prefixed onto its
# ``name_ru``/``name_en`` button captions (bot.py:21626-21633);
# relationship icons are the explicit ``icon`` column
# (bot.py:22240-22258).
MARRIAGE_ACTIVITIES: tuple[MarriageActivity, ...] = (
    MarriageActivity("dinner", "🍽", 100, 15),
    MarriageActivity("flowers_m", "🌸", 150, 25),
    MarriageActivity("date", "🎬", 300, 50),
    MarriageActivity("anniversary", "🎂", 600, 100),
    MarriageActivity("gift", "🎁", 1000, 150),
    MarriageActivity("trip_m", "✈️", 2000, 250),
)

RELATIONSHIP_ACTIVITIES: tuple[RelationshipActivity, ...] = (
    RelationshipActivity("big_gift", "🎁", 1350, 3000, 7, 90),
    RelationshipActivity("surprise", "🎁", 356, 750, 6, 72),
    RelationshipActivity("club", "🪩", 238, 500, 5, 60),
    RelationshipActivity("soul_talk", "💬", 150, 300, 4, 48),
    RelationshipActivity("cinema", "🎬", 100, 200, 3, 40),
    RelationshipActivity("candy", "🍬", 50, 100, 2, 30),
    RelationshipActivity("breakfast", "🍳", 50, 100, 2, 28),
    RelationshipActivity("walk_invite", "🚶", 35, 70, 2, 24),
    RelationshipActivity("chocolate", "🍫", 25, 50, 1, 18),
    RelationshipActivity("hug_act", "🫂", 15, 30, 1, 12),
    RelationshipActivity("talk_act", "💭", 15, 30, 1, 12),
    RelationshipActivity("meme", "😂", 10, 20, 0, 8),
    RelationshipActivity("share_food", "🍱", 10, 20, 0, 8),
    RelationshipActivity("joke", "😄", 5, 10, 0, 6),
    RelationshipActivity("compliment", "✨", 3, 5, 0, 4),
)

# Keys that appear ONLY in the history log — never in either catalog,
# so no button ever builds them and no story line exists for them
# (#232). Two legacy tables feed this: the RP-action display names
# (``RELATIONSHIP_RP_DISPLAY``, bot.py:22212-22238) and the activity
# keys retired before the catalog was last reshuffled
# (``RELATIONSHIP_ACTIVITIES_LEGACY_NAMES``, bot.py:22260-22267).
#
# Legacy stored icon and title fused into one localised string
# ("😆 Щекотать" / "😆 Tickle"); the split here follows the same rule
# the catalogs above use — the glyph is identical in both locales so
# it is DATA and lives here, the title is COPY and lives in
# ``h_couple_act_name_*``. Order is the legacy table order.
#
# Without this table the history card rendered the raw key: 14 of the
# 18 rows on production read "💕 rp_tickle" instead of "😆 Щекотать".
HISTORY_ONLY_ICONS: dict[str, str] = {
    "rp_handshake": "🤝",
    "rp_highfive": "🖐",
    "rp_hit": "👊",
    "rp_kick": "🦵",
    "rp_hug": "🫂",
    "rp_stroke": "🤲",
    "rp_sorry": "🙏",
    "rp_bite": "😬",
    "rp_tickle": "😆",
    "rp_calm": "🤗",
    "rp_feed": "🍽",
    "rp_drink": "🥤",
    "rp_offend": "😤",
    "rp_kiss": "💋",
    "rp_lick": "😛",
    "rp_gift": "🎁",
    "rp_dinner": "🍷",
    "rp_compliment": "💬",
    "rp_flowers": "🌸",
    "rp_confess": "💕",
    "rp_ring": "💍",
    "rp_photo": "📷",
    "rp_propose": "💍",
    "rp_engagement": "💒",
    "rp_wedding": "💒",
    "rp_sex": "👉👌",
    # Retired catalog keys — still referenced by old log rows.
    "cafe": "☕",
    "flowers": "🌸",
    "restaurant": "🍽",
    "concert": "🎵",
    "trip": "✈️",
    "iphone": "📱",
    "ring": "💍",
}

# Fast lookups by key for the do-activity callback path (avoids a linear
# scan over the tuple on every click).
MARRIAGE_BY_KEY: dict[str, MarriageActivity] = {a.key: a for a in MARRIAGE_ACTIVITIES}
RELATIONSHIP_BY_KEY: dict[str, RelationshipActivity] = {a.key: a for a in RELATIONSHIP_ACTIVITIES}


def relationship_available(activity: RelationshipActivity, *, level: int, balance: int) -> bool:
    """True iff the pair may perform ``activity`` right now.

    Two gates, both required: the couple's current ``level`` must be at
    least the activity's ``min_level`` AND the clicker's ``balance`` must
    cover the ``cost``. Marriage activities have no level gate — they use
    only the balance check at the call site, so this helper is
    relationship-only.
    """
    return level >= activity.min_level and balance >= activity.cost


def effect_hours_split(effect_hours: int) -> tuple[int, int]:
    """Split a flat ``effect_hours`` into ``(days, hours)`` for rendering.

    Mirrors the legacy flavour-line split: ``effect_hours`` divided into
    whole days plus the leftover hours. The handler picks the
    ``_d`` / ``_dh`` / ``_h`` i18n template based on which parts are
    non-zero (see :func:`effect_template_key`).
    """
    days, hours = divmod(max(effect_hours, 0), 24)
    return days, hours


def effect_template_key(effect_hours: int) -> str:
    """Choose the cosmetic-effect i18n key suffix for ``effect_hours``.

    Returns one of ``rel_activity_done_effect_d`` (whole days only),
    ``rel_activity_done_effect_dh`` (days + hours), or
    ``rel_activity_done_effect_h`` (hours only). ``effect_hours`` is
    always ≥ 1 for every catalog row, so there is no zero case.
    """
    days, hours = effect_hours_split(effect_hours)
    if days and hours:
        return "rel_activity_done_effect_dh"
    if days:
        return "rel_activity_done_effect_d"
    return "rel_activity_done_effect_h"
