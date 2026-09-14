"""``/profile`` handler — Stage 6 + T-024.4 (live preview restoration).

Two surfaces, one command:

* **Private chat** → an enriched **text** card (name / id / username /
  language / timezone / joined / last_seen / premium). Reads
  ``users.db`` only. This is the legacy private behaviour
  (``send_self_profile_with_preview`` rendered text-only in DMs).
* **Group chat** → a freshly-generated **PNG stats card** — the
  "real-time preview" the legacy monolith showed in groups
  (``bot.py:send_self_profile_with_preview``): the user's message
  activity for today / 7 / 30 days plus the all-time total, drawn as a
  labelled bar chart, with an identity+balance caption and a single
  owner-gated "🔄 Обновить" button that re-renders the card live.

The split mirrors the data each surface can reach: the group card
needs ``message_stats.db`` (per-chat activity) and ``economy.db``
(balance), both keyed by ``chat_id`` — meaningless in a DM where the
user doesn't accrue chat activity. So the private card stays text and
the group card carries the live image.

Aliases mirror legacy: ``/profile`` / ``/info`` / ``/профиль`` /
``/инфо`` plus ``/whoami`` / ``/me`` / ``/kom_whoami`` (self-profile
aliases at bot.py:41258). ``/profile @user`` / ``/profile <id>`` (args
present, #4) diverts to a read-only **cross-user** card that exposes
only public signals (name / id / rank / game record / achievement
count) — never the target's wallet balance or ledger, which stay
private to their own card. The reply form (``/profile`` as a reply)
is a deferred follow-on.

Side effect: ``user_service.touch()`` bumps ``last_seen`` on every
view — same legacy semantics (``update_user_info`` ran at the top of
nearly every handler).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.core.achievements import TOTAL as _ACH_TOTAL
from telegram_invite_bot.core.couple_activities import MARRIAGE_BY_KEY, RELATIONSHIP_BY_KEY
from telegram_invite_bot.core.moderation_reasons import reason_html
from telegram_invite_bot.core.ranks import RankLevel, rank_name
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.achievements import render_body as render_achievements_body
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import ProfilePanel, ProfileRefresh
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.message_stats import MessageStatsMiddleware
from telegram_invite_bot.repositories.bonds_repo import (
    MarriagesRepo,
    RelationshipsRepo,
    UserBond,
)
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
from telegram_invite_bot.repositories.referrals_repo import ReferralsRepo
from telegram_invite_bot.repositories.user_group_joins_repo import UserGroupJoinsRepo
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.services.payments.rates import (
    COINS_PER_USD,
    FALLBACK_USD_TO_RUB,
    rub_per_coin,
)
from telegram_invite_bot.services.rank_service import RankService
from telegram_invite_bot.services.vip_display import VipDisplayService
from telegram_invite_bot.utils.aiogram import edit_card, require_from_user
from telegram_invite_bot.utils.bonds import (
    format_db_date,
    marriage_level_name,
    marriage_xp_to_level,
    relationship_xp_to_level,
)
from telegram_invite_bot.utils.html import html_user_mention, visible_len
from telegram_invite_bot.utils.numbers import format_number, parse_int_token
from telegram_invite_bot.utils.stats_image import render_stats_card

log = logger.bind(component="handlers.profile")

# Coin→RUB peg used when no rate service is wired (unit tests, and the
# instant before the shared FX cache is warm). T-019 (R4) retired the
# hard-coded ``0.1``: it was the *derived* value at 900 coins/USDT and
# USD/RUB = 90, so it silently mispriced the card the moment either moved.
# The live path asks the shared CurrencyService, which anchors on the
# real /withdraw rate; this constant only reproduces the historic number
# for the no-service case.
_FALLBACK_RUB_PER_COIN = rub_per_coin(FALLBACK_USD_TO_RUB, float(COINS_PER_USD))
# Functional (non-cosmetic) privileges surfaced on the profile card, in
# display order, mapped to their i18n label key.
_PRIVILEGE_LABELS: tuple[tuple[str, str], ...] = (
    ("mute_protection", "h_profile_priv_mute_protection"),
    ("double_daily", "h_profile_priv_double_daily"),
    ("xp_boost", "h_profile_priv_xp_boost"),
)
# Audit-log ``action`` values (the literals ``ModerationRepo.record_action``
# is called with) → their i18n label key. An action outside this map falls
# back to ``h_profile_log_action_other`` rather than leaking a raw column
# value into the card.
#
# Legacy's ``_log_action_label`` (bot.py:35135) did exactly this, and its
# keys *do* exist (translations.py:453-461 ru, :1941-1949 en) — the map is
# a port, not a repair. The one genuine addition is ``fine``, which legacy
# had no label for at all.
#
# ``pin``/``unpin`` are deliberately absent even though
# ``ModerationRepo.record_action`` is called with them: those rows carry
# ``user_id=None`` (moderation.py:2175-2181, :2225-2231) and
# ``recent_actions`` filters on ``ModerationLog.user_id == user_id``
# (moderation_repo.py:476), which SQL equality never matches against
# NULL. They stay readable elsewhere: /groupadmin's log is
# ``recent_chat_actions`` (moderation_repo.py:489-511), which filters on
# ``chat_id`` alone, and ``groupadmin._ACTION_LABELS`` does carry both
# words.
#
# Legacy's ``change_rank`` rows are absent for a DIFFERENT reason, and
# an earlier version of this comment got it wrong (#1030). Written with
# a NULL ``chat_id`` (bot.py:6698-6703 against the signature at
# bot.py:9193-9200), they fail the ``chat_id`` equality on BOTH reads,
# so no surface shows them — /groupadmin included. Nor is there a label
# for them: ``_ACTION_LABELS`` has no ``change_rank`` entry, so one
# would render as the raw string through ``groupadmin._action_label``
# if it ever arrived. The port never writes them, so nothing here
# needs to change unless it starts to.
_LOG_ACTION_LABELS: dict[str, str] = {
    "warn": "h_profile_log_action_warn",
    # ``unwarn`` is the block's most common piece of *good* news (prod's
    # audit log has them), and without an entry here it rendered as the
    # generic "действие" placeholder.
    "unwarn": "h_profile_log_action_unwarn",
    "ban": "h_profile_log_action_ban",
    "kick": "h_profile_log_action_kick",
    "mute": "h_profile_log_action_mute",
    "unmute": "h_profile_log_action_unmute",
    "unban": "h_profile_log_action_unban",
    "fine": "h_profile_log_action_fine",
}
# Audit rows older than this stop following the user around, and at most
# ``_LOG_LIMIT`` of them are shown — both mirror legacy's
# ``get_moderation_logs(limit=3, …, days=90)`` call at bot.py:39783.
_LOG_DAYS = 90
_LOG_LIMIT = 3
# Ledger ``reason`` values matched WHOLE → the i18n key that renders
# them. ``reason`` is machine data exactly like the ``action`` column
# above: the writer picks the string, the reader picks the words.
# Before #1547 this panel printed the column verbatim, so the reader
# got the writer's language rather than their own.
#
# The first two entries are bare slugs, unreadable in any language.
# The three purchase lines (#1582) are Russian sentences and are
# matched here rather than re-spelled at the writer, for the reason
# spelled out over ``_TX_PROSE_REASONS`` below: only a reader-side
# mapping reaches the rows already in the production ledger.
_TX_REASON_LABELS: dict[str, str] = {
    "marriage_extend": "h_profile_tx_marriage_extend",
    "marriage_extend_refund": "h_profile_tx_marriage_extend_refund",
    "Покупка (ЮKassa)": "h_profile_tx_purchase_yookassa",
    "Покупка криптой (Crypto Pay)": "h_profile_tx_purchase_crypto",
    "Покупка (Stripe)": "h_profile_tx_purchase_stripe",
}
# The same, for reasons written as ``"<prefix>:<id>"``. The id names
# what the row is about — a user for the referral commission, a group
# for the treasury payout — and is part of the phrase, so the key here
# takes an ``{id}`` placeholder.
_TX_REASON_ID_LABELS: dict[str, str] = {
    "referral_purchase_commission": "h_profile_tx_referral_commission",
    "group_treasury_payout": "h_profile_tx_treasury_payout",
}
# Reasons whose writer stores Russian prose around a value that has to
# be lifted back out, so an exact-match table cannot hold them. Two are
# the strings #1547 was filed about; the third keeps that shape
# deliberately, so an admin greps the ledger identically across legacy
# and the port — see the note at
# ``ReferralCommissionService.apply_developer_commission``. The fourth
# is the shop line (#1582), and it is the one whose captured group is
# not a number: the third element of each entry names the placeholder
# the key expects, so the item name does not have to travel under
# ``{id}``.
#
# Recognising the prose here rather than only re-spelling the writers is
# what makes the fix reach rows ALREADY in the production ledger: a
# writer-side change can only ever fix rows not yet written, and
# ``TransactionsRepo.recent`` is a top-five rather than a window, so an
# inactive user's card keeps showing its oldest rows indefinitely. Each
# pattern is pinned to the source line that produces it by
# ``tests/regression/test_tx_reason_labels.py``, which is what stops a
# reworded writer from silently falling back to the raw string.
_TX_PROSE_REASONS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"^С покупки в группе (-?\d+) \(за вычетом комиссии\)$"),
        "h_profile_tx_group_share",
        "id",
    ),
    (
        re.compile(r"^Комиссия с покупки в группе (-?\d+)$"),
        "h_profile_tx_group_fee",
        "id",
    ),
    (
        re.compile(r"^Комиссия с покупки монет \(покупатель (\d+)\)$"),
        "h_profile_tx_developer_commission",
        "id",
    ),
    (
        re.compile(r"^Покупка: (.+)$"),
        "h_profile_tx_purchase_item",
        "name",
    ),
    # #2007's three lines. The share pattern has to be tried before the
    # bare one, or ``Донат в группу -100555 (за вычетом комиссии)``
    # would never reach it — but both are anchored at ``$``, so the
    # order is documentation rather than load-bearing.
    (
        re.compile(r"^Донат в группу (-?\d+) \(за вычетом комиссии\)$"),
        "h_profile_tx_donate_share",
        "id",
    ),
    (
        re.compile(r"^Донат в группу (-?\d+)$"),
        "h_profile_tx_donate",
        "id",
    ),
    (
        re.compile(r"^Комиссия с доната в группу (-?\d+)$"),
        "h_profile_tx_donate_fee",
        "id",
    ),
)
# Legacy truncated each reason to 50 chars *before* escaping (bot.py:39802).
# Kept identical: the cap is about caption budget, and counting escaped
# length instead would silently show less text for a reason containing
# ``<`` than for one that doesn't. Both the cap and that order apply to
# a moderator's own words only — a bot-written slug resolves to a label
# of our own choosing and goes out whole (#1639). ``reason_html`` is
# where the two branches part.
_REASON_MAX = 50
# Telegram's photo-caption ceiling. The group profile card is sent as a
# photo caption, so everything below has to fit in one.
_CAPTION_MAX = 1024
# Lines :func:`_clamp_caption` will never drop — title, name, id. Below
# that the message stops being a profile card at all, and a card whose
# identity block alone overflows is a bug to fix at the source, not
# something to paper over by emitting a nameless stub.
_CAPTION_MIN_LINES = 3
# Bonds of each kind shown on the social panel. A marriage or a
# relationship per group adds up for anyone active in many chats, and
# the panel is one edited message — the rest collapse into a "+N more".
_SOC_BOND_LIMIT = 5
# How many of each kind we actually pull. The footer reports how many were
# hidden, and that number has to be real — fetching ``_SOC_BOND_LIMIT + 1``
# would make it permanently "1" for someone with twenty. Fifty rows is a
# trivial read and past any plausible user; beyond it the footer says so
# explicitly ("45+") rather than quietly under-counting.
_SOC_BOND_FETCH = 50

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import CallbackQuery
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import Settings, StatsConfig
    from telegram_invite_bot.core.entities.user import User as UserEntity
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.achievements_repo import AchievementsRepo
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo
    from telegram_invite_bot.repositories.moderation_repo import ActionRow
    from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.repositories.vip_repo import VipRepo
    from telegram_invite_bot.services.currency_service import CurrencyService
    from telegram_invite_bot.services.emoji_badge_service import EmojiBadgeService
    from telegram_invite_bot.services.user_service import UserService
    from telegram_invite_bot.services.vip_display import VipDisplayEffects


def _user_tz(tz_name: str | None) -> tzinfo:
    """Resolve a ``/timezone`` preference to a tzinfo, degrading to UTC.

    Same failure set :func:`utils.time.local_now` swallows: a stale or
    garbage ``user_settings.timezone`` row must not 500 a card. Kept
    local rather than pushed into ``utils.time`` because the card wants
    the *zone*, not a clock reading.
    """
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return UTC


def _fmt_dt(value: object, tz: tzinfo | None = None) -> str:
    """Render a datetime as ``YYYY-MM-DD HH:MM``; collapse missing → ``—``.

    Stored wall-clock columns are naive UTC (``utils.time.db_now``), so a
    ``tz`` argument converts before formatting. Without it the raw UTC
    value is printed, which is only honest when nothing on the same card
    claims otherwise — the DM card prints the user's ``/timezone`` two
    lines above these stamps, so it always passes one (#476).
    """
    if isinstance(value, datetime):
        if tz is not None:
            aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
            value = aware.astimezone(tz)
        return value.strftime("%Y-%m-%d %H:%M")
    return "—"


def _full_name(user: UserEntity) -> str:
    parts = [user.first_name, user.last_name]
    return " ".join(p for p in parts if p) or "—"


def _caption_len(text: str) -> int:
    """Length of ``text`` in the units Telegram actually counts.

    Two corrections over ``len(text)``, and they pull in opposite
    directions, so neither cancels the other:

    * Markup doesn't count. Telegram parses the HTML first and measures
      the resulting *text*; the tags become entity offsets. Counting the
      raw string over-states a card by hundreds of characters — a full
      ru card measures ~1100 raw but ~900 parsed — and the tail we'd
      drop to "fix" that is exactly the restored history block.
    * Emoji count double. The limit is in UTF-16 code units, so every
      astral character (most emoji, and a display name may be nothing
      but emoji) is two. Counting code points under-states the card and
      lets a caption past the guard that Telegram then rejects whole.

    #302: neither correction is specific to a caption budget, and this
    used to say otherwise while spelling both out by hand. They are the
    same two corrections every Telegram length guard in this codebase
    needs, and since #187 :func:`utils.html.visible_len` is the one
    implementation of them. What stays local here is only the *limit*
    (``_CAPTION_MAX``, 1024 rather than 4096) and the line-boundary
    clamping below — the measuring is shared.
    """
    return visible_len(text)


def _clamp_caption(lines: list[str], limit: int = _CAPTION_MAX) -> str:
    """Join ``lines`` into a caption that fits ``limit``, dropping tails.

    Whole lines are dropped, never characters: every line in this card
    opens and closes its own tags, so cutting at a line boundary always
    leaves valid HTML, while cutting mid-string could leave a dangling
    ``<a href=`` and make Telegram reject the *entire* message — turning
    a slightly-too-long card into no card at all.

    The lines this sheds are the ones appended last (moderation history,
    then activity), which is also the order we'd choose deliberately:
    identity survives, history is the part a reader can go look up
    elsewhere. Blank separators and block headers left stranded at the
    end are trimmed too: a caption ending in ``📋 Последние действия:``
    with nothing under it reads as a bug, not as a truncation.
    """
    kept = list(lines)
    while len(kept) > _CAPTION_MIN_LINES and _caption_len("\n".join(kept)) > limit:
        kept.pop()
    # ``endswith(":</b>")`` identifies a header because that is how this
    # module writes them (``h_profile_last_actions`` and friends are
    # ``<b>…:</b>``) — it is a property of our own templates, not a guess
    # about arbitrary text.
    while len(kept) > _CAPTION_MIN_LINES and (
        not kept[-1].strip() or kept[-1].rstrip().endswith(":</b>")
    ):
        kept.pop()
    return "\n".join(kept)


def _log_action_label(action: str, lang: str) -> str:
    """Human label for an audit-log ``action`` value."""
    return t(_LOG_ACTION_LABELS.get(action, "h_profile_log_action_other"), lang)


def _tx_label(reason: str | None, tx_type: str, lang: str) -> str:
    """Human label for one ledger row, in the *reader's* language.

    ``reason`` is machine data the writer chose, exactly like the
    ``action`` column above; the words come from here. #1547: three
    writers stored Russian prose instead and this panel printed the
    column verbatim, so a group owner reading an English card was told
    «С покупки в группе -100123 (за вычетом комиссии)» — the same
    regression :mod:`telegram_invite_bot.utils.names` was written to
    end. Two more writers stored readable-but-bare ASCII slugs
    (``marriage_extend``, ``marry:flowers``), which nobody can read in
    either language.

    Rows in every one of those spellings are already in the ledger, so
    the prose forms are recognised here rather than only re-spelled at
    the writers: ``TransactionsRepo.recent`` is a top-five and not a
    window, so an inactive user's card would keep showing the old rows
    indefinitely (the date-conversion comment in
    :func:`_panel_finances` makes the same point about a one-off
    backfill).

    An unrecognised reason falls back to the pre-#1547 behaviour, the
    escaped raw string: a label this has no words for still beats no
    label at all. That is also the only branch that escapes —
    :func:`telegram_invite_bot.i18n.t` returns HTML already.
    """
    if not reason:
        return html.escape(tx_type)

    key = _TX_REASON_LABELS.get(reason)
    if key is not None:
        return t(key, lang)

    prefix, separator, argument = reason.partition(":")
    if separator:
        id_key = _TX_REASON_ID_LABELS.get(prefix)
        if id_key is not None:
            # Our own writer puts a user id here, but the value arrives
            # from the column and this string is spliced into HTML.
            return t(id_key, lang, id=html.escape(argument))
        # The couple activities already own a localised name for every
        # key they write, so the panel borrows it instead of printing
        # ``marry:flowers``. Membership is checked because ``t`` answers
        # an unknown key with the key itself, which would read worse
        # than the raw reason it replaced.
        if (prefix == "marry" and argument in MARRIAGE_BY_KEY) or (
            prefix == "rel" and argument in RELATIONSHIP_BY_KEY
        ):
            return t(f"h_couple_act_name_{argument}", lang)

    for pattern, prose_key, placeholder in _TX_PROSE_REASONS:
        match = pattern.match(reason)
        if match is not None:
            # Escaped for the same reason the prefixed branch above
            # escapes it: the captured text comes off the column and
            # is spliced into HTML. For the three numeric captures
            # that is a no-op; for the shop item name (#1582), which
            # is free text an owner typed, it is not.
            captured = html.escape(match.group(1))
            return t(prose_key, lang, **{placeholder: captured})

    return html.escape(reason)


def _mod_action_lines(rows: list[ActionRow], lang: str, now: datetime) -> list[str]:
    """The ``📋 Последние действия`` block, or ``[]`` when there are none.

    ``ActionRow.date`` is naive UTC (that is what ``record_action``
    writes); ``now`` carries the card's configured display timezone. The
    rows are converted before formatting so a Moscow reader isn't told
    their mute happened three hours earlier than it did. Legacy did the
    same for ``ru`` (bot.py:44381-44389 converts UTC→MSK); only its
    ``en`` branch printed the raw stamp with a literal "UTC" suffix,
    which the caller then stripped (bot.py:39798). Converting for both
    languages is the ``ru`` behaviour generalised, not a new invention.

    The 90-day window means legacy-written rows are still in scope, and
    legacy wrote a bare ``datetime.now()`` — naive *server-local*. The
    production host runs ``Europe/Moscow``, not UTC, so those pre-cutover
    rows are already three hours ahead of the frame this function assumes
    and the column carries no discriminator to tell them apart. They age
    out of the window on their own; do not "fix" them by changing the
    server timezone, which would only shift the boundary.

    ``reason`` is free-form text typed by a moderator on most rows and
    a slug the bot wrote itself on the rest;
    :func:`~telegram_invite_bot.core.moderation_reasons.reason_html`
    tells those apart and returns finished HTML either way (#1346,
    #1639). It is called instead of ``reason_label`` because escaping
    and clipping are right for one branch and wrong for the other,
    and a caller holding only the rendered label cannot tell which of
    the two it is holding.

    The action label is not escaped here either, for the same reason:
    :func:`_log_action_label` returns ``t()`` output, which is already
    HTML. That escape was the same latent double-encode as the one
    #1639 names, one line below it.
    """
    if not rows:
        return []
    lines = [t("h_profile_last_actions", lang)]
    for row in rows:
        stamp = row.date.replace(tzinfo=UTC).astimezone(now.tzinfo).strftime("%d.%m.%Y %H:%M")
        reason = reason_html(row.reason or "", lang, limit=_REASON_MAX) or "—"
        label = _log_action_label(row.action, lang)
        lines.append(f"  • {label} ({stamp}): {reason}")
    return lines


def _name_html(
    user: UserEntity,
    effects: VipDisplayEffects | None,
    badge: str | None = None,
) -> str:
    """Render the bolded display name, prefixed by cosmetic markers.

    The name is HTML-escaped first (free-form Telegram string), then the
    bot-controlled colour marker (🌈/🎨) is prepended *outside* the
    ``<b>`` so the emoji isn't bolded — matching legacy
    ``get_user_display_name_in_bot`` (``bot.py:6903``) where the emoji
    leads the name. No effects (or none owned) → plain bold name, byte
    for byte the previous behaviour.

    ``badge`` is the VIP cosmetic emoji badge (#25, ``/emoji_set``),
    already gated on *current* VIP status by
    :meth:`EmojiBadgeService.active_badge`. It is a trusted
    ``VIP_BADGE_SET`` member (no escaping needed) and sits *outermost* —
    before the colour marker — so a VIP with both a rainbow nick and a
    badge renders ``👑 🌈 <b>Name</b>``."""
    safe = f"<b>{html.escape(_full_name(user))}</b>"
    if effects is not None:
        safe = effects.decorate_name(safe)
    if badge:
        safe = f"{badge} {safe}"
    return safe


def _effect_lines(effects: VipDisplayEffects | None, lang: str) -> list[str]:
    """Build the optional ``💎 Легенда`` / ``📝 <title>`` lines.

    Returns ``[]`` when the user owns neither, so the caller appends
    nothing and the card is unchanged for a plain user. The legend
    label + badge are bot-controlled (safe); the custom title is
    user-supplied and HTML-escaped here before embedding."""
    if effects is None:
        return []
    lines: list[str] = []
    legend = effects.legend_label(lang)
    if legend is not None:
        lines.append(legend)
    title = effects.title_label(lang)
    if title is not None:
        # ``title`` is ``📝 <raw user text>``; escape the whole thing —
        # the 📝 marker is ASCII-safe so escaping it is a no-op, and the
        # user-typed remainder gets neutralised.
        lines.append(html.escape(title))
    return lines


def _vip_line(vip_till: float | None, now: datetime, lang: str) -> str | None:
    """The 👑 VIP status line for the DM card, or ``None`` to omit it.

    Active grant → "until DD.MM.YYYY (N days)"; an absent or already
    elapsed grant returns ``None`` so a never-VIP user's card isn't
    cluttered with a redundant "no VIP" row (legacy bot.py:39759 only
    printed the line when VIP was live).
    """
    if vip_till is None or vip_till <= now.timestamp():
        return None
    expires = datetime.fromtimestamp(vip_till, tz=now.tzinfo)
    days_left = max(0, (expires.date() - now.date()).days)
    until = expires.strftime("%d.%m.%Y")
    value = t("h_profile_vip_active", lang, until=until, days=days_left)
    return f"{t('h_profile_vip', lang)}: {value}"


def _format_text(
    user: UserEntity,
    effects: VipDisplayEffects | None = None,
    badge: str | None = None,
    *,
    balance: int | None = None,
    privileges: str | None = None,
    rank: str | None = None,
    vip_line: str | None = None,
) -> str:
    """Private-chat text card. Escapes every free-form Telegram string.

    RR-1 #1: ``balance`` / ``privileges`` / ``rank`` / ``vip_line`` are
    the dashboard lines legacy showed in the DM card; all optional so a
    caller that can't reach a given source degrades to a leaner card
    rather than failing.
    """
    lang = user.language
    username = f"@{user.username}" if user.username else "—"
    premium = "✅" if user.is_premium else "—"
    tz_value = html.escape(user.timezone) if user.timezone else "—"
    # The two stamps below are rendered in the very timezone printed on
    # the line above them; without this they were raw UTC under a
    # "Часовой пояс: Europe/Moscow" label (#476).
    user_tz = _user_tz(user.timezone)
    lines = [
        t("h_profile_title", lang),
        "",
        f"{t('h_profile_name', lang)}: {_name_html(user, effects, badge)}",
        f"{t('h_profile_id', lang)}: <code>{user.user_id}</code>",
        f"{t('h_profile_username', lang)}: {html.escape(username)}",
        f"{t('h_profile_lang', lang)}: {lang}",
        f"{t('h_profile_timezone', lang)}: {tz_value}",
        f"{t('h_profile_joined', lang)}: {_fmt_dt(user.joined_date, user_tz)}",
        f"{t('h_profile_last_seen', lang)}: {_fmt_dt(user.last_seen, user_tz)}",
        f"{t('h_profile_premium', lang)}: {premium}",
        *_effect_lines(effects, lang),
    ]
    if rank is not None:
        lines.append(f"🎖 {t('h_profile_status', lang)}: <b>{html.escape(rank)}</b>")
    if vip_line is not None:
        lines.append(vip_line)
    if privileges is not None:
        lines.append(f"📋 {t('h_profile_privileges', lang)}: {privileges}")
    if balance is not None:
        lines.append(f"{t('h_profile_balance', lang)}: <b>{format_number(balance)}</b> 🪙")
    lines += ["", t("h_profile_footer", lang)]
    return "\n".join(lines)


async def _privileges_line(
    privileges_repo: PrivilegesRepo,
    user_id: int,
    effects: VipDisplayEffects | None,
    lang: str,
    *,
    now: datetime,
) -> str:
    """Comma list of the user's active perks (legacy ``📋 Привилегии``).

    Cosmetic perks come from the already-resolved ``effects`` bundle;
    the functional ones (mute-protection / double-daily / xp-boost) are
    probed against the privileges table. ``—`` when the user owns none.
    """
    labels: list[str] = []
    if effects is not None:
        if effects.legend_badge:
            labels.append(t("h_profile_priv_legend", lang))
        if effects.custom_title:
            labels.append(t("h_profile_priv_custom_title", lang))
        if effects.color_marker:
            labels.append(t("h_profile_priv_color_nick", lang))
    for priv_type, label_key in _PRIVILEGE_LABELS:
        if await privileges_repo.get_active(user_id, priv_type, now=now) is not None:
            labels.append(t(label_key, lang))
    return ", ".join(labels) if labels else "—"


async def _rub_per_coin(currency: CurrencyService | None) -> float:
    """RUB a coin is worth right now, best-effort.

    Asks the shared (1h-cached) FX service, which anchors the table on
    the real /withdraw rate. Falls back to the historic peg on a missing
    service or a nonsense quote — a profile card must never fail, and
    must never print a zero or negative price, over an FX hiccup.
    """
    if currency is None:
        return _FALLBACK_RUB_PER_COIN
    try:
        live = await currency.get_rate("RUB")
    except Exception:  # noqa: BLE001 — the RUB line is best-effort
        log.opt(exception=True).debug("profile RUB peg fell back to the offline anchor")
        return _FALLBACK_RUB_PER_COIN
    return live if live > 0.0 else _FALLBACK_RUB_PER_COIN


async def _group_caption(
    user: UserEntity,
    chat_id: int,
    balance: int,
    *,
    currency: CurrencyService | None,
    now: datetime,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    privileges_repo: PrivilegesRepo,
    message_stats_repo: MessageStatsRepo,
    vip_repo: VipRepo,
    today_n: int,
    week_n: int,
    month_n: int,
    total_n: int,
    effects: VipDisplayEffects | None,
    badge: str | None,
) -> str:
    """The rich group-profile caption (legacy ``send_profile_card`` parity).

    Restores the full identity + lifecycle + status + activity + warnings
    block the monolith showed, which was trimmed to 4 lines during the
    strangler split. Each cross-DB read degrades gracefully so a transient
    failure on one block never blanks the whole card.

    RR-1 #3 adds back the last three pieces:

    * **"in this group since"** now prefers the real per-chat join record
      (``users.user_group_joins``) and only falls back to the first day
      we counted a message. The two can differ by months for anyone who
      lurked, and only the former can honestly anchor the next line.
    * **"messages since joining"** — ``—`` when the join date is unknown,
      exactly as legacy rendered it (bot.py:39860). Deriving it from
      first-activity instead would make it equal the lifetime total for
      every user, i.e. print the line above twice under a new label.
    * **the moderation history block** — the last few audit entries, and
      like the warnings line above it, never shown to the chat's creator.

    The whole thing goes through :func:`_clamp_caption` because it is a
    *photo caption* (1024, not 4096) and the blocks added here are the
    ones that scale with how eventful a user has been.
    """
    lang = user.language
    name_link = f'<a href="tg://user?id={user.user_id}">{_name_html(user, effects, badge)}</a>'

    # Status (rank) — masks developer→owner inside a group (rank_name).
    try:
        rank_level = await RankService(registry, settings).get_rank(user.user_id)
    except Exception:  # noqa: BLE001 — never blank the card on a rank hiccup
        rank_level = RankLevel.USER
    status_str = rank_name(rank_level, lang, in_group=True)

    # Live Telegram membership → "group admin: yes" line + creator flag
    # (the chat creator never shows a warnings block, mirroring legacy).
    is_admin = False
    # Tri-state on purpose. ``False`` means "Telegram told us they are not
    # the creator"; ``None`` means we never got an answer. The moderation
    # block below is hidden in both the True and the None case, so a
    # transient ``get_chat_member`` failure can't publish the creator's
    # own warnings and history into the group — the one thing this branch
    # exists to prevent. It fails closed; the cost is a missing block on a
    # card that can be refreshed.
    is_creator: bool | None = None
    with contextlib.suppress(Exception):
        member = await bot.get_chat_member(chat_id, user.user_id)
        is_admin = member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
        is_creator = member.status == ChatMemberStatus.CREATOR

    privileges = await _privileges_line(privileges_repo, user.user_id, effects, lang, now=now)

    # "In this group since" + "messages since joining". The join record is
    # authoritative; ``first_activity_date`` is the fallback for everyone
    # who was already in the chat before either bot started recording
    # joins. Only the authoritative date can anchor a since-join counter —
    # counting from first activity would return the lifetime total.
    # Degrading is deliberate; degrading *silently* is not — every one of
    # these blocks logs what it swallowed, so "the VIP line vanished"
    # leaves a trail instead of looking like a design choice.
    #
    # The stored instant is naive UTC; ``now`` carries the card's display
    # timezone, and ``message_stats`` rows are stamped as *calendar days
    # in that same zone*. So the instant is converted before either use:
    # taking ``.date()`` off the raw UTC value would ask for the wrong
    # calendar day for anyone who joined near midnight (23:30 UTC is
    # already tomorrow in Moscow), folding a day of pre-membership
    # messages into the counter and printing a date one off from the one
    # the rest of the card speaks.
    join_day: date | None = None
    since_join: int | None = None
    try:
        async with session_for(registry, DBName.USERS) as usession:
            join_dt = await UserGroupJoinsRepo(usession).joined_at(user.user_id, chat_id)
        if join_dt is not None:
            join_day = join_dt.replace(tzinfo=UTC).astimezone(now.tzinfo).date()
            since_join = await message_stats_repo.count_since(user.user_id, chat_id, since=join_day)
    except Exception:  # noqa: BLE001 — the lifecycle lines are best-effort
        log.opt(exception=True).debug("profile join-date block skipped")
    if join_day is not None:
        since_str = join_day.strftime("%d.%m.%Y")
    else:
        since_date = await message_stats_repo.first_activity_date(user.user_id, chat_id)
        since_str = since_date.strftime("%d.%m.%Y") if since_date is not None else "—"

    vip_line: str | None = None
    try:
        vip_line = _vip_line(await vip_repo.get_vip_till(user.user_id), now, lang)
    except Exception:  # noqa: BLE001 — the VIP line is best-effort
        log.opt(exception=True).debug("profile VIP line skipped")

    rub = round(balance * await _rub_per_coin(currency))

    # The city is a user-set preference (``/city``) and legacy printed it
    # here, showing the "set your city" hint only while it was still
    # empty (bot.py:39828-39832). The port hardcoded the dash, so the
    # hint was permanent and ``/city`` had no visible effect anywhere
    # (#481). Best-effort like the lifecycle block above: a missing
    # ``user_settings`` schema degrades to the legacy empty rendering.
    city: str | None = None
    try:
        async with session_for(registry, DBName.USERS) as usession:
            city = await UserSettingsRepo(usession).get_city(user.user_id)
    except Exception:  # noqa: BLE001 — the city line is best-effort
        log.opt(exception=True).debug("profile city lookup skipped")
    city_value = html.escape(city) if city else "—"
    city_lines = [f"📍 {t('h_profile_city', lang)}: <b>{city_value}</b>"]
    if not city:
        city_lines.append(f"   <i>{t('h_profile_city_hint', lang)}</i>")

    lines = [
        t("h_profile_card_title", lang),
        f"👤 {name_link}",
        f"🆔 <code>{user.user_id}</code>",
        *city_lines,
        # ``now`` is the card's display timezone; the group-join date two
        # lines down is already converted into it (see the block above),
        # so leaving this one in raw UTC made the same card disagree with
        # itself for anyone east of Greenwich (#478).
        f"🕰 {t('h_profile_in_bot_since', lang)}: <b>{_fmt_dt(user.joined_date, now.tzinfo)}</b>",
        f"👥 {t('h_profile_in_group_since', lang)}: <b>{since_str}</b>",
        f"🎖 {t('h_profile_status', lang)}: <b>{html.escape(status_str)}</b>",
    ]
    if vip_line is not None:
        lines.append(vip_line)
    lines.append(f"📋 {t('h_profile_privileges', lang)}: <b>{html.escape(privileges)}</b>")
    if is_admin:
        lines.append(f"👮 {t('h_profile_is_group_admin', lang)}: <b>{t('h_profile_yes', lang)}</b>")
    lines.append(
        f"{t('h_profile_balance', lang)}: <b>{format_number(balance)}</b> 🪙"
        f" (~{format_number(rub)} ₽)"
    )
    lines.append("")
    lines.append(t("h_profile_messages_header", lang))
    lines.append(f"• {t('h_profile_msg_today', lang)}: <code>{today_n}</code>")
    lines.append(f"• {t('h_profile_msg_7d', lang)}: <code>{week_n}</code>")
    lines.append(f"• {t('h_profile_msg_30d', lang)}: <code>{month_n}</code>")
    lines.append(f"• {t('h_profile_msg_total', lang)}: <code>{total_n}</code>")
    # ``—`` (not 0) when the join date is unknown: zero would be a claim,
    # and the honest answer is that we don't know when to count from.
    lines.append(
        f"• {t('h_profile_msg_since_join', lang)}: "
        f"<code>{format_number(since_join) if since_join is not None else '—'}</code>"
    )

    if is_creator is False:
        try:
            async with session_for(registry, DBName.MODERATION) as msession:
                repo = ModerationRepo(msession)
                warn_count = await repo.get_warning_count(user_id=user.user_id, chat_id=chat_id)
                max_w = (await GroupModConfigRepo(msession).get_or_default(chat_id)).max_warns
                actions = await repo.recent_actions(
                    user_id=user.user_id,
                    chat_id=chat_id,
                    limit=_LOG_LIMIT,
                    days=_LOG_DAYS,
                )
            lines.append("")
            lines.append(f"⚠️ {t('h_profile_warnings', lang)}: <code>{warn_count}</code> / {max_w}")
            lines.extend(_mod_action_lines(actions, lang, now))
        except Exception:  # noqa: BLE001 — warnings are best-effort
            log.opt(exception=True).debug("profile warnings block skipped")
    return _clamp_caption(lines)


def _refresh_keyboard(owner_id: int, lang: str) -> InlineKeyboardMarkup:
    """Single owner-gated "🔄 Обновить" button carrying the card owner id."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_profile_refresh_btn", lang),
                    callback_data=ProfileRefresh(user_id=owner_id).pack(),
                )
            ]
        ]
    )


