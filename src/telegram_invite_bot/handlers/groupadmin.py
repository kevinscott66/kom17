"""/groupadmin — group control card (L-42 + the read half of L-53).

Legacy ``cmd_groupadmin`` (bot.py:32279-32320, triggers ``groupadmin`` /
``group_admin`` / ``управлениегруппой``) opens a Markdown menu whose
buttons deep-link into write-side FSM flows (banned-words editor,
settings, stats, staff — bot.py:32302-32309 and the roles menu at
bot.py:27141-27282). Its header already aggregates live counters
(profanity-word count, active warnings, automod flag, effective warn
limit — bot.py:32311-32317).

The L-42 port keeps the aggregation idea and re-shapes the surface:

* ONE read-only CONTROL CARD per group, sections rendered from the
  per-group stores the already-shipped subsystems own:

  1. moderation config — ``GroupModConfigRepo.get_or_default``
     (handlers/modcfg.py), edit via /modcfg;
  2. word filter — ``WordFilterRepo.list`` count
     (handlers/wordfilter.py), list via /filter_list;
  3. welcome — ``WelcomeConfigRepo.get`` (handlers/group_events.py),
     edit via /setwelcome;
  4. aliases — ``GroupAliasRepo.list`` count
     (handlers/group_aliases.py), list via /alias list;
  5. treasury — ``groups_donations`` balance + group XP (the same
     aggregate ``handlers/group_pay.py`` / ``handlers/groupstats.py``
     read), payout via /group_pay;
  6. rules — ``group_settings.rules`` set/unset (handlers/rules.py),
     edit via /setrules.

* NO write-side FSMs here — every subsystem already has a dedicated
  edit command; the section buttons only re-render the card (the
  constructor write-half of L-53 is intentionally out of scope).

RR-4 #35/#36/#37 — the sub-pages
--------------------------------

Legacy's panel had four sub-pages behind its buttons (words / settings /
stats / staff, bot.py:32302-32309). The L-42 port collapsed all of them
into "re-render the overview", so none was reachable. Three are back:

* **Settings** (#36) — the six boolean gates as inline toggles plus
  numeric pickers for the warn limit and the mute duration. This is the
  panel's only write path, and it is deliberately narrower than legacy's:
  legacy's toggles assigned to module-global ``AUTO_MODERATE`` /
  ``MAX_WARNINGS`` and saved ``settings.json`` (bot.py:30883-30901), so
  ONE group's admin silently re-configured moderation for EVERY group the
  bot served. Ours writes ``group_mod_config`` for the group the card was
  posted in, and nothing else.

  Legacy's profanity-STRENGTH picker (bot.py:30983) is not restored: it
  scaled a fuzzy-matching threshold that has no counterpart here — the
  port's filter is a deterministic per-group word list — so the dial
  would move nothing. Restoring the widget without the mechanism would
  be worse than its absence.

* **Stats** (#37) — active warnings, per-action totals and the last five
  moderation-log entries. The log has been written since T-020 and was
  never displayed anywhere until now.

* **Words** (#35 + #43) — the group's filter list, with ➕ to add a word
  and a per-word delete grid. Two legacy bugs do not come back with it:
  legacy's ``profanity_filter`` was ONE process-wide list, so a word
  banned from any group's panel was banned everywhere (bot.py:32328);
  and its delete buttons put the word itself in ``callback_data``
  (bot.py:32386), which Telegram caps at 64 bytes — a long or Cyrillic
  word made the keyboard unbuildable. Ours writes ``word_filters`` for
  the card's own group and its buttons carry the row id.

* **Staff** (#38) — who holds power in this group and why, plus grant /
  demote. Legacy printed ``name — rank — id`` and nothing more; the
  rows here also mark whether the person moderates through a Telegram
  admin badge or through a bot rank, which are separate mechanisms in
  this bot. Demote became a per-person button instead of legacy's
  "type a raw user id into the chat", and every write re-checks the
  permission, the actor's ceiling and the target's CURRENT rank —
  guards legacy's staff panel did not have at all.

Gate: legacy called ``has_group_admin_rights`` (bot.py:32291), whose
body (bot.py:7555-7565) is ``user_id in DEVELOPER_IDS`` → group chat →
``is_telegram_group_admin``. There is NO rank path in it. The rank
path belongs to a DIFFERENT legacy helper, ``require_group_moderation``
(bot.py:7568-7577), which the moderation COMMANDS use — and it reaches
the ranks through ``check_permission_and_reply``, i.e. the permission
matrix, not a bare level threshold.

#670: an earlier revision of this paragraph claimed the legacy gate was
"live TG admin OR ranked staff" and that DESIGN_RANKS.md §2.2 mapped it
to "live Telegram admin OR global rank >= 4", and the code carried a
matching ``RankService.get_rank >= ADMIN`` branch. Both halves were
false. §2.2 specifies ``rank_service.check(actor, chat, PERMISSION)``
against the matrix, and it does not cover this command at all —
DESIGN_RANKS.md:128 defers ``/groupadmin`` to "wave 2 of the epic".
So the branch was an unsanctioned widening, and a load-bearing one:
this panel WRITES (moderation toggles, the word list, staff grants and
demotes), ranks are GLOBAL, and the branch never looked at the chat.
Any global rank-4/5 holder could therefore reconfigure moderation in
every group the bot sits in, including groups they have no standing in
and legacy would have refused them.

Now implemented as developer → live TG-admin
(``utils.telegram_admin.is_user_admin``, R-FIX-007 fail-closed posture
preserved: API error never grants), which is legacy's sequence exactly.
Anonymous-admin actors delegate to the unchanged ``_require_admin``
policy (R-FIX-011). Denials reuse the existing
``h_mod_no_permission`` / ``h_mod_retry_later`` copy.

#674 — one legacy gate IS dropped, and deliberately: legacy also ran
``require_group_feature(message, "group_admin", …)`` (bot.py:32285),
the per-group ``group_settings.features_mode`` opt-out that lets a
chat run "AI only". That whole mechanism is unported across the
pipeline — ``handlers/daily.py``, ``handlers/games.py``,
``handlers/send.py``, ``handlers/help.py`` and ``handlers/timezone.py``
each say so about their own command. This one had not, which is the
only reason it is worth a paragraph: the omission was invisible here
while its siblings disclosed it.

Sessions: short reads via ``session_for`` (moderation.db for sections
1-4, economy.db for 5, users.db engine for 6) — each section is
fetched best-effort and degrades to an "unavailable" line on error so
one broken store never blanks the whole card.
"""

from __future__ import annotations

import contextlib
import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.core.moderation_reasons import reason_html
from telegram_invite_bot.core.ranks import RankLevel, rank_name
from telegram_invite_bot.db.models.economy import GroupDonationsAggregate
from telegram_invite_bot.db.models.users import GroupSettings
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.fsm.group_staff import GroupStaffStates
from telegram_invite_bot.fsm.group_words import GroupWordsStates
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND
from telegram_invite_bot.handlers.moderation import _is_chat_backed_actor, _require_admin
from telegram_invite_bot.handlers.wordfilter import add_refusal, validate_word
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.groupadmin import (
    PAGE_SETTINGS,
    PAGE_STAFF,
    PAGE_STAFF_DROP,
    PAGE_STATS,
    PAGE_WORDS,
    PAGE_WORDS_DROP,
    PICKER_FIELDS,
    TOGGLE_FIELDS,
    GroupAdminPick,
    GroupAdminRefresh,
    GroupAdminSet,
    GroupAdminStaffAdd,
    GroupAdminStaffDrop,
    GroupAdminWordAdd,
    GroupAdminWordDrop,
    build_card_markup,
    build_picker_markup,
    build_settings_markup,
    build_staff_drop_markup,
    build_staff_markup,
    build_subpage_markup,
    build_words_drop_markup,
    build_words_markup,
    format_mute_choice,
    picker_choices,
    resolve_page,
)
from telegram_invite_bot.repositories.group_aliases_repo import GroupAliasRepo
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.repositories.welcome_config_repo import WelcomeConfigRepo
from telegram_invite_bot.repositories.word_filter_repo import WordFilterRepo
from telegram_invite_bot.services.rank_service import RankService
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.numbers import parse_int_token
from telegram_invite_bot.utils.plural import plural
from telegram_invite_bot.utils.render import clamp_utf16, state_mark
from telegram_invite_bot.utils.telegram_admin import is_user_admin

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import InlineKeyboardMarkup

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigView
    from telegram_invite_bot.repositories.moderation_repo import ActionRow
    from telegram_invite_bot.repositories.users_repo import RankedUser
    from telegram_invite_bot.repositories.word_filter_repo import WordEntry

log = logger.bind(component="handlers.groupadmin")


