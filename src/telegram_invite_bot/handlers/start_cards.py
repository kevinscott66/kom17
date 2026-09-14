"""Renderers (and the two lookups they need) for the ``/start`` cards.

Split out of :mod:`~handlers.start` for two reasons:

* **No import cycle.** ``handlers.start`` imports the keyboard from
  ``handlers.main_menu``, and ``main_menu``'s ``home`` tap has to
  re-render the very same returning-user card. A shared leaf module is
  the only way both can call one renderer.
* **Testability.** The renderers are pure ``(data) -> str`` functions —
  no bot, no session, no clock — so the *shape* of what the user reads
  is pinned by cheap unit tests in both locales at once.

What the port restores (RR-6 #60/#61), relative to the one-line welcome
the monolith split shipped with:

* the new-user feature card (legacy ``get_welcome_text_new_user``,
  ``bot.py:16011``) — six capability bullets + the signup gift;
* the returning-user greeting (legacy ``get_greeting``, ``bot.py:15963``)
  — time-of-day salute, role glyph, balance, games played;
* the balance widget's daily-bonus flag (legacy ``get_balance_widget``,
  ``bot.py:16000``), upgraded from a boolean to the actual wait;
* the three-section group welcome (legacy ``send_start_panel``,
  ``bot.py:16384``) with a quick-command row.

Two deliberate improvements over legacy: the time-of-day bucket is
computed in the *user's* timezone (legacy used the server's local hour,
so a user abroad got "good morning" at midnight), and the cooldown is
rendered as a concrete ``Xч Yм`` instead of "bonus tomorrow".

All copy lives in YAML — this module only decides *which* keys to
compose and in what order.
"""

from __future__ import annotations

import html
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from telegram_invite_bot.db import DBName
from telegram_invite_bot.db.models.users import BotGroup
from telegram_invite_bot.handlers.daily import _format_wait
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.numbers import format_number
from telegram_invite_bot.utils.time import day_period

if TYPE_CHECKING:
    from datetime import datetime

    from aiogram.types import User as TgUser

    from telegram_invite_bot.db import EngineRegistry

#: Legacy ``get_greeting`` role glyphs (``bot.py:15983``), same precedence:
#: developer wins over group owner wins over plain member.
ROLE_DEVELOPER = "💻"
ROLE_OWNER = "👑"
ROLE_MEMBER = "👤"

#: Time-of-day glyph leading the returning-user salute. Legacy had none —
#: the greeting was bare text — but an emoji-led header is the house
#: style for every other card in the bot.
_PERIOD_ICON: dict[str, str] = {
    "morning": "🌅",
    "day": "☀️",
    "evening": "🌆",
    "night": "🌙",
}

#: Fallback display name when Telegram gives us neither ``first_name``
#: nor ``username``. Localised so an ``en`` user isn't called "друг".
_ANON_NAME = {"ru": "друг", "en": "friend"}


def display_name(tg_user: TgUser, lang: str) -> str:
    """Best available display name, localised fallback, HTML-escaped.

    Escaping happens here — one choke point for every ``/start`` branch
    and the menu re-render — because the dispatcher sends with
    ``parse_mode=HTML``, so a ``first_name``/``username`` carrying markup
    would otherwise render as live HTML (phishing / formatting
    injection; SEC audit).
    """
    raw = tg_user.first_name or tg_user.username
    return html.escape(raw or _ANON_NAME.get(lang, _ANON_NAME["ru"]))


async def owns_groups(registry: EngineRegistry, user_id: int) -> bool:
    """Is ``user_id`` the registrar of any chat the bot STILL lives in?

    Backs the 👑 role glyph legacy computed via ``user_has_groups``
    (``bot.py:15984``). One ``SELECT … LIMIT 1`` served straight off
    ``idx_bot_groups_added_by`` — cheap enough to sit on the bot's
    highest-traffic command, and read-only so it needs no session
    scoping. Engine-level (not :class:`BotGroupsRepo`) because neither
    ``/start`` nor the menu carries a ``users.db`` session — same
    approach ``handlers/mygroups.py`` already takes for its list query.
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(BotGroup.chat_id)
            .where(BotGroup.added_by_user_id == user_id)
            .where(BotGroup.is_active.is_distinct_from(0))
            .limit(1)
        )
        return result.first() is not None


def role_icon(*, is_developer: bool, owns_groups: bool) -> str:  # noqa: FBT001 — kw-only
    """Pick the glyph in front of the user's name.

    Mirrors legacy's precedence exactly: a developer who also owns
    groups still renders as 💻.
    """
    if is_developer:
        return ROLE_DEVELOPER
    if owns_groups:
        return ROLE_OWNER
    return ROLE_MEMBER


def greeting(lang: str, now: datetime) -> tuple[str, str]:
    """``(icon, salute)`` for the time-of-day bucket ``now`` falls in."""
    period = day_period(now)
    return _PERIOD_ICON[period], t(f"h_start_greet_{period}", lang)


def render_welcome_new(lang: str, *, name: str, bonus: int) -> str:
    """The first-launch feature card.

    ``name`` must already be HTML-escaped — use :func:`display_name`.
    """
    return t("h_start_welcome_new", lang, name=name, bonus=format_number(bonus))


def render_welcome_back(
    lang: str,
    *,
    name: str,
    role: str,
    now: datetime,
    balance: int,
    games_played: int,
    streak: int,
    cooldown: timedelta,
) -> str:
    """The returning-user dashboard card.

    Line order is legacy's (salute → balance → games) with the daily
    widget promoted from a trailing ``|``-joined fragment to its own
    line, plus a streak line that only appears when there *is* a streak
    to protect — an empty "🔥 0 days" row is noise, not information.

    ``name`` must already be HTML-escaped (see :func:`render_welcome_new`).
    """
    icon, salute = greeting(lang, now)
    lines = [
        t("h_start_back_hi", lang, icon=icon, greet=salute, role=role, name=name),
        "",
        t("h_start_back_balance", lang, balance=format_number(balance)),
        t("h_start_back_games", lang, games=format_number(games_played)),
    ]
    if streak > 0:
        lines.append(t("h_start_back_streak", lang, streak=streak))
    lines.append(
        t("h_start_daily_ready", lang)
        if cooldown <= timedelta(0)
        else t("h_start_daily_wait", lang, wait=_format_wait(cooldown, lang=lang))
    )
    lines += ["", t("h_start_back_choose", lang)]
    return "\n".join(lines)


def render_group_welcome(lang: str, *, name: str) -> str:
    """The three-section group card: hello → quick start → full list.

    Legacy split this across ``group_welcome_hi`` /
    ``group_welcome_quick`` / ``group_welcome_full_list``; keeping one
    key per section would leave translators guessing at the joins, so
    the sections are one template with the blank lines baked in.

    Legacy also had an *admin* variant pointing at a
    ``group_setup_<chat_id>`` deep link. That payload has no owner in the
    new pipeline, so porting it would ship a dead button — group admins
    get the same card and reach the panel via ``/groupadmin``.

    ``name`` must already be HTML-escaped (see :func:`render_welcome_new`).
    """
    return t("h_start_group_welcome", lang, name=name)