async def _render_group_card(
    user: UserEntity,
    chat_id: int,
    *,
    currency: CurrencyService | None = None,
    now: datetime,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    economy_repo: EconomyRepo,
    message_stats_repo: MessageStatsRepo,
    privileges_repo: PrivilegesRepo,
    vip_repo: VipRepo,
    effects: VipDisplayEffects | None = None,
    badge: str | None = None,
) -> tuple[bytes | None, str, InlineKeyboardMarkup]:
    """Gather live activity + balance and render the PNG + caption + keyboard.

    Returns ``(png_bytes, html_caption, inline_keyboard)`` so both the
    first render and the refresh callback share one code path — the card
    can never drift between "fresh" and "refreshed".

    ``png_bytes`` is ``None`` when the rasterisation failed. The caption
    and the keyboard are still whole, and each caller degrades in the
    way its surface allows — a first render answers as text, a refresh
    edits the caption over the stale picture.
    """
    lang = user.language
    today = now.date()
    today_n = await message_stats_repo.count_for_days(user.user_id, chat_id, days=1, today=today)
    week_n = await message_stats_repo.count_for_days(user.user_id, chat_id, days=7, today=today)
    month_n = await message_stats_repo.count_for_days(user.user_id, chat_id, days=30, today=today)
    total_n = await message_stats_repo.total_for_user(user.user_id, chat_id)
    wallet = await economy_repo.get(user.user_id)
    balance = wallet.balance if wallet is not None else 0

    rows: list[tuple[str, int]] = [
        (t("h_profile_msg_today", lang), today_n),
        (t("h_profile_msg_7d", lang), week_n),
        (t("h_profile_msg_30d", lang), month_n),
        (t("h_profile_msg_total", lang), total_n),
    ]
    try:
        # PNG generation is pure-CPU; keep the event loop free while Pillow
        # rasterises the ~1200x760 card.
        png = await asyncio.to_thread(
            render_stats_card,
            _full_name(user),
            rows,
            subtitle=t("h_profile_card_subtitle", lang),
            generated_at=now,
        )
    except Exception as exc:  # noqa: BLE001 - the card is decorative
        # Same posture as ``handlers/stats.py`` and
        # ``handlers/chatstats.py``, which have degraded to text since
        # they were ported. Pillow's failure family here is scattered
        # (OSError on a bad save target, ValueError on an unencodable
        # glyph, ``struct.error`` on a truncated TTF mid font-package
        # upgrade) and the caption built below already carries every
        # number the picture shows. Losing the picture must not lose the
        # profile.
        png = None
        log.bind(uid=user.user_id, chat_id=chat_id).warning(
            "profile card render failed: {e!r}", e=exc
        )
    caption = await _group_caption(
        user,
        chat_id,
        balance,
        currency=currency,
        now=now,
        bot=bot,
        registry=registry,
        settings=settings,
        privileges_repo=privileges_repo,
        message_stats_repo=message_stats_repo,
        vip_repo=vip_repo,
        today_n=today_n,
        week_n=week_n,
        month_n=month_n,
        total_n=total_n,
        effects=effects,
        badge=badge,
    )
    return png, caption, _refresh_keyboard(user.user_id, lang)