# State marks live in utils.render: the settings KEYBOARD renders them
# too, and a button label that disagreed with the card line above it
# would be worse than no label at all.
_mark = state_mark


# ---------------------------------------------------------------------------
# Gate: developer OR live TG-admin — legacy ``has_group_admin_rights``
# (bot.py:7555-7565) exactly. NOT a rank threshold; see the module
# docstring and #670 for why the rank branch was removed.
# ---------------------------------------------------------------------------


async def _actor_allowed(
    bot: Bot,
    settings: Settings,
    *,
    chat_id: int,
    user_id: int,
) -> bool | None:
    """Tri-state gate verdict for a non-anonymous actor.

    True → allowed, False → not allowed, ``None`` → the live-admin probe
    failed (caller renders "retry later", preserving the R-FIX-007
    fail-closed posture).

    #670: there is no rank branch. A global rank says nothing about
    standing in THIS chat, and this panel writes.
    """
    if settings.bot.is_developer(user_id):
        return True
    status = await is_user_admin(bot, chat_id, user_id)
    if status is True:
        return True
    return None if status is None else False


async def _gate_message(
    message: Message,
    bot: Bot,
    settings: Settings,
    lang: str,
) -> bool:
    """Reply with the denial and return False when the caller may not open the card."""
    if _is_chat_backed_actor(message):
        # An actor that is a chat, not a person, has no per-user rank to
        # look up — delegate to the R-FIX-011 policy, which admits a
        # genuine anonymous admin of this chat, refuses a foreign
        # ``sender_chat`` (#246), and replies on refusal itself.
        return await _require_admin(message, bot, settings, lang)
    tg_user = require_from_user(message)
    verdict = await _actor_allowed(bot, settings, chat_id=message.chat.id, user_id=tg_user.id)
    if verdict is True:
        return True
    if verdict is None:
        await message.reply(t("h_mod_retry_later", lang))
        return False
    await message.reply(t("h_mod_no_permission", lang))
    return False


async def _gate_callback(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
    *,
    chat_id: int,
    lang: str,
) -> bool:
    """Same gate for button taps; denial is an alert, not a chat reply.

    Callback taps always carry the REAL clicking user (Telegram does
    not anonymise ``callback.from_user``), so no anonymous branch.
    """
    verdict = await _actor_allowed(bot, settings, chat_id=chat_id, user_id=callback.from_user.id)
    if verdict is True:
        return True
    key = "h_mod_retry_later" if verdict is None else "h_mod_no_permission"
    await callback.answer(t(key, lang), show_alert=True)
    return False


# ---------------------------------------------------------------------------
# Snapshot: best-effort reads over the per-group stores
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupAdminSnapshot:
    """One read-only sweep over the six per-group stores.

    Every field is ``None`` when its read failed — the renderer shows
    a per-section "unavailable" line instead of dropping the card.
    """

    modcfg: GroupModConfigView | None
    filter_count: int | None
    welcome: tuple[bool, bool] | None  # (enabled, template_set)
    alias_count: int | None
    treasury: tuple[int, int] | None  # (balance, group_xp)
    rules_set: bool | None
    active_warns: int | None = None  # RR-4 #44: live active-warnings count


async def _fetch_moderation_sections(
    registry: EngineRegistry, group_id: int
) -> tuple[
    GroupModConfigView | None,
    int | None,
    tuple[bool, bool] | None,
    int | None,
    int | None,
]:
    """Sections 1-4 share one short moderation.db session; each read is
    individually guarded so e.g. a broken ``welcome_config`` table does
    not blank the mod-config line."""
    modcfg: GroupModConfigView | None = None
    filter_count: int | None = None
    welcome: tuple[bool, bool] | None = None
    alias_count: int | None = None
    active_warns: int | None = None
    try:
        async with session_for(registry, DBName.MODERATION) as session:
            try:
                modcfg = await GroupModConfigRepo(session).get_or_default(group_id)
            except Exception:  # noqa: BLE001 — degrade to the unavailable line
                log.opt(exception=True).warning("groupadmin: modcfg read failed")
            try:
                # RR-4 #44: live active-warnings count for the header.
                active_warns = await ModerationRepo(session).count_active_warnings_in_chat(
                    chat_id=group_id
                )
            except Exception:  # noqa: BLE001
                log.opt(exception=True).warning("groupadmin: warns count failed")
            try:
                filter_count = len(await WordFilterRepo(session).list(group_id=group_id))
            except Exception:  # noqa: BLE001
                log.opt(exception=True).warning("groupadmin: word-filter read failed")
            try:
                row = await WelcomeConfigRepo(session).get(group_id)
                if row is None:
                    welcome = (False, False)
                else:
                    welcome = (row.enabled, bool((row.template or "").strip()))
            except Exception:  # noqa: BLE001
                log.opt(exception=True).warning("groupadmin: welcome read failed")
            try:
                alias_count = len(await GroupAliasRepo(session).list(group_id=group_id))
            except Exception:  # noqa: BLE001
                log.opt(exception=True).warning("groupadmin: alias read failed")
    except Exception:  # noqa: BLE001 — session open itself failed
        log.opt(exception=True).warning("groupadmin: moderation.db session failed")
    return modcfg, filter_count, welcome, alias_count, active_warns


async def _fetch_treasury(registry: EngineRegistry, group_id: int) -> tuple[int, int] | None:
    """Section 5: ``groups_donations`` balance + group XP (economy.db).

    A group without a ``groups_donations`` row has an empty treasury —
    rendered as 0/0, matching how /group_pay treats the missing row
    (rejects the payout exactly like a zero balance, bot.py:10917).
    """
    try:
        async with session_for(registry, DBName.ECONOMY) as session:
            result = await session.execute(
                select(
                    GroupDonationsAggregate.total_donations,
                    GroupDonationsAggregate.group_xp,
                ).where(GroupDonationsAggregate.group_id == group_id)
            )
            row = result.first()
    except Exception:  # noqa: BLE001
        log.opt(exception=True).warning("groupadmin: treasury read failed")
        return None
    if row is None:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