# --- Private-DM hub (#1/#2): main card + drill-down panels -----------------


def _foreign_tap_lang(callback: CallbackQuery, owner_id: int) -> str | None:
    """Owner-guard for profile-card callbacks.

    Returns ``None`` when the card owner tapped (the caller proceeds), or
    the tapper's best-effort ``ru``/``en`` (from their Telegram
    ``language_code``, no DB round-trip) when a bystander tapped — so the
    caller can render the refusal toast in a language they'll understand.
    Shared by the refresh + panel callbacks so the guard never drifts.
    """
    if callback.from_user is not None and callback.from_user.id == owner_id:
        return None
    code = ""
    if callback.from_user is not None:
        code = (callback.from_user.language_code or "").strip().lower()
    return "ru" if code.startswith("ru") else "en"


def _dm_hub_keyboard(user_id: int, lang: str) -> InlineKeyboardMarkup:
    """Drill-down rows under the private profile card.

    Finances and achievements sit on one row; social gets its own so the
    three labels don't get squeezed into ellipses on a narrow phone.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_profile_btn_finances", lang),
                    callback_data=ProfilePanel(panel="fin", user_id=user_id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_profile_btn_achievements", lang),
                    callback_data=ProfilePanel(panel="ach", user_id=user_id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("h_profile_btn_social", lang),
                    callback_data=ProfilePanel(panel="soc", user_id=user_id).pack(),
                ),
            ],
        ]
    )


def _panel_back_keyboard(user_id: int, lang: str) -> InlineKeyboardMarkup:
    """Single "« back to card" button shown on every drill-down panel."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_profile_btn_back", lang),
                    callback_data=ProfilePanel(panel="home", user_id=user_id).pack(),
                )
            ]
        ]
    )


async def _dm_card_text(
    user: UserEntity,
    *,
    now: datetime,
    registry: EngineRegistry,
    settings: Settings,
    economy_repo: EconomyRepo,
    privileges_repo: PrivilegesRepo,
    vip_repo: VipRepo,
    effects: VipDisplayEffects | None,
    badge: str | None,
) -> str:
    """Render the enriched private profile card body (#1).

    Each data source degrades independently — one failing read drops its
    line rather than blanking the whole card. Shared by the ``/profile``
    entry and the hub's "« back" action so both render identically.
    """
    balance: int | None = None
    privileges: str | None = None
    rank: str | None = None
    vip_line: str | None = None
    try:
        wallet = await economy_repo.get(user.user_id)
        balance = wallet.balance if wallet is not None else 0
        privileges = await _privileges_line(
            privileges_repo, user.user_id, effects, user.language, now=now
        )
        vip_till = await vip_repo.get_vip_till(user.user_id)
        vip_line = _vip_line(vip_till, now, user.language)
    except Exception:  # noqa: BLE001 — dashboard lines are best-effort
        log.opt(exception=True).warning("/profile DM extras failed")
    try:
        rank_level = await RankService(registry, settings).get_rank(user.user_id)
        rank = rank_name(rank_level, user.language, in_group=False)
    except Exception:  # noqa: BLE001 — rank is best-effort too
        log.opt(exception=True).warning("/profile DM rank failed")
    return _format_text(
        user,
        effects,
        badge,
        balance=balance,
        privileges=privileges,
        rank=rank,
        vip_line=vip_line,
    )