async def _fetch_rules_set(registry: EngineRegistry, group_id: int) -> bool | None:
    """Section 6: is ``group_settings.rules`` non-empty? (users.db)

    Missing row / NULL / blank all collapse to "not set" — the same
    conflation ``handlers.rules._fetch_rules`` makes (bot.py:32118).
    """
    try:
        async with session_for(registry, DBName.USERS) as session:
            value = (
                await session.execute(
                    select(GroupSettings.rules).where(GroupSettings.group_id == group_id)
                )
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        log.opt(exception=True).warning("groupadmin: rules read failed")
        return None
    return bool(value and value.strip())


async def _gather(registry: EngineRegistry, group_id: int) -> GroupAdminSnapshot:
    modcfg, filter_count, welcome, alias_count, active_warns = await _fetch_moderation_sections(
        registry, group_id
    )
    treasury = await _fetch_treasury(registry, group_id)
    rules_set = await _fetch_rules_set(registry, group_id)
    return GroupAdminSnapshot(
        modcfg=modcfg,
        filter_count=filter_count,
        welcome=welcome,
        alias_count=alias_count,
        treasury=treasury,
        rules_set=rules_set,
        active_warns=active_warns,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _title(raw: str | None) -> str:
    """HTML-escape the chat title before it goes into a card header.

    The title is set by the group's own owner and lands inside an
    HTML-parse-mode message. An unescaped ``<`` either renders as markup
    or makes Telegram reject the whole card with "can't parse entities"
    — i.e. a group could name itself into a permanently broken panel.
    """
    return html.escape(raw or "")


def _render_card(snapshot: GroupAdminSnapshot, lang: str, *, title: str | None) -> str:
    """Build the control-card text (HTML).

    The footer timestamp guarantees a refresh tap always changes the
    message body, so ``edit_text`` never trips Telegram's
    "message is not modified" on back-to-back taps (the handler still
    guards against it for the same-second case).
    """
    lines: list[str] = [t("h_ga_header", lang, title=_title(title))]

    cfg = snapshot.modcfg
    if cfg is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        lines.append(
            t(
                "h_ga_mod",
                lang,
                automod=_mark(cfg.automod_enabled),
                profanity=_mark(cfg.profanity_enabled),
                warns=cfg.max_warns,
                mute=cfg.mute_minutes,
                autoban=_mark(cfg.autoban_enabled),
                antiflood=_mark(cfg.antiflood_enabled),
                captcha=_mark(cfg.captcha_enabled),
            )
        )
        # RR-4 #44: live active-warnings load (across all members).
        if snapshot.active_warns is not None:
            lines.append(t("h_ga_active_warns", lang, count=snapshot.active_warns))

    if snapshot.filter_count is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        lines.append(t("h_ga_filter", lang, count=snapshot.filter_count))

    if snapshot.welcome is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        enabled, template_set = snapshot.welcome
        template_key = "h_ga_welcome_tpl_set" if template_set else "h_ga_welcome_tpl_unset"
        lines.append(
            t(
                "h_ga_welcome",
                lang,
                enabled=_mark(enabled),
                template=t(template_key, lang),
            )
        )

    if snapshot.alias_count is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        lines.append(t("h_ga_aliases", lang, count=snapshot.alias_count))

    if snapshot.treasury is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        balance, group_xp = snapshot.treasury
        lines.append(
            t(
                "h_ga_treasury",
                lang,
                balance=balance,
                noun=plural(balance, "h_plural_coins", lang),
                xp=group_xp,
            )
        )

    if snapshot.rules_set is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        lines.append(t("h_ga_rules_set" if snapshot.rules_set else "h_ga_rules_unset", lang))

    lines.append("")
    lines.append(_footer(lang))
    return "\n".join(lines)


def _footer(lang: str) -> str:
    """Shared footer stamp.

    Every page carries it for the same two reasons: it tells the admin
    how fresh the numbers are, and it guarantees a refresh tap changes
    the message body so ``edit_text`` does not trip Telegram's "message
    is not modified" (the handler still guards the same-second case).
    """
    return t("h_ga_footer", lang, time=datetime.now(UTC).strftime("%H:%M:%S"))


# ---------------------------------------------------------------------------
# Sub-pages: Settings (RR-4 #36), Stats (#37), Words (#35)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModStats:
    """One sweep over moderation.db for the stats page.

    ``None`` marks a failed read; the renderer shows the shared
    "unavailable" line for that block rather than dropping the page.
    """

    active_warns: int | None
    action_counts: dict[str, int] | None
    filter_count: int | None
    recent: list[ActionRow] | None


# Audit actions that get their own counter row, in display order.
# Legacy counted mutes and bans out of dedicated tables (mutes at
# bot.py:31043-31044, bans at bot.py:31045-31050 — :31042 next door is
# the WARNINGS count, a different thing); the port's append-only
# ``moderation_log`` is the record, so these are
# "how many times this was done here", which is what an admin reading a
# moderation panel actually wants to know.
_COUNTED_ACTIONS: tuple[tuple[str, str], ...] = (
    ("warn", "h_ga_stats_warns_total"),
    ("mute", "h_ga_stats_mutes"),
    ("ban", "h_ga_stats_bans"),
    ("kick", "h_ga_stats_kicks"),
    ("fine", "h_ga_stats_fines"),
)

# Human labels for the log tail. A raw action string is escaped and
# shown as-is when it has no entry here, so a new action type degrades
# to something readable instead of a missing-key placeholder.
_ACTION_LABELS: dict[str, str] = {
    "warn": "h_ga_action_warn",
    "unwarn": "h_ga_action_unwarn",
    "mute": "h_ga_action_mute",
    "unmute": "h_ga_action_unmute",
    "ban": "h_ga_action_ban",
    "unban": "h_ga_action_unban",
    "kick": "h_ga_action_kick",
    "fine": "h_ga_action_fine",
    "pin": "h_ga_action_pin",
    "unpin": "h_ga_action_unpin",
}

# How much of a free-text reason survives into a log row. Long enough to
# be informative, short enough that five rows can't blow the 4096-char
# message limit between them.
_REASON_MAX = 40

# How many filter words the Words page previews. Legacy showed 20
# (bot.py:32330) and said how many more there were.
_WORDS_PREVIEW = 20


async def _fetch_stats(registry: EngineRegistry, group_id: int) -> ModStats:
    """Stats page reads — one moderation.db session, each guarded."""
    active_warns: int | None = None
    action_counts: dict[str, int] | None = None
    filter_count: int | None = None
    recent: list[ActionRow] | None = None
    try:
        async with session_for(registry, DBName.MODERATION) as session:
            repo = ModerationRepo(session)
            try:
                active_warns = await repo.count_active_warnings_in_chat(chat_id=group_id)
            except Exception:  # noqa: BLE001 — degrade to the unavailable line
                log.opt(exception=True).warning("groupadmin: stats warns failed")
            try:
                action_counts = await repo.count_chat_actions(chat_id=group_id)
            except Exception:  # noqa: BLE001
                log.opt(exception=True).warning("groupadmin: stats counts failed")
            try:
                recent = await repo.recent_chat_actions(chat_id=group_id, limit=5)
            except Exception:  # noqa: BLE001
                log.opt(exception=True).warning("groupadmin: stats log failed")
            try:
                filter_count = len(await WordFilterRepo(session).list(group_id=group_id))
            except Exception:  # noqa: BLE001
                log.opt(exception=True).warning("groupadmin: stats words failed")
    except Exception:  # noqa: BLE001 — session open itself failed
        log.opt(exception=True).warning("groupadmin: stats session failed")
    return ModStats(
        active_warns=active_warns,
        action_counts=action_counts,
        filter_count=filter_count,
        recent=recent,
    )


async def _fetch_modcfg(registry: EngineRegistry, group_id: int) -> GroupModConfigView | None:
    """The settings page's single read — the group's moderation config."""
    try:
        async with session_for(registry, DBName.MODERATION) as session:
            return await GroupModConfigRepo(session).get_or_default(group_id)
    except Exception:  # noqa: BLE001
        log.opt(exception=True).warning("groupadmin: settings modcfg read failed")
        return None


async def _fetch_words(registry: EngineRegistry, group_id: int) -> list[WordEntry] | None:
    """The words page's single read — the group's filter list with ids.

    The ids are what the per-word delete buttons carry (RR-4 #43); the
    renderer only ever sees the words.
    """
    try:
        async with session_for(registry, DBName.MODERATION) as session:
            return await WordFilterRepo(session).list_entries(group_id=group_id)
    except Exception:  # noqa: BLE001
        log.opt(exception=True).warning("groupadmin: words read failed")
        return None


def _render_settings(cfg: GroupModConfigView | None, lang: str, *, title: str | None) -> str:
    """Settings page body — the readout the toggles below it act on."""
    lines = [t("h_ga_set_header", lang, title=_title(title))]
    if cfg is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        lines.append(
            t(
                "h_ga_set_body",
                lang,
                automod=_mark(cfg.automod_enabled),
                profanity=_mark(cfg.profanity_enabled),
                autoban=_mark(cfg.autoban_enabled),
                antiflood=_mark(cfg.antiflood_enabled),
                captcha=_mark(cfg.captcha_enabled),
                coins=_mark(cfg.coins_enabled),
                warns=cfg.max_warns,
                mute=format_mute_choice(cfg.mute_minutes, lang),
            )
        )
        lines.append(t("h_ga_set_hint", lang))
    lines.append("")
    lines.append(_footer(lang))
    return "\n".join(lines)


def _action_label(action: str, lang: str) -> str:
    key = _ACTION_LABELS.get(action)
    return t(key, lang) if key is not None else html.escape(action)


def _render_stats(stats: ModStats, lang: str, *, title: str | None) -> str:
    """Stats page body — counters, then the last five log entries."""
    lines = [t("h_ga_stats_header", lang, title=_title(title))]

    if stats.active_warns is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        lines.append(t("h_ga_stats_warns_active", lang, count=stats.active_warns))

    if stats.action_counts is None:
        lines.append(t("h_ga_unavailable", lang))
    else:
        for action, key in _COUNTED_ACTIONS:
            lines.append(t(key, lang, count=stats.action_counts.get(action, 0)))

    if stats.filter_count is not None:
        lines.append(t("h_ga_stats_words", lang, count=stats.filter_count))

    lines.append("")
    lines.append(t("h_ga_stats_log_header", lang))
    if stats.recent is None:
        lines.append(t("h_ga_unavailable", lang))
    elif not stats.recent:
        lines.append(t("h_ga_stats_log_empty", lang))
    else:
        for row in stats.recent:
            # Free text typed by whoever ran the command, or a slug the
            # bot wrote itself (#1346). ``reason_html`` is what tells the
            # two apart, and it returns finished HTML either way: free
            # text is clipped to ``_REASON_MAX`` and only then escaped,
            # so five rows can't grow the card past Telegram's message
            # limit; a localized label goes out whole and undressed,
            # because ``t()`` yields HTML already. This line used to
            # escape both branches — the same latent double-encode #1639
            # fixed in ``handlers/profile.py``, filed here as #1641.
            reason = reason_html(row.reason, lang, limit=_REASON_MAX)
            lines.append(
                t(
                    "h_ga_stats_log_row",
                    lang,
                    action=_action_label(row.action, lang),
                    date=row.date.strftime("%d.%m %H:%M"),
                    reason=" — " + reason if reason else "",
                )
            )

    lines.append("")
    lines.append(_footer(lang))
    return "\n".join(lines)


def _render_words(words: list[str] | None, lang: str, *, title: str | None) -> str:
    """Words page body — a preview of the group's filter list."""
    lines = [t("h_ga_words_header", lang, title=_title(title))]
    if words is None:
        lines.append(t("h_ga_unavailable", lang))
    elif not words:
        lines.append(t("h_ga_words_empty", lang))
    else:
        lines.append(t("h_ga_words_count", lang, count=len(words)))
        preview = words[:_WORDS_PREVIEW]
        # Every word was typed by an admin — escaped before it goes into
        # an HTML-parse-mode message. ``<code>`` keeps a word with
        # trailing spaces or lookalike characters legible.
        lines.append(", ".join(f"<code>{html.escape(word)}</code>" for word in preview))
        if len(words) > _WORDS_PREVIEW:
            lines.append(t("h_ga_words_more", lang, count=len(words) - _WORDS_PREVIEW))
    lines.append(t("h_ga_words_hint", lang))
    lines.append("")
    lines.append(_footer(lang))
    return "\n".join(lines)


async def _render_words_page(
    page: str,
    registry: EngineRegistry,
    group_id: int,
    lang: str,
    *,
    title: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Words list, or the "pick a word to drop" grid (RR-4 #43).

    A failed read yields the read-only keyboard: ➕ over a list we could
    not load would skip the per-group ceiling check, and a delete grid
    would have nothing to put in it.
    """
    entries = await _fetch_words(registry, group_id)
    words = None if entries is None else [entry.word for entry in entries]

    if entries is None:
        return _render_words(words, lang, title=title), build_subpage_markup(PAGE_WORDS, lang)

    if page == PAGE_WORDS_DROP:
        if not entries:
            # The ➖ button is hidden on an empty list, so this is a
            # stale card or a forged token — show the list itself.
            return _render_words(words, lang, title=title), build_words_markup(
                lang, has_words=False
            )
        shown = entries[:_WORDS_PREVIEW]
        prompt = [t("h_ga_words_drop_prompt", lang)]
        if len(entries) > len(shown):
            prompt.append(t("h_ga_words_drop_more", lang, count=len(entries) - len(shown)))
        return "\n".join(prompt), build_words_drop_markup(
            [(entry.id, entry.word) for entry in shown], lang
        )

    return _render_words(words, lang, title=title), build_words_markup(
        lang, has_words=bool(entries)
    )


# ---------------------------------------------------------------------------
# Staff page (RR-4 #38)
# ---------------------------------------------------------------------------

#: How many rank-holders we are willing to pull from ``users.db`` and
#: membership-probe for one staff page. Ranks are GLOBAL here, so this
#: table is "everyone with bot power anywhere" — without a cap, a group
#: with three members could cost one ``get_chat_member`` call per ranked
#: user in the whole bot on every refresh. Legacy had no cap at all
#: (bot.py:31108). Truncation is shown in the card, never silent.
_STAFF_PROBE_LIMIT: Final[int] = 25

#: How many roster rows the card prints, and how many demote buttons the
#: grid offers (#120). The probe limit above bounds only the DATABASE
#: half: the other half is ``get_chat_administrators``, which a
#: supergroup may fill with up to 50 people and which nothing here
#: capped. A row runs to roughly 90 characters at Telegram's 64-character
#: first_name ceiling, so somewhere past ~45 people the card crossed
#: 4096 — and Telegram answers that with a 400, so the admin sees no
#: card at all rather than a shortened one. The demote grid grew a
#: button per person in the same unbounded way.
#: Both cut-offs are disclosed in the copy, never silent.
_STAFF_ROWS_MAX: Final[int] = 20
_STAFF_DROP_MAX: Final[int] = 20

#: Longest display name a roster row or a demote button spells out.
#: Telegram allows 64 characters of first_name, and twenty of those at
#: full length is a card nobody can read on a phone — the id on the row
#: is what identifies the person unambiguously anyway.
_STAFF_NAME_MAX: Final[int] = 32

#: Ranks the panel may hand out. Legacy's prompt offered 1..4
#: (bot.py:31147, ``range(1, 5)``) and we keep exactly that range —
#: which also means the panel structurally cannot mint an OWNER (5) or a
#: DEVELOPER (6) even if the actor holds one.
PANEL_GRANT_MIN: Final[int] = int(RankLevel.JUNIOR_MOD)
PANEL_GRANT_MAX: Final[int] = int(RankLevel.ADMIN)

#: Statuses that mean "not in this chat" for the membership probe.
_ABSENT_STATUSES: Final[frozenset[str]] = frozenset(
    {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
)


@dataclass(frozen=True, slots=True)
class StaffMember:
    """One row of the staff roster."""

    user_id: int
    name: str
    rank: int
    tg_admin: bool


@dataclass(frozen=True, slots=True)
class StaffRoster:
    """Staff page data. ``members is None`` means "could not be read"."""

    members: tuple[StaffMember, ...] | None
    truncated: bool


def _member_name(first_name: str | None, user_id: int) -> str:
    """Display name, falling back to the id for a user we've never seen.

    A rank can be granted by id to somebody who has never messaged the
    bot, so ``first_name`` is genuinely optional — an empty label would
    render a bullet with nothing after it.
    """
    name = (first_name or "").strip()
    return name or f"ID {user_id}"


def _staff_label(name: str) -> str:
    """Display name clipped to :data:`_STAFF_NAME_MAX` units (#120).

    Measured in UTF-16 units because that is what Telegram counts
    against the 4096 ceiling — an emoji-heavy name costs twice what
    ``len()`` reports. The ellipsis marks the clip so a truncated name
    does not read as somebody's actual name.
    """
    clipped = clamp_utf16(name, _STAFF_NAME_MAX)
    return name if clipped == name else clipped + "…"


async def _fetch_staff(bot: Bot, registry: EngineRegistry, group_id: int) -> StaffRoster:
    """Who holds power in this group, and at what rank.

    Two sources, because the answer genuinely has two halves:

    * **Telegram admins of this chat** — one ``get_chat_administrators``
      call for the whole list. Legacy instead called
      ``get_chat_member`` once per globally-ranked user (bot.py:31111);
      this is one request no matter how large the admin list is.
    * **Bot rank-holders** who are not chat admins — read from
      ``users.db``, capped at :data:`_STAFF_PROBE_LIMIT`, then
      membership-probed so the roster shows people who are actually
      here. That probe is the only per-person call left, and the cap
      bounds it.

    Every read is guarded separately: a Telegram outage still shows the
    DB half, and a DB failure still shows the chat's admins. Only a
    total failure yields ``members=None``.
    """
    by_id: dict[int, StaffMember] = {}

    try:
        chat_admins = await bot.get_chat_administrators(group_id)
    except Exception as exc:  # noqa: BLE001 — degrade to the DB half
        log.bind(group_id=group_id).debug(
            "groupadmin: get_chat_administrators failed: {exc!r}", exc=exc
        )
        chat_admins = []
    # Bots hold admin rights in most groups (ours included) and are not
    # staff in any sense a human cares about.
    human_admins = [adm for adm in chat_admins if not adm.user.is_bot]
    admin_ids = {adm.user.id for adm in human_admins}

    ranked: list[RankedUser] = []
    admin_ranks: dict[int, int] = {}
    truncated = False
    try:
        async with session_for(registry, DBName.USERS) as session:
            repo = UsersRepo(session)
            if admin_ids:
                admin_ranks = await repo.ranks_for(sorted(admin_ids))
            # One extra row is what tells us the cap actually bit.
            ranked = await repo.list_ranked(
                min_rank=int(RankLevel.JUNIOR_MOD), limit=_STAFF_PROBE_LIMIT + 1
            )
    except Exception as exc:  # noqa: BLE001 — degrade to the Telegram half
        log.bind(group_id=group_id).warning("groupadmin: staff ranks read failed: {exc!r}", exc=exc)
        if not human_admins:
            return StaffRoster(members=None, truncated=False)

    if len(ranked) > _STAFF_PROBE_LIMIT:
        truncated = True
        ranked = ranked[:_STAFF_PROBE_LIMIT]

    for adm in human_admins:
        by_id[adm.user.id] = StaffMember(
            user_id=adm.user.id,
            name=_member_name(adm.user.first_name, adm.user.id),
            rank=admin_ranks.get(adm.user.id, 0),
            tg_admin=True,
        )

    for row in ranked:
        if row.user_id in by_id:
            continue
        if await _is_present(bot, group_id, row.user_id) is not True:
            continue
        by_id[row.user_id] = StaffMember(
            user_id=row.user_id,
            name=_member_name(row.first_name, row.user_id),
            rank=row.rank,
            tg_admin=False,
        )

    members = sorted(by_id.values(), key=lambda m: (-m.rank, not m.tg_admin, m.name.casefold()))
    return StaffRoster(members=tuple(members), truncated=truncated)


async def _is_present(bot: Bot, chat_id: int, user_id: int) -> bool | None:
    """Is ``user_id`` currently in ``chat_id``? ``None`` on API error.

    ``None`` is not ``False`` on purpose: an API hiccup must not quietly
    erase somebody from the roster as though they had left. The caller
    treats "unknown" as "don't list", which is the safe direction for a
    page that also offers a demote button.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — see the None contract above
        log.bind(group_id=chat_id, uid=user_id).debug(
            "groupadmin: membership probe failed: {exc!r}", exc=exc
        )
        return None
    return member.status not in _ABSENT_STATUSES


def _render_staff(roster: StaffRoster, lang: str, *, title: str | None, can_manage: bool) -> str:
    """Staff page body — the roster legacy showed, plus what it omitted.

    Legacy printed ``name — rank — id`` and nothing else (bot.py:31121),
    which left the reader unable to tell WHY somebody had power: a chat
    admin bypasses the rank system entirely in this bot, so an admin
    sitting at rank 0 still moderates. The rows mark that explicitly.

    The row count and each name are bounded (#120): the Telegram-admin
    half of the roster has no upper limit of its own, so a big
    supergroup rendered a card Telegram refuses with a 400 — and a
    refused card is indistinguishable from a dead button.
    """
    lines = [t("h_ga_staff_header", lang, title=_title(title))]
    members = roster.members
    if members is None:
        lines.append(t("h_ga_unavailable", lang))
    elif not members:
        lines.append(t("h_ga_staff_empty", lang))
    else:
        # The count line reports EVERYONE we found; the rows below are a
        # page of them. Reporting only the rendered number would hide
        # exactly the fact the truncation line exists to disclose.
        lines.append(t("h_ga_staff_count", lang, count=len(members)))
        lines.append("")
        shown = members[:_STAFF_ROWS_MAX]
        for member in shown:
            lines.append(
                t(
                    "h_ga_staff_row",
                    lang,
                    mark=t(
                        "h_ga_staff_mark_admin" if member.tg_admin else "h_ga_staff_mark_rank",
                        lang,
                    ),
                    # Telegram display names are user-controlled text
                    # heading into an HTML-parse-mode message.
                    name=html.escape(_staff_label(member.name)),
                    rank=rank_name(member.rank, lang, in_group=True),
                    user_id=member.user_id,
                )
            )
        if roster.truncated or len(members) > len(shown):
            lines.append("")
            # ``len(shown)`` rather than a constant: the DB half can be
            # cut by the probe limit while the rendered rows stop at a
            # different number, and the copy says "the first {count}",
            # which is a claim about what is on screen.
            lines.append(t("h_ga_staff_truncated", lang, count=len(shown)))
    lines.append("")
    lines.append(t("h_ga_staff_hint", lang))
    if not can_manage:
        lines.append(t("h_ga_staff_readonly", lang))
    lines.append("")
    lines.append(_footer(lang))
    return "\n".join(lines)


def _droppable(
    roster: StaffRoster, *, actor_id: int, actor_rank: int, settings: Settings
) -> list[StaffMember]:
    """Staff the ACTOR is allowed to demote, in roster order.

    The same four guards the write path enforces, applied here so the
    grid never offers a button that would refuse:

    * nobody at rank 0 — there is nothing to strip;
    * never yourself (legacy's ``can_moderate`` checks self first,
      bot.py:7592) — a panel that can demote its own operator is a
      lockout waiting to happen;
    * never a developer (legacy rank immutability, bot.py:31415-31417);
    * never somebody ranked at or above you, so a rank-4 admin cannot
      strip the owner. Legacy's staff panel had NO such guard — it
      called ``set_user_rank`` straight from a group-admin callback.
    """
    if roster.members is None:
        return []
    return [
        member
        for member in roster.members
        if member.rank > 0
        and member.user_id != actor_id
        and not settings.bot.is_developer(member.user_id)
        and member.rank < actor_rank
    ]


async def _render_page(
    page: str,
    registry: EngineRegistry,
    group_id: int,
    lang: str,
    *,
    title: str | None,
    bot: Bot,
    settings: Settings,
    actor_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Body + keyboard for one panel page.

    ``page`` has already been through
    :func:`~telegram_invite_bot.keyboards.builders.groupadmin.resolve_page`,
    so an unknown token can never reach here — it arrives as the
    overview.
    """
    if page in (PAGE_STAFF, PAGE_STAFF_DROP):
        return await _render_staff_page(
            page,
            registry,
            group_id,
            lang,
            title=title,
            bot=bot,
            settings=settings,
            actor_id=actor_id,
        )
    if page == PAGE_SETTINGS:
        cfg = await _fetch_modcfg(registry, group_id)
        return _render_settings(cfg, lang, title=title), _settings_markup(cfg, lang)
    if page == PAGE_STATS:
        stats = await _fetch_stats(registry, group_id)
        return _render_stats(stats, lang, title=title), build_subpage_markup(PAGE_STATS, lang)
    if page in (PAGE_WORDS, PAGE_WORDS_DROP):
        return await _render_words_page(page, registry, group_id, lang, title=title)
    snapshot = await _gather(registry, group_id)
    return _render_card(snapshot, lang, title=title), build_card_markup(lang)


async def _staff_authority(
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    *,
    actor_id: int,
    group_id: int,
) -> tuple[bool, int]:
    """``(may change ranks, effective rank)`` for the tapping admin.

    The permission comes from the SAME check ``!повысить`` runs
    (``RankService.may_manage_ranks``) — the panel button must never be
    an easier way in than the command it mirrors.

    Being a live Telegram admin of THIS chat is deliberately not enough:
    the ranks this panel writes are global, and anyone can create a
    group, add the bot and be its admin (see
    :meth:`~telegram_invite_bot.services.rank_service.RankService.may_manage_ranks`).
    Such an actor still sees the roster — read-only.

    The effective rank is what the target guards compare against;
    developers sit at the top, everyone else keeps their own bot rank.
    """
    verdict = await RankService(registry, settings).may_manage_ranks(actor_id, group_id, bot)
    if settings.bot.is_developer(actor_id):
        return verdict.allowed, int(RankLevel.DEVELOPER)
    return verdict.allowed, int(verdict.actor_rank)


async def _render_staff_page(
    page: str,
    registry: EngineRegistry,
    group_id: int,
    lang: str,
    *,
    title: str | None,
    bot: Bot,
    settings: Settings,
    actor_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Staff roster, or the "pick someone to demote" grid."""
    can_manage, actor_rank = await _staff_authority(
        bot, registry, settings, actor_id=actor_id, group_id=group_id
    )
    roster = await _fetch_staff(bot, registry, group_id)

    if page == PAGE_STAFF_DROP and can_manage:
        people = _droppable(roster, actor_id=actor_id, actor_rank=actor_rank, settings=settings)
        if people:
            # #120: one button per removable person, and "removable"
            # includes every ranked Telegram admin of the chat — a list
            # with no ceiling of its own. Cut it to a grid Telegram will
            # accept and say how many are left, exactly as the Words
            # panel does; each demote re-renders this page, so the next
            # batch is one tap away rather than lost.
            shown = people[:_STAFF_DROP_MAX]
            prompt = [t("h_ga_staff_drop_prompt", lang)]
            if len(people) > len(shown):
                prompt.append(t("h_ga_staff_drop_more", lang, count=len(people) - len(shown)))
            return "\n".join(prompt), build_staff_drop_markup(
                [
                    (
                        m.user_id,
                        t("h_ga_staff_drop_btn", lang, name=_staff_label(m.name)),
                    )
                    for m in shown
                ],
                lang,
            )
        # Nobody this actor may demote — say so instead of an empty grid.
        return t("h_ga_staff_drop_none", lang), build_staff_drop_markup([], lang)

    return _render_staff(roster, lang, title=title, can_manage=can_manage), (
        build_staff_markup(lang, can_manage=can_manage)
    )


def _settings_markup(cfg: GroupModConfigView | None, lang: str) -> InlineKeyboardMarkup:
    """Settings keyboard, or just "back" when the config could not be read.

    Offering toggles over a config we failed to load would mean writing
    a value derived from nothing — the tap would silently reset the
    group to defaults. No readout, no controls.
    """
    if cfg is None:
        return build_subpage_markup(PAGE_SETTINGS, lang)
    return build_settings_markup(
        lang=lang,
        automod=cfg.automod_enabled,
        profanity=cfg.profanity_enabled,
        autoban=cfg.autoban_enabled,
        antiflood=cfg.antiflood_enabled,
        captcha=cfg.captcha_enabled,
        coins=cfg.coins_enabled,
        max_warns=cfg.max_warns,
        mute_minutes=cfg.mute_minutes,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_groupadmin(
    message: Message,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """``/groupadmin`` — render the control card for the current group."""
    if not await _gate_message(message, bot, settings, lang):
        return
    snapshot = await _gather(registry, message.chat.id)
    await message.answer(
        _render_card(snapshot, lang, title=message.chat.title),
        reply_markup=build_card_markup(lang),
    )
    log.bind(group_id=message.chat.id).info("/groupadmin card rendered")


async def _open_panel(
    callback: CallbackQuery,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> Message | None:
    """Shared preamble for every button: locate the card, re-gate the tap.

    Returns the card message when the tapping user may act, ``None``
    otherwise (the refusal/ack has already been sent).

    The group id comes from ``callback.message.chat.id`` (Telegram-set,
    not user-controlled). An inaccessible/absent message (card too old,
    or a forged payload with no message context) is answered and
    dropped — there is nothing trustworthy to render or write against.
    """
    message = callback.message
    if not isinstance(message, Message) or message.chat.type not in GROUP_TYPES:
        await callback.answer()
        return None
    if not await _gate_callback(callback, bot, settings, chat_id=message.chat.id, lang=lang):
        return None
    return message


async def _edit_in_place(
    callback: CallbackQuery,
    message: Message,
    text: str,
    markup: InlineKeyboardMarkup,
    *,
    toast: str | None = None,
) -> None:
    """Swap the card's body + keyboard, then ack the tap.

    ``toast`` is the short confirmation Telegram shows over the chat —
    legacy popped one on every settings change (bot.py:30892) and it is
    the only feedback that a tap on a button whose label barely changes
    actually did something.
    """
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        # Two taps within the same second produce an identical body
        # (the footer has 1s resolution) — benign, just ack the tap.
        if "message is not modified" not in str(exc):
            raise
    await callback.answer(toast)


async def handle_groupadmin_refresh(
    callback: CallbackQuery,
    callback_data: GroupAdminRefresh,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Navigation: open a sub-page, or re-render the overview in place."""
    message = await _open_panel(callback, bot, registry, settings, lang)
    if message is None:
        return
    # Anything that isn't an explicit page token — including every
    # section button and any forged value — lands on the overview.
    page = resolve_page(callback_data.section)
    text, markup = await _render_page(
        page,
        registry,
        message.chat.id,
        lang,
        title=message.chat.title,
        bot=bot,
        settings=settings,
        actor_id=callback.from_user.id,
    )
    await _edit_in_place(callback, message, text, markup)
    log.bind(group_id=message.chat.id, section=callback_data.section, page=page).info(
        "/groupadmin page rendered"
    )


async def handle_groupadmin_pick(
    callback: CallbackQuery,
    callback_data: GroupAdminPick,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Open the value grid for one numeric setting (RR-4 #36)."""
    message = await _open_panel(callback, bot, registry, settings, lang)
    if message is None:
        return
    picker = callback_data.field
    cfg = await _fetch_modcfg(registry, message.chat.id)
    if picker not in PICKER_FIELDS:
        # Forged or stale token — show the page it belongs to rather
        # than a grid of nothing.
        await _edit_in_place(
            callback,
            message,
            _render_settings(cfg, lang, title=message.chat.title),
            _settings_markup(cfg, lang),
        )
        return
    if cfg is None:
        await callback.answer(t("h_ga_unavailable", lang), show_alert=True)
        return
    is_mute = picker == "mute"
    current = getattr(cfg, PICKER_FIELDS[picker])
    text = t(
        "h_ga_pick_header_mute" if is_mute else "h_ga_pick_header",
        lang,
        current=format_mute_choice(current, lang) if is_mute else current,
    )
    await _edit_in_place(callback, message, text, build_picker_markup(picker, lang))


async def handle_groupadmin_set(
    callback: CallbackQuery,
    callback_data: GroupAdminSet,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Write one moderation-config field, then re-render Settings (RR-4 #36).

    Both halves of the payload are user-controlled wire data and are
    re-validated here against the published vocabulary:

    * ``field`` must be a token this module publishes as writable, so a
      forged payload cannot reach any other ``group_mod_config`` column;
    * ``value`` must be 0/1 for a toggle, or one of the picker's own
      offered numbers — never an arbitrary integer. That is what stops a
      tampered payload from setting ``max_warns`` to 0 (every message an
      instant ban) or ``mute_minutes`` to a century.

    The write targets the group the CARD lives in, never a group named
    by the payload, and the tapping user has already been re-gated.
    """
    message = await _open_panel(callback, bot, registry, settings, lang)
    if message is None:
        return

    field_token = callback_data.field
    value = callback_data.value
    field: str
    typed_value: bool | int
    if field_token in TOGGLE_FIELDS:
        if value not in (0, 1):
            await callback.answer()
            return
        field, typed_value = TOGGLE_FIELDS[field_token], bool(value)
    elif field_token in PICKER_FIELDS:
        if value not in picker_choices(field_token):
            await callback.answer()
            return
        field, typed_value = PICKER_FIELDS[field_token], value
    else:
        await callback.answer()
        return

    try:
        async with session_for(registry, DBName.MODERATION) as session:
            cfg = await GroupModConfigRepo(session).set_field(
                group_id=message.chat.id, field=field, value=typed_value
            )
    except Exception:  # noqa: BLE001 — a failed write must not eat the tap
        log.opt(exception=True).warning("groupadmin: settings write failed")
        await callback.answer(t("h_ga_save_failed", lang), show_alert=True)
        return

    log.bind(
        group_id=message.chat.id,
        admin_id=callback.from_user.id,
        field=field,
        value=typed_value,
    ).info("/groupadmin setting changed")
    # ``set_field`` returns the merged config, so the page re-renders
    # from the value we just wrote instead of re-reading it.
    await _edit_in_place(
        callback,
        message,
        _render_settings(cfg, lang, title=message.chat.title),
        _settings_markup(cfg, lang),
        toast=t("h_ga_saved", lang),
    )


# ---------------------------------------------------------------------------
# Staff handlers (RR-4 #38)
# ---------------------------------------------------------------------------


async def _rerender_staff(
    callback: CallbackQuery,
    message: Message,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
    *,
    toast: str | None = None,
) -> None:
    """Re-read the roster and swap the card back to the staff page."""
    text, markup = await _render_staff_page(
        PAGE_STAFF,
        registry,
        message.chat.id,
        lang,
        title=message.chat.title,
        bot=bot,
        settings=settings,
        actor_id=callback.from_user.id,
    )
    await _edit_in_place(callback, message, text, markup, toast=toast)


async def handle_groupadmin_staff_drop(
    callback: CallbackQuery,
    callback_data: GroupAdminStaffDrop,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Strip one user's global rank from the staff grid (RR-4 #38).

    The button was built from a roster this bot had just read, but the
    payload comes back through the user, so nothing about it is taken on
    trust: the permission, the actor's ceiling and the target's CURRENT
    rank are all re-read here. That closes the window where a grid
    rendered a minute ago is tapped after the target has been promoted
    above the tapper.

    Ranks are global, so this removes the user's power everywhere, not
    just in this group — the confirmation says so.
    """
    message = await _open_panel(callback, bot, registry, settings, lang)
    if message is None:
        return

    actor_id = callback.from_user.id
    can_manage, actor_rank = await _staff_authority(
        bot, registry, settings, actor_id=actor_id, group_id=message.chat.id
    )
    if not can_manage:
        await callback.answer(t("h_ga_staff_denied", lang), show_alert=True)
        return

    target_id = callback_data.user_id
    ranks = RankService(registry, settings)
    target_rank = await ranks.get_rank(target_id)
    if (
        target_id == actor_id
        or settings.bot.is_developer(target_id)
        or target_rank <= 0
        or target_rank >= actor_rank
    ):
        await callback.answer(t("h_ga_staff_denied", lang), show_alert=True)
        return

    if not await ranks.set_rank(target_id, RankLevel.USER, by=actor_id):
        await callback.answer(t("h_ga_staff_failed", lang), show_alert=True)
        return

    log.bind(
        group_id=message.chat.id,
        admin_id=actor_id,
        target_id=target_id,
        was_rank=target_rank,
    ).info("/groupadmin staff rank stripped")
    await _rerender_staff(
        callback,
        message,
        bot,
        registry,
        settings,
        lang,
        toast=t("h_ga_staff_dropped", lang),
    )


async def handle_groupadmin_staff_add(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Prompt for ``<id|@username> <rank>`` and park the admin in the FSM.

    Legacy stashed the pending action in a process-global ``temp_data``
    dict keyed by user id (bot.py:31155 and :31174), so the same admin
    prompting in two chats clobbered themselves. The aiogram FSM key is
    (chat, user), which fixes that for free.
    """
    message = await _open_panel(callback, bot, registry, settings, lang)
    if message is None:
        return
    can_manage, _ = await _staff_authority(
        bot, registry, settings, actor_id=callback.from_user.id, group_id=message.chat.id
    )
    if not can_manage:
        await callback.answer(t("h_ga_staff_denied", lang), show_alert=True)
        return

    from telegram_invite_bot.scheduler.fsm_sweeper import (
        STATE_ENTERED_AT_FIELD,
        utc_now_iso,
    )

    await state.set_state(GroupStaffStates.awaiting_grant)
    await state.set_data({STATE_ENTERED_AT_FIELD: utc_now_iso(), "lang": lang})
    legend = "\n".join(
        t("h_ga_staff_rank_row", lang, value=level, name=rank_name(level, lang, in_group=True))
        for level in range(PANEL_GRANT_MIN, PANEL_GRANT_MAX + 1)
    )
    await _edit_in_place(
        callback,
        message,
        t("h_ga_staff_add_prompt", lang, ranks=legend),
        build_subpage_markup(PAGE_STAFF, lang),
    )
    log.bind(group_id=message.chat.id, admin_id=callback.from_user.id).info(
        "/groupadmin staff grant prompt opened"
    )


def parse_staff_grant(raw: str | None) -> tuple[str, int] | None:
    """Parse ``<id|@username> <rank>`` — ``None`` when it isn't that.

    Legacy accepted a numeric id only (``int(parts[0])``,
    bot.py:31190); ``@username`` is accepted here because that is what
    an admin actually has in front of them, and the resolver already
    exists for every other target-taking command.

    The rank bound is checked by the caller against
    :data:`PANEL_GRANT_MIN` / :data:`PANEL_GRANT_MAX` so the refusal can
    name the allowed range.
    """
    parts = (raw or "").split()
    if len(parts) != 2:
        return None
    target, rank_raw = parts
    try:
        rank = int(rank_raw)
    except ValueError:
        return None
    return target, rank


async def handle_groupadmin_staff_text(
    message: Message,
    bot: Bot,
    state: FSMContext,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Apply the typed grant, then clear the FSM (RR-4 #38).

    The whole gate runs again here, not just at prompt time: the state
    survives across messages, and the admin could have been demoted (or
    the bot's view of the chat could have changed) between the tap and
    the text.

    Malformed input does NOT clear the state — the admin retypes instead
    of re-navigating the panel, matching how the other input flows in
    this codebase behave.
    """
    if message.from_user is None:
        return
    actor_id = message.from_user.id
    can_manage, actor_rank = await _staff_authority(
        bot, registry, settings, actor_id=actor_id, group_id=message.chat.id
    )
    if not can_manage:
        await state.clear()
        await message.reply(t("h_ga_staff_denied", lang))
        return

    parsed = parse_staff_grant(message.text)
    if parsed is None:
        await message.reply(t("h_ga_staff_bad_format", lang))
        return
    target_raw, level = parsed
    if not PANEL_GRANT_MIN <= level <= PANEL_GRANT_MAX:
        await message.reply(
            t("h_ga_staff_bad_rank", lang, low=PANEL_GRANT_MIN, high=PANEL_GRANT_MAX)
        )
        return

    target_id = await _resolve_staff_target(registry, target_raw)
    if target_id is None:
        await message.reply(t("h_ga_staff_no_target", lang))
        return

    ranks = RankService(registry, settings)
    target_rank = await ranks.get_rank(target_id)
    if (
        target_id == actor_id
        or settings.bot.is_developer(target_id)
        or level >= actor_rank
        or target_rank >= actor_rank
    ):
        await state.clear()
        await message.reply(t("h_ga_staff_denied", lang))
        return

    if not await ranks.set_rank(target_id, level, by=actor_id):
        await state.clear()
        await message.reply(t("h_ga_staff_failed", lang))
        return

    await state.clear()
    log.bind(
        group_id=message.chat.id,
        admin_id=actor_id,
        target_id=target_id,
        rank=level,
    ).info("/groupadmin staff rank granted")
    await message.reply(
        t(
            "h_ga_staff_granted",
            lang,
            user_id=target_id,
            rank=rank_name(level, lang, in_group=True),
        )
    )


async def on_expire_group_staff(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """FSM-sweeper callback for an abandoned staff-grant prompt.

    Nothing was written — the rank change happens only when a parseable
    message arrives — so expiry costs the admin nothing. The notice
    matters because while the state was set, their ordinary messages in
    that chat were being read as staff input; they should know that has
    stopped. It goes to the chat the prompt was opened in, since that is
    where they are looking.
    """
    lang_raw = data.get("lang")
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(key.chat_id, t("h_ga_staff_timeout", lang))
    log.bind(group_id=key.chat_id, admin_id=key.user_id).info(
        "/groupadmin staff grant session expired by sweeper"
    )


async def _resolve_staff_target(registry: EngineRegistry, raw: str) -> int | None:
    """``123456`` or ``@name`` → user id, or ``None`` if unresolvable."""
    if raw.startswith("@"):
        username = raw[1:].strip()
        if not username:
            return None
        try:
            async with session_for(registry, DBName.USERS) as session:
                user = await UsersRepo(session).get_by_username(username)
        except Exception as exc:  # noqa: BLE001 — unresolvable, not fatal
            log.warning("groupadmin: staff target lookup failed: {exc!r}", exc=exc)
            return None
        return user.user_id if user is not None else None
    # #1692: the sign was checked below, the magnitude was not — a
    # twenty-digit run parsed cleanly here and raised OverflowError
    # in ``RankService.get_rank``, which binds it.
    target_id = parse_int_token(raw, signed=True)
    if target_id is None:
        return None
    # Telegram ids are positive; a negative value here is a chat id
    # (or a typo) and must never be written into ``users.rank``.
    return target_id if target_id > 0 else None


# ---------------------------------------------------------------------------
# Words handlers (RR-4 #43)
# ---------------------------------------------------------------------------
#
# Gate: the panel's own — developer OR live TG-admin (``_actor_allowed``,
# see :214-216). There is NO rank branch: it was removed in #670 as an
# unsanctioned widening, and it must not be restored here. That is not a
# lost privilege — whoever reaches this page can already switch the whole
# profanity filter off from the Settings page, which is strictly more
# than removing one word from it, so the narrower gate costs nothing.


async def _rerender_words(
    callback: CallbackQuery,
    message: Message,
    registry: EngineRegistry,
    lang: str,
    *,
    page: str,
    toast: str | None = None,
) -> None:
    """Re-read the filter list and swap the card to ``page``."""
    text, markup = await _render_words_page(
        page, registry, message.chat.id, lang, title=message.chat.title
    )
    await _edit_in_place(callback, message, text, markup, toast=toast)


async def handle_groupadmin_word_drop(
    callback: CallbackQuery,
    callback_data: GroupAdminWordDrop,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Delete one filter word from the grid (RR-4 #43).

    The row id came back through the user, so the delete is scoped to
    ``(group_id, id)`` where the group is the card's OWN chat — an id
    lifted from another group's panel matches nothing. A row that is
    already gone (two admins on the same grid) is reported, not retried.

    The grid is re-rendered rather than the list, so an admin cleaning
    up several words does not have to walk back in each time; when the
    last word goes the page falls back to the list by itself.
    """
    message = await _open_panel(callback, bot, registry, settings, lang)
    if message is None:
        return

    try:
        async with session_for(registry, DBName.MODERATION) as session:
            removed = await WordFilterRepo(session).remove_by_id(
                group_id=message.chat.id, word_id=callback_data.word_id
            )
    except Exception:  # noqa: BLE001 — a failed write must not eat the tap
        log.opt(exception=True).warning("groupadmin: word delete failed")
        await callback.answer(t("h_ga_save_failed", lang), show_alert=True)
        return

    if removed is None:
        await _rerender_words(
            callback,
            message,
            registry,
            lang,
            page=PAGE_WORDS_DROP,
            toast=t("h_ga_words_drop_gone", lang),
        )
        return

    log.bind(group_id=message.chat.id, admin_id=callback.from_user.id, word=removed).info(
        "/groupadmin filter word removed"
    )
    await _rerender_words(
        callback,
        message,
        registry,
        lang,
        page=PAGE_WORDS_DROP,
        # The toast names the word: the grid re-renders under the
        # admin's finger and labels are clipped at 24 chars, so "removed"
        # alone would not say WHICH one went. A callback answer is plain
        # text (Telegram parses no entities there), hence no escaping —
        # and 100 chars fits the 200-char alert cap.
        toast=t("h_ga_words_dropped", lang, word=removed),
    )


async def handle_groupadmin_word_add(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Prompt for a word and park the admin in the FSM (RR-4 #43)."""
    message = await _open_panel(callback, bot, registry, settings, lang)
    if message is None:
        return

    from telegram_invite_bot.scheduler.fsm_sweeper import (
        STATE_ENTERED_AT_FIELD,
        utc_now_iso,
    )

    await state.set_state(GroupWordsStates.awaiting_word)
    await state.set_data({STATE_ENTERED_AT_FIELD: utc_now_iso(), "lang": lang})
    await _edit_in_place(
        callback,
        message,
        t("h_ga_words_add_prompt", lang),
        build_subpage_markup(PAGE_WORDS, lang),
    )
    log.bind(group_id=message.chat.id, admin_id=callback.from_user.id).info(
        "/groupadmin word add prompt opened"
    )


async def handle_groupadmin_word_text(
    message: Message,
    bot: Bot,
    state: FSMContext,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Store the typed word for THIS group, then clear the FSM (RR-4 #43).

    The gate runs again here: the state survives across messages, and
    the admin may have lost their rights between the tap and the text.

    Validation is :func:`~telegram_invite_bot.handlers.wordfilter.validate_word`
    — the same rules /filter_add applies, so the panel is not a way
    around the length cap or the "don't ban a command" rule. A refusal
    keeps the state so the admin retypes instead of re-navigating,
    matching the staff flow and every other input flow here.

    Answers are ``answer``, not ``reply``: ``WordFilterAutomodMiddleware``
    is an OUTER middleware and exempts nobody, so an admin re-typing a
    word that is ALREADY filtered has their own message deleted before
    this handler runs — and a reply to a deleted message is an API
    error. Sending to the chat works either way, and "that one is
    already in the list" is exactly the case where the message vanishes.
    """
    if message.from_user is None:
        return
    verdict = await _actor_allowed(
        bot, settings, chat_id=message.chat.id, user_id=message.from_user.id
    )
    if verdict is not True:
        await state.clear()
        await message.answer(
            t("h_mod_retry_later" if verdict is None else "h_mod_no_permission", lang)
        )
        return

    word, refusal = validate_word(message.text or "", empty_key="h_ga_words_add_empty")
    if refusal is not None:
        await message.answer(t(refusal, lang))
        return

    try:
        async with session_for(registry, DBName.MODERATION) as session:
            repo = WordFilterRepo(session)
            over = add_refusal(await repo.list(group_id=message.chat.id), word)
            if over is not None:
                await state.clear()
                await message.answer(t(over, lang))
                return
            added = await repo.add(
                group_id=message.chat.id,
                word=word,
                added_by=message.from_user.id,
            )
    except Exception:  # noqa: BLE001 — say so rather than silently dropping it
        log.opt(exception=True).warning("groupadmin: word add failed")
        await state.clear()
        await message.answer(t("h_ga_save_failed", lang))
        return

    await state.clear()
    safe = html.escape(word)
    if added:
        log.bind(group_id=message.chat.id, admin_id=message.from_user.id, word=word).info(
            "/groupadmin filter word added"
        )
        await message.answer(t("h_wf_added", lang, word=safe))
    else:
        await message.answer(t("h_wf_already", lang, word=safe))


async def on_expire_group_words(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """FSM-sweeper callback for an abandoned add-a-word prompt.

    Nothing was written — the word is stored only when a message
    arrives — so expiry costs the admin nothing. The notice matters
    because while the state was set their ordinary messages in that chat
    were being read as filter input; they should know that has stopped.
    """
    lang_raw = data.get("lang")
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(key.chat_id, t("h_ga_words_timeout", lang))
    log.bind(group_id=key.chat_id, admin_id=key.user_id).info(
        "/groupadmin word add session expired by sweeper"
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the /groupadmin router (messages group-only).

    Callbacks carry no chat-type filter at registration: the prefix is
    unique to buttons this router itself rendered in a group, and the
    handler re-checks the chat type + re-gates the tapping user anyway.
    ``lang`` is injected by the root :class:`LanguageMiddleware` on both
    message and callback updates.
    """
    router = Router(name="groupadmin")
    group_filter = F.chat.type.in_(GROUP_TYPES)

    async def _entry(message: Message, bot: Bot, lang: str) -> None:
        await handle_groupadmin(message, bot, registry, settings, lang)

    async def _refresh(
        callback: CallbackQuery,
        callback_data: GroupAdminRefresh,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_groupadmin_refresh(callback, callback_data, bot, registry, settings, lang)

    async def _pick(
        callback: CallbackQuery,
        callback_data: GroupAdminPick,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_groupadmin_pick(callback, callback_data, bot, registry, settings, lang)

    async def _set(
        callback: CallbackQuery,
        callback_data: GroupAdminSet,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_groupadmin_set(callback, callback_data, bot, registry, settings, lang)

    async def _staff_add(
        callback: CallbackQuery,
        bot: Bot,
        state: FSMContext,
        lang: str,
    ) -> None:
        await handle_groupadmin_staff_add(callback, bot, state, registry, settings, lang)

    async def _staff_drop(
        callback: CallbackQuery,
        callback_data: GroupAdminStaffDrop,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_groupadmin_staff_drop(callback, callback_data, bot, registry, settings, lang)

    async def _staff_text(
        message: Message,
        bot: Bot,
        state: FSMContext,
        lang: str,
    ) -> None:
        await handle_groupadmin_staff_text(message, bot, state, registry, settings, lang)

    async def _word_add(
        callback: CallbackQuery,
        bot: Bot,
        state: FSMContext,
        lang: str,
    ) -> None:
        await handle_groupadmin_word_add(callback, bot, state, registry, settings, lang)

    async def _word_drop(
        callback: CallbackQuery,
        callback_data: GroupAdminWordDrop,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_groupadmin_word_drop(callback, callback_data, bot, registry, settings, lang)

    async def _word_text(
        message: Message,
        bot: Bot,
        state: FSMContext,
        lang: str,
    ) -> None:
        await handle_groupadmin_word_text(message, bot, state, registry, settings, lang)

    router.message.register(
        _entry,
        Command("groupadmin", "group_admin", "управлениегруппой", ignore_case=True),
        F.from_user,
        group_filter,
    )
    # The staff-input step is state-scoped AND group-scoped: only an
    # admin who just tapped ➕ on a card in this chat is in the state, so
    # ordinary traffic never reaches it.
    #
    # Commands are excluded explicitly. A grant line never starts with
    # "/", and this router sits late in the chain — without the guard a
    # pending prompt would swallow every command owned by a LATER router
    # and answer it with "wrong format". An admin who taps ➕ and then
    # changes their mind should still be able to run other commands; the
    # state simply waits for /cancel or the sweeper.
    router.message.register(
        _staff_text,
        StateFilter(GroupStaffStates.awaiting_grant),
        group_filter,
        F.text,
        NOT_A_COMMAND,
    )
    # Same shape for the word-input step, and the "/" exclusion is
    # load-bearing twice over here: it keeps a pending prompt from
    # swallowing later routers' commands, AND a word starting with "/"
    # is refused by ``validate_word`` anyway (legacy's
    # ``filter_cmd_ignored``), so nothing legitimate is lost.
    router.message.register(
        _word_text,
        StateFilter(GroupWordsStates.awaiting_word),
        group_filter,
        F.text,
        NOT_A_COMMAND,
    )
    # These two steps deliberately get NO ``register_text_expected``
    # (``handlers/fsm_text.py``) twin, unlike every private interview.
    # Both live in a group, and the abandonment case above is the common
    # one: the admin taps ➕, gets distracted, and the state waits for the
    # sweeper. A "send me text" reply to every non-text message would then
    # answer their memes in front of the whole room for as long as the
    # state lives. Silence is the lesser harm here — the prompt is on
    # screen and the panel is one tap away.
    router.callback_query.register(_refresh, GroupAdminRefresh.filter())
    router.callback_query.register(_pick, GroupAdminPick.filter())
    router.callback_query.register(_set, GroupAdminSet.filter())
    router.callback_query.register(_staff_add, GroupAdminStaffAdd.filter())
    router.callback_query.register(_staff_drop, GroupAdminStaffDrop.filter())
    router.callback_query.register(_word_add, GroupAdminWordAdd.filter())
    router.callback_query.register(_word_drop, GroupAdminWordDrop.filter())
    return with_chat_type_refusal(router, scope="group")