async def _panel_finances(
    user: UserEntity,
    *,
    now: datetime,
    economy_repo: EconomyRepo,
    transactions_repo: TransactionsRepo,
) -> str:
    """💰 Finances drill-down: balance, 7d/30d cashflow, last-5 ledger rows.

    ``now`` is the card's display timezone, used only to render the
    ledger day stamps; the cashflow windows below stay in naive UTC
    because that is the frame ``transactions.date`` is stored in.
    """
    lang = user.language
    wallet = await economy_repo.get(user.user_id)
    balance = wallet.balance if wallet is not None else 0
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    week = await transactions_repo.window_stats(user.user_id, since=now_utc - timedelta(days=7))
    month = await transactions_repo.window_stats(user.user_id, since=now_utc - timedelta(days=30))
    recent = await transactions_repo.recent(user.user_id, limit=5)
    lines = [
        t("h_profile_fin_title", lang),
        "",
        t("h_profile_fin_balance", lang, balance=format_number(balance)),
        t(
            "h_profile_fin_week",
            lang,
            recv=format_number(week.received),
            sent=format_number(week.sent),
            n=week.tx_count,
        ),
        t(
            "h_profile_fin_month",
            lang,
            recv=format_number(month.received),
            sent=format_number(month.sent),
            n=month.tx_count,
        ),
        "",
        t("h_profile_fin_recent_header", lang),
    ]
    if recent:
        for tx in recent:
            sign = "➕" if tx.signed_amount >= 0 else "➖"
            # #478 is why a conversion happens at all: at 02:00 MSK an
            # untranslated stamp names yesterday.
            #
            # #1475: the column holds a naive stamp in one of TWO
            # frames and carries nothing to tell them apart. The new
            # pipeline writes UTC (:meth:`TransactionsRepo.record`);
            # legacy wrote a bare ``datetime.now()``, which on this
            # host is MSK. The line below is right for the first and
            # adds three hours to the second, so a pre-cutover row
            # timed after 21:00 MSK names the following day. This
            # comment used to say "naive UTC in the column" flatly,
            # which is the half of it that is convenient.
            #
            # Still converting unconditionally rather than branching on
            # a guessed cutover instant: a constant that landed on the
            # wrong side would move rows that are currently right, and
            # this is a money table. :func:`_mod_action_lines` carries
            # the identical ambiguity and gets away with it because its
            # 90-day window ages the legacy rows out on its own;
            # ``recent()`` is a top-five rather than a window, so an
            # inactive user's card keeps showing them indefinitely. The
            # repair for that is a one-off backfill, decided and run
            # once against the real data — not a branch guessing here
            # on every render.
            when = tx.date.replace(tzinfo=UTC).astimezone(now.tzinfo).strftime("%d.%m")
            label = _tx_label(tx.reason, tx.type, lang)
            lines.append(
                f"{sign} <b>{format_number(abs(tx.signed_amount))}</b> 🪙 · {label} · <i>{when}</i>"
            )
    else:
        lines.append(t("h_profile_fin_recent_none", lang))
    return "\n".join(lines)


async def _panel_achievements(user: UserEntity, *, achievements_repo: AchievementsRepo) -> str:
    """🏆 Achievements drill-down — the exact /achievements card body
    (shared :func:`render_achievements_body`, so the two never drift)."""
    earned = await achievements_repo.earned_for_user(user.user_id)
    stats = await achievements_repo.stats_for_user(user.user_id)
    return render_achievements_body(
        lang=user.language,
        earned=earned,
        balance=stats.balance,
        games_played=stats.games_played,
        games_won=stats.games_won,
    )


async def _social_partner_name(session: AsyncSession, user_id: int) -> str:
    """Display name for a bond partner / inviter, or an ``ID<n>`` stub.

    Mirrors ``handlers/referrals._fetch_names``' fallback: a wallet or a
    bond row outlives the profile row it points at (the other party can
    wipe their account and keep the marriage), so a missing name must
    degrade to something tappable rather than blanking the line.
    """
    name = await session.scalar(select(User.first_name).where(User.user_id == user_id))
    return (name or "").strip() or f"ID{user_id}"


def _bond_lines(marriages: list[UserBond], relationships: list[UserBond], lang: str) -> list[str]:
    """Render the bonds block, marriages first, then relationships.

    Both lists arrive capped at ``_SOC_BOND_FETCH`` so the hidden-count
    footer is exact without a second COUNT per kind.
    """
    lines: list[str] = []
    hidden = 0
    capped = False
    # ``created_at`` is TEXT for legacy-migrated rows and is still written
    # by the live legacy process, so it is not ours to trust: a ``<`` in
    # the first ten characters would fail the whole ``edit_text``, taking
    # the card down rather than one line (same class of hole closed on the
    # voice-settings card in #73).
    for bond in marriages[:_SOC_BOND_LIMIT]:
        level = marriage_xp_to_level(bond.experience)
        lines.append(
            t(
                "h_profile_soc_marriage",
                lang,
                mention=html_user_mention(
                    bond.partner_id, bond.partner_name or f"ID{bond.partner_id}"
                ),
                level_name=marriage_level_name(level, lang),
                since=html.escape(format_db_date(bond.created_at)),
            )
        )
    hidden += max(0, len(marriages) - _SOC_BOND_LIMIT)
    capped = capped or len(marriages) >= _SOC_BOND_FETCH
    for bond in relationships[:_SOC_BOND_LIMIT]:
        lines.append(
            t(
                "h_profile_soc_relationship",
                lang,
                mention=html_user_mention(
                    bond.partner_id, bond.partner_name or f"ID{bond.partner_id}"
                ),
                abbr=t("h_relations_level_abbr", lang),
                level=relationship_xp_to_level(bond.experience),
                since=html.escape(format_db_date(bond.created_at)),
            )
        )
    hidden += max(0, len(relationships) - _SOC_BOND_LIMIT)
    capped = capped or len(relationships) >= _SOC_BOND_FETCH
    if not lines:
        return [t("h_profile_soc_bonds_none", lang)]
    if hidden:
        # ``45+`` when the fetch cap bit, so the number is never a claim we
        # cannot back — the read stopped counting, and the card says so.
        shown = f"{hidden}+" if capped else str(hidden)
        lines.append(t("h_profile_soc_bonds_more", lang, n=shown))
    return lines


async def _panel_social(user: UserEntity, *, registry: EngineRegistry, settings: Settings) -> str:
    """👥 Social drill-down: referral chain, commission, bonds (RR-1 #2).

    Legacy split this across four separate profile buttons (``referrals``,
    ``commission``, ``relations``, ``marriage``) and refused three of them
    outside a group, because every bond query it had was chat-scoped. The
    hub is a DM, so instead of porting that refusal we ask the bonds repos
    a cross-chat question and show the user every bond they hold at once.

    Chat titles are deliberately absent: resolving them means a
    ``BotGroup`` lookup that misses for groups the bot was added to before
    it tracked titles, and it would surface group names into a DM that the
    partner never chose to share there. The partner mention alone is what
    the user actually asked for.
    """
    lang = user.language
    async with session_for(registry, DBName.ECONOMY) as session:
        referrals = ReferralsRepo(session)
        invited = await referrals.count_invitees(user.user_id)
        earned = await referrals.fetch_earnings(user.user_id)
        inviter_id = await referrals.fetch_inviter(user.user_id)
        second_level = await referrals.count_second_level(user.user_id)
    async with session_for(registry, DBName.USERS) as session:
        # Fetch wide, render narrow — see :data:`_SOC_BOND_FETCH`.
        marriages = await MarriagesRepo(session).list_for_user(user.user_id, limit=_SOC_BOND_FETCH)
        relationships = await RelationshipsRepo(session).list_for_user(
            user.user_id, limit=_SOC_BOND_FETCH
        )
        inviter_line = (
            t(
                "h_profile_soc_ref_inviter",
                lang,
                mention=html_user_mention(
                    inviter_id, await _social_partner_name(session, inviter_id)
                ),
            )
            if inviter_id is not None
            else t("h_profile_soc_ref_inviter_none", lang)
        )
    lines = [
        t("h_profile_soc_title", lang),
        "",
        t("h_profile_soc_ref_header", lang),
        inviter_line,
        t("h_profile_soc_ref_invited", lang, n=format_number(invited)),
    ]
    if second_level > 0:
        lines.append(t("h_profile_soc_ref_reach", lang, n=format_number(second_level)))
    lines += [
        t("h_profile_soc_ref_earned", lang, earned=format_number(earned)),
        t(
            "h_profile_soc_ref_rate",
            lang,
            percent=settings.economy.referral_commission_percent,
        ),
    ]
    if invited == 0:
        lines.append(t("h_profile_soc_ref_cta", lang))
    lines += ["", t("h_profile_soc_bonds_header", lang)]
    lines += _bond_lines(marriages, relationships, lang)
    return "\n".join(lines)


# --- Cross-user profile (#4): /profile @user | /profile <id> | reply --------


def _win_rate(played: int, won: int) -> int:
    """Integer win-% with a division-by-zero guard (0 games → 0%)."""
    return round(won * 100 / played) if played > 0 else 0


async def _resolve_target(arg: str, users_repo: UsersRepo) -> UserEntity | None:
    """Resolve a ``/profile`` argument to a stored user, or ``None``.

    Accepts ``@username``, a bare ``username``, or a numeric id. Only
    users with a stored row resolve — there is no way to surface a card
    for someone the bot has never seen, which also bounds id-enumeration
    to "does this id have a row" (the card itself exposes only public
    game/achievement stats, never the wallet — see :func:`_format_other`).
    """
    arg = arg.strip()
    if not arg:
        return None
    if arg.startswith("@"):
        return await users_repo.get_by_username(arg)
    target_id = parse_int_token(arg, signed=True)
    if target_id is not None:
        return await users_repo.get(target_id)
    return await users_repo.get_by_username(arg)


def _format_other(
    target: UserEntity,
    *,
    lang: str,
    rank: str,
    earned: int,
    games_played: int,
    games_won: int,
) -> str:
    """A read-only card for ANOTHER user (#4), rendered in the *viewer's*
    language.

    Privacy boundary: this shows only inherently-public signals — display
    name, id, rank, game record (played / won / win-rate) and the
    achievement count. It deliberately OMITS the wallet balance and any
    transaction history, which are private to the owner's own /profile
    hub. Legacy surfaced an earned/spent line here; we drop it as a
    wallet-privacy leak.
    """
    username = f"@{target.username}" if target.username else "—"
    return "\n".join(
        [
            t("h_profile_other_title", lang),
            "",
            f"{t('h_profile_name', lang)}: <b>{html.escape(_full_name(target))}</b>",
            f"{t('h_profile_id', lang)}: <code>{target.user_id}</code>",
            f"{t('h_profile_username', lang)}: {html.escape(username)}",
            f"🎖 {t('h_profile_status', lang)}: <b>{html.escape(rank)}</b>",
            "",
            t(
                "h_profile_other_games",
                lang,
                played=format_number(games_played),
                won=format_number(games_won),
                rate=_win_rate(games_played, games_won),
            ),
            t("h_profile_other_achievements", lang, earned=earned, total=_ACH_TOTAL),
        ]
    )


def build_router(
    registry: EngineRegistry,
    stats_config: StatsConfig,
    settings: Settings,
    currency: CurrencyService | None = None,
) -> Router:
    """Factory — fresh ``Router`` + middleware per call so tests can re-wire.

    ``stats_config.timezone`` is captured by the inner handlers' closure
    so "today" is computed against the same calendar boundary the
    chat-wide ``/stats`` card uses — no drift between the two surfaces.

    ``currency`` is the shared, 1h-cached FX service. It is optional so a
    test can build the router without an HTTP-backed dependency; the card
    then prices coins off the offline anchor instead of a live quote.
    """
    tz = ZoneInfo(stats_config.timezone)

    async def handle_profile(
        message: Message,
        bot: Bot,
        user_service: UserService,
        economy_repo: EconomyRepo,
        message_stats_repo: MessageStatsRepo,
        privileges_repo: PrivilegesRepo,
        emoji_badge_service: EmojiBadgeService,
        vip_repo: VipRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        now = datetime.now(tz)
        user = await user_service.touch(require_from_user(message))
        # The touch is the only write this card makes, and the price
        # line below may go out to the FX provider (10 s timeout) on a
        # cold cache. End the write transaction here so users.db isn't
        # locked for that wait — see :class:`db.session.Checkpoint`.
        if checkpoint is not None:
            await checkpoint()
        # Resolve the caller's owned cosmetic VIP effects (color nick /
        # custom title / legend) once, against the same ``now`` the card
        # uses, and thread the bundle through both surfaces (L-36).
        effects = await VipDisplayService(privileges_repo).resolve(user.user_id, now=now)
        # VIP cosmetic emoji badge (#25, /emoji_set). ``safe_active_badge``
        # is the render-time gate (only shown while currently VIP) AND
        # degrades to ``None`` when the economy schema is absent — the
        # private text path below runs against a users-only session, so a
        # missing ``user_emoji_badge`` table must not 500 the card.
        badge = await emoji_badge_service.safe_active_badge(user.user_id, now=now)
        chat = message.chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            png, caption, keyboard = await _render_group_card(
                user,
                chat.id,
                currency=currency,
                now=now,
                bot=bot,
                registry=registry,
                settings=settings,
                economy_repo=economy_repo,
                message_stats_repo=message_stats_repo,
                privileges_repo=privileges_repo,
                vip_repo=vip_repo,
                effects=effects,
                badge=badge,
            )
            if png is None:
                await message.answer(caption, reply_markup=keyboard)
            else:
                await message.answer_photo(
                    BufferedInputFile(png, filename="profile.png"),
                    caption=caption,
                    reply_markup=keyboard,
                )
            log.bind(uid=user.user_id, chat_id=chat.id).info(
                "/profile preview rendered as {mode}", mode="text" if png is None else "png"
            )
            return
        # RR-1 #1: the enriched DM card (rank / VIP / privileges / balance)
        # plus the drill-down hub (#2). Shared renderer so the hub's
        # "« back" rebuilds the identical body.
        text = await _dm_card_text(
            user,
            now=now,
            registry=registry,
            settings=settings,
            economy_repo=economy_repo,
            privileges_repo=privileges_repo,
            vip_repo=vip_repo,
            effects=effects,
            badge=badge,
        )
        await message.answer(text, reply_markup=_dm_hub_keyboard(user.user_id, user.language))
        log.bind(uid=user.user_id, lang=user.language).info("/profile text rendered")

    async def handle_refresh(
        callback: CallbackQuery,
        callback_data: ProfileRefresh,
        bot: Bot,
        user_service: UserService,
        economy_repo: EconomyRepo,
        message_stats_repo: MessageStatsRepo,
        privileges_repo: PrivilegesRepo,
        emoji_badge_service: EmojiBadgeService,
        vip_repo: VipRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        foreign_lang = _foreign_tap_lang(callback, callback_data.user_id)
        if foreign_lang is not None:
            # A bystander tapped someone else's card — refuse without
            # leaking the owner's fresh stats.
            await callback.answer(t("h_profile_refresh_foreign", foreign_lang), show_alert=False)
            return
        assert callback.from_user is not None  # _foreign_tap_lang None ⇒ owner
        user = await user_service.touch(callback.from_user)
        # Same posture as ``handle_profile``: the touch is final, the
        # re-render may wait on the FX provider.
        if checkpoint is not None:
            await checkpoint()
        message = callback.message
        # ``callback.message`` is ``Message | InaccessibleMessage | None``;
        # only a live ``Message`` can be edited (an aged-out card surfaces
        # as ``InaccessibleMessage``). Acknowledge and bail otherwise.
        if not isinstance(message, Message) or message.chat is None:
            await callback.answer()
            return
        now = datetime.now(tz)
        effects = await VipDisplayService(privileges_repo).resolve(user.user_id, now=now)
        badge = await emoji_badge_service.safe_active_badge(user.user_id, now=now)
        png, caption, keyboard = await _render_group_card(
            user,
            message.chat.id,
            currency=currency,
            now=now,
            bot=bot,
            registry=registry,
            settings=settings,
            economy_repo=economy_repo,
            message_stats_repo=message_stats_repo,
            privileges_repo=privileges_repo,
            vip_repo=vip_repo,
            effects=effects,
            badge=badge,
        )
        # "message is not modified" (identical card within the same
        # minute) or the message aged out of edit range — neither is
        # actionable; the toast still tells the user we tried.
        with contextlib.suppress(TelegramBadRequest):
            if png is None:
                # A photo message cannot become a text one, so the stale
                # picture stays on screen — but the numbers the tap asked
                # for are replaced, which is the part that was stale.
                await message.edit_caption(caption=caption, reply_markup=keyboard)
            else:
                await message.edit_media(
                    InputMediaPhoto(
                        media=BufferedInputFile(png, filename="profile.png"),
                        caption=caption,
                    ),
                    reply_markup=keyboard,
                )
        await callback.answer(t("h_profile_refreshed", user.language))
        log.bind(uid=user.user_id, chat_id=message.chat.id).info("/profile preview refreshed")

    async def handle_panel(
        callback: CallbackQuery,
        callback_data: ProfilePanel,
        user_service: UserService,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        achievements_repo: AchievementsRepo,
        privileges_repo: PrivilegesRepo,
        vip_repo: VipRepo,
        emoji_badge_service: EmojiBadgeService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        """Drill-down + "« back" taps on the private profile hub (#2).

        Owner-guarded exactly like :func:`handle_refresh` — a bystander's
        tap on a forwarded card is refused without leaking the owner's
        data. Each panel edits the card text in place and swaps the
        keyboard (back-button on a panel, the hub row on the home card).
        """
        foreign_lang = _foreign_tap_lang(callback, callback_data.user_id)
        if foreign_lang is not None:
            await callback.answer(t("h_profile_refresh_foreign", foreign_lang), show_alert=False)
            return
        assert callback.from_user is not None  # _foreign_tap_lang None ⇒ owner
        message = callback.message
        if not isinstance(message, Message):
            await callback.answer()
            return
        user = await user_service.touch(callback.from_user)
        # The touch is final; the home panel prices coins off the FX
        # provider, which can take seconds on a cold cache.
        if checkpoint is not None:
            await checkpoint()
        now = datetime.now(tz)
        # Each panel reads its own data sources; wrap the render so a missing
        # economy/achievements schema degrades to a friendly notice (mirrors
        # the home card's per-source best-effort contract) instead of a 500.
        try:
            if callback_data.panel == "home":
                effects = await VipDisplayService(privileges_repo).resolve(user.user_id, now=now)
                badge = await emoji_badge_service.safe_active_badge(user.user_id, now=now)
                body = await _dm_card_text(
                    user,
                    now=now,
                    registry=registry,
                    settings=settings,
                    economy_repo=economy_repo,
                    privileges_repo=privileges_repo,
                    vip_repo=vip_repo,
                    effects=effects,
                    badge=badge,
                )
                markup = _dm_hub_keyboard(user.user_id, user.language)
            elif callback_data.panel == "fin":
                body = await _panel_finances(
                    user,
                    now=now,
                    economy_repo=economy_repo,
                    transactions_repo=transactions_repo,
                )
                markup = _panel_back_keyboard(user.user_id, user.language)
            elif callback_data.panel == "soc":
                body = await _panel_social(user, registry=registry, settings=settings)
                markup = _panel_back_keyboard(user.user_id, user.language)
            else:  # "ach"
                body = await _panel_achievements(user, achievements_repo=achievements_repo)
                markup = _panel_back_keyboard(user.user_id, user.language)
        except Exception:  # noqa: BLE001 — a panel must never 500 the card
            log.opt(exception=True).warning("/profile panel render failed")
            body = t("h_profile_panel_unavailable", user.language)
            markup = _panel_back_keyboard(user.user_id, user.language)
        # Re-opening the panel already on screen renders the same
        # bytes; the ``except Exception`` above already turned a broken
        # panel into a rendered notice, so anything left is Telegram
        # telling us the card is unchanged or gone.
        await edit_card(message, body, reply_markup=markup)
        await callback.answer()
        log.bind(uid=user.user_id, panel=callback_data.panel).info("/profile panel shown")

    async def _render_other(
        target: UserEntity,
        viewer_lang: str,
        achievements_repo: AchievementsRepo,
    ) -> str:
        """Gather a target's public stats + rank, render the cross-user card."""
        try:
            rank_level = await RankService(registry, settings).get_rank(target.user_id)
            rank = rank_name(rank_level, viewer_lang, in_group=False)
        except Exception:  # noqa: BLE001 — rank is best-effort
            rank = rank_name(RankLevel.USER, viewer_lang, in_group=False)
        earned = await achievements_repo.earned_for_user(target.user_id)
        stats = await achievements_repo.stats_for_user(target.user_id)
        return _format_other(
            target,
            lang=viewer_lang,
            rank=rank,
            earned=len(earned),
            games_played=stats.games_played,
            games_won=stats.games_won,
        )

    async def handle_other_profile(
        message: Message,
        command: CommandObject,
        user_service: UserService,
        users_repo: UsersRepo,
        achievements_repo: AchievementsRepo,
    ) -> None:
        """``/profile @user`` / ``/profile <id>`` — a read-only card for
        another user (#4). Renders in the *viewer's* language; exposes only
        public game/achievement stats (no wallet — see _format_other)."""
        viewer = await user_service.touch(require_from_user(message))
        lang = viewer.language
        arg = command.args.split()[0] if command.args else ""
        target = await _resolve_target(arg, users_repo)
        if target is None:
            await message.answer(t("h_profile_other_not_found", lang))
            return
        body = await _render_other(target, lang, achievements_repo)
        await message.answer(body)
        log.bind(viewer=viewer.user_id, target=target.user_id).info("/profile cross-user rendered")

    router = Router(name="profile")
    # Group preview needs economy.db (balance) + message_stats.db
    # (activity); mount on BOTH observers so the refresh callback sees
    # the same repos as the initial render. The private text path opens
    # these sessions but never queries them (lazy — no table needed).
    router.message.middleware(EconomyMiddleware(registry))
    router.message.middleware(MessageStatsMiddleware(registry))
    router.callback_query.middleware(EconomyMiddleware(registry))
    router.callback_query.middleware(MessageStatsMiddleware(registry))
    _PROFILE_ALIASES = (
        "profile",
        "info",
        "профиль",
        "инфо",
        "whoami",
        "me",
        "kom_whoami",
        # ``kom_profile``: the multi-bot spelling legacy registered and
        # the catalog still advertises — see handlers/economy.py.
        "kom_profile",
    )
    router.message.register(
        handle_profile,
        Command(*_PROFILE_ALIASES, ignore_case=True, magic=F.args.is_(None)),
        F.from_user,
    )
    # #4: ``/profile @user`` / ``/profile <id>`` — args present diverts to the
    # cross-user card. Mutually exclusive with the self handler above
    # (magic is_(None) vs. is_not(None)), so registration order is irrelevant.
    router.message.register(
        handle_other_profile,
        Command(*_PROFILE_ALIASES, ignore_case=True, magic=F.args.is_not(None)),
        F.from_user,
    )
    router.callback_query.register(handle_refresh, ProfileRefresh.filter())
    router.callback_query.register(handle_panel, ProfilePanel.filter())
    return router
