"""/ban, /unban, /kick, /mute, /unmute, /warn, /unwarn, /warnings, /pin, /unpin, /fine — T-020.

Ports the group-moderation commands from legacy ``bot.py`` (telebot,
Markdown parse_mode) to the new aiogram pipeline (HTML parse_mode).

Commands claimed (group-only — a private chat gets the #123 refusal):

  /ban        (alias бан)              — ban user; optional duration + reason
  /unban      (aliases разбан, kom_unban) — lift a ban (reversal of /ban)
  /kick       (alias кик)              — kick (ban + immediate unban)
  /mute       (alias мут)              — restrict send; optional duration
  /unmute     (aliases размут, kom_unmute) — lift a mute (reversal of /mute)
  /warn       (aliases варн, предупреждение) — issue a warning; auto-ban at threshold
  /unwarn     (alias снять_варн)       — remove one warning
  /warnings   (alias предупреждения)   — list warnings for a user
  /pin        (alias закрепить)        — pin the replied-to message
  /unpin      (alias открепить)        — unpin a message
  /fine       (alias штраф)            — economy fine; requires EconomyMiddleware

Admin check (CRITICAL)
-----------------------
The caller must be either:

* a group admin *with moderation rights* — creator, or an administrator
  Telegram actually granted one of ``can_restrict_members`` /
  ``can_delete_messages`` / ``can_pin_messages`` / ``can_promote_members``
  (:func:`~telegram_invite_bot.utils.telegram_admin.has_moderation_rights`,
  porting ``bot.py:7455-7476``). A title-only administrator is NOT an
  admin for this purpose — see #337 — and falls through to the rank
  path below, exactly as it did in legacy,
  OR
* a bot developer (``settings.bot.is_developer(user_id)``),
  OR (ranks epic R4, DESIGN_RANKS.md §2.2 — strictly ADDITIVE widening)
* a ranked user whose rank's permission-matrix row grants the command's
  permission (``RankService.check``). Legacy parity: every moderation
  command in legacy gates on ``require_group_moderation(<can_*>)``
  (bot.py:31452 warn, 31589 unwarn, 31688 mute, 31788 unmute, 31841 ban,
  31957 kick, 31982 unban, 32207/32230 pin/unpin), whose precedence is
  developer → live-TG-admin → global rank vs matrix (bot.py:7555-7577).
  The TG-admin path here remains FIRST and byte-identical to the
  pre-R4 ``_require_admin`` — the rank path is consulted only where the
  old gate would have refused, so no existing admin loses anything.

An actor that is a chat rather than a person (Telegram's "Remain
anonymous") is judged before all three, and only by the first rule:
the chat's anonymous administrators must include at least one Telegram
granted a moderation right to (#883). There is no rank path for them —
a rank belongs to a user id, and the acting user id is exactly what
Telegram withholds.

Ranked (non-TG-admin) actors additionally pass through
``RankService.can_moderate`` target-guards on warn/mute/ban/kick
(self / chat-creator / target_rank >= actor_rank — legacy
``can_moderate``, bot.py:7580-7622). For TG-admin actors the
pre-existing ``_check_target_ok`` already enforces the self- and
creator-guards legacy applied to everyone (bot.py:7592-7605: the self
check and the creator probe sit BEFORE the developer allow): the self
check is explicit and the creator is caught by the target-is-admin
refusal (a creator always has status "creator"). That refusal is
deliberately *wider* than legacy — see :func:`_check_target_ok`.

Admin status is read live from Telegram on every handler call and not
cached (a short-lived in-process LRU would be stale across restarts and
wrong for operator permission changes). Note it is a *per-user* probe,
``await bot.get_chat_member(chat_id, user_id)``, not the chat-wide
``get_chat_administrators`` this paragraph claimed until #344 — one
Telegram API call per moderation action either way, which is acceptable
since moderation is infrequent.

Edge cases
----------
* Replied-to is the bot itself → refuse.
* Replied-to author is the caller → refuse (no self-mod).
* Target is a group admin → refuse (clean i18n error, not a raw API error).
* /ban duration parser (RR-4 #39): the /mute vocabulary plus the
  permanent words (0 / forever / навсегда / …) and a bare number read
  as HOURS, which is what the command help and the FAQ have always
  promised. No token = permanent; see :func:`handle_ban` for why that
  keeps diverging from legacy's 7-day default on purpose.
* /mute duration parser: 10s, 30m, 2h, 1d, 1w; no duration = the
  per-group default (``group_mod_config.mute_minutes``, default 1440 =
  legacy ``mute_duration`` 24h — bot.py:2530).
* /warn auto-ban at threshold (``group_mod_config.max_warns``, default
  3 = legacy ``max_warnings`` — bot.py:2529) IFF
  ``group_mod_config.autoban_enabled``; when autoban is off the warn is
  recorded and reported without sanctions (legacy bot.py:31498 for the
  reply form and bot.py:31566 for the args form — the
  ``AUTO_BAN_ON_MAX_WARNINGS and count >= max_w`` guard falls through
  to the plain ``warn_success`` reply).
* /fine max cap = FINE_MAX_AMOUNT = 100_000 coins.

Per-group config (L-43 follow-up)
---------------------------------
The warn threshold, autoban toggle and default mute duration are read
per-call from :class:`GroupModConfigRepo` (``moderation.group_mod_config``,
written by /modcfg). A group with no config row gets the synthesised
defaults view, which mirrors the previous hardcoded behaviour exactly.
``automod_enabled`` and ``profanity_enabled`` are not read here — they
are consumed by ``WordFilterAutomodMiddleware`` (handlers/wordfilter.py):
``profanity_enabled`` decides whether a matcher is built at all
(wordfilter.py:512), and ``automod_enabled`` gates the warn / auto-ban
chain that follows a deletion (wordfilter.py:700).

OUT OF SCOPE (stay in legacy): anti-spam ML, /setrules, role promotion.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command
from aiogram.types import ChatMemberUnion, Message
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.core.ranks import RankLevel, rank_name
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.moderation import ModerationMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from telegram_invite_bot.services.effect_gates import has_mute_protection
from telegram_invite_bot.services.rank_service import (
    REASON_DEVELOPER,
    REASON_RANK,
    REASON_TG_ADMIN,
    RankService,
    RankVerdict,
)
from telegram_invite_bot.utils.aiogram import command_body, require_from_user
from telegram_invite_bot.utils.chat_permissions import MUTED_PERMS, UNRESTRICTED_PERMS
from telegram_invite_bot.utils.html import html_user_mention
from telegram_invite_bot.utils.numbers import is_int_token
from telegram_invite_bot.utils.plural import plural
from telegram_invite_bot.utils.render import paginate_lines
from telegram_invite_bot.utils.telegram_admin import has_moderation_rights
from telegram_invite_bot.utils.telegram_kick import KickOutcome, kick_member

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo

log = logger.bind(component="handlers.moderation")

_ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}

# Mirrors legacy ``max_warnings`` default (bot.py:2529). Since L-43 this
# is only the *fallback default* — the effective per-group threshold is
# ``GroupModConfigRepo.get_or_default(chat_id).max_warns`` (whose
# defaults view carries this same value for unconfigured groups). Kept
# as a module constant because tests and the repo-defaults documentation
# reference it as the canonical default.
WARNING_THRESHOLD: int = 3

# Maximum fine in coins (economy safety cap).
FINE_MAX_AMOUNT: int = 100_000

# Duration suffixes for /mute parser.
#
# ``re.ASCII`` is load-bearing twice over (#1646), and neither reason is
# obvious from reading the pattern:
#
# * Under Unicode ``IGNORECASE`` Python case-FOLDS, so the literal ``s``
#   also matched ``ſ`` (U+017F LATIN SMALL LETTER LONG S). The match
#   then handed ``_parse_duration_seconds`` a unit of ``"ſ"`` — and
#   ``"ſ".lower()`` is ``"ſ"``, not ``"s"``, so the unguarded lookup in
#   ``_DURATION_MULTIPLIERS`` below raised ``KeyError``. ``/mute 30ſ``
#   was a 500-shaped failure, not a usage hint.
# * Without it ``\d`` matches all of category Nd, so ``/mute ٣٠m`` read
#   as thirty minutes. Every other numeric gate in ``handlers/`` goes
#   through ``utils.numbers.is_int_token``, which refuses Arabic-Indic
#   digits deliberately; this regex was the one that did not.
#
# The flag is the whole fix for both: it narrows ``\d`` and it turns
# ``IGNORECASE`` back into plain ASCII case-insensitivity.
_DURATION_RE = re.compile(
    r"^(\d+)\s*(s|sec|m|min|h|hr|d|day|w|week)s?$",
    re.IGNORECASE | re.ASCII,
)
_DURATION_MULTIPLIERS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "m": 60,
    "min": 60,
    "h": 3600,
    "hr": 3600,
    "d": 86400,
    "day": 86400,
    "w": 604800,
    "week": 604800,
}

# Telegram caps ``restrict_chat_member`` ``until_date`` at 366 days from
# now. A value beyond that (e.g. ``/mute 52w``) is rejected by the API
# with a generic Bad Request, surfacing to the user as the unhelpful
# "couldn't mute" copy. Clamp before the call so an over-long request
# degrades gracefully into the maximum finite mute Telegram allows.
_TELEGRAM_MAX_MUTE_SECONDS: int = 366 * 86400

# RR-4 #39 — /ban duration vocabulary.
#
# Tokens that mean "no expiry". Legacy's set (bot.py:31221/31249) plus
# nothing invented: an admin's muscle memory is the whole point of
# keeping them. Cyrillic belongs here — this is *input* an admin types,
# not display copy, so the en.yaml no-Cyrillic rule does not apply.
_PERMANENT_BAN_TOKENS: Final[frozenset[str]] = frozenset(
    {"0", "forever", "permanent", "permanently", "∞", "навсегда"}
)

# Sentinel for "banned with no expiry". 0 seconds is not a meaningful
# ban length, so it can carry the meaning without a second return
# channel — ``None`` already means "this token is not a duration".
BAN_PERMANENT: Final[int] = 0

# Telegram treats an ``until_date`` less than 30s or more than 366 days
# out as a permanent ban. Both edges are handled explicitly rather than
# left to the API: below the floor we round UP to a minute (a "/ban 5s"
# that silently became permanent would be the worst possible surprise),
# above the ceiling we call it permanent and SAY so in the reply.
_TELEGRAM_MIN_BAN_SECONDS: Final[int] = 60
_TELEGRAM_MAX_BAN_SECONDS: Final[int] = 366 * 86400

# A bare number means HOURS. Legacy's parser defaulted a unitless token
# to minutes (bot.py:31227) while legacy's own command help and the FAQ
# copy both promised hours ("/ban [часы]" at ru.yaml:881, "`/ban 168
# спам` — бан на неделю" at ru.yaml:883). That is a legacy bug, not a
# legacy convention: an admin who types 168 wants a week and would have
# got 2.8 hours, and the troll walks back in. The documented promise
# wins; every other unit stays available with an explicit suffix.
_BARE_BAN_NUMBER_UNIT_SECONDS: Final[int] = 3600

# #252 — /warn duration vocabulary.
#
# A warning's lifetime is stored in DAYS: ``add_warning`` takes
# ``expires_days`` and reads 0 there as "never expires"
# (``ModerationRepo.add_warning``). Legacy converted its parsed minutes
# with ``max(0, mins // (24 * 60)) or 30`` (bot.py:31484, and again at
# bot.py:31530 in the args-form twin of ``cmd_warn``): anything shorter
# than a day fell back to the default rather than becoming permanent,
# and a "/warn 12h" that silently never expired is the wrong surprise
# of the two. The ceiling is legacy's own (bot.py:31232).
_WARN_DEFAULT_EXPIRY_DAYS: Final[int] = 30
_WARN_MAX_EXPIRY_DAYS: Final[int] = 365


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lang(message: Message) -> str:
    code = (message.from_user.language_code or "ru") if message.from_user else "ru"
    return code[:2].lower()


async def _resolve_lang(
    message: Message,
    user_settings_repo: UserSettingsRepo,
) -> str:
    """Return the caller's effective bot language.

    M-M-5: previously used only ``message.from_user.language_code``
    (the Telegram client locale), ignoring the explicit choice the user
    made via ``/lang`` (stored in ``user_settings.language``). Every
    other handler in the new pipeline routes language through
    ``UserService.touch``/``UserSettingsRepo`` — moderation was the
    sole inconsistency, so a user with RU set in the bot saw moderation
    replies in their device locale (often EN for travellers).

    Resolution order mirrors ``UserEntity.language`` /
    ``get_user_language`` (bot.py:41778):

    1. ``user_settings.language`` (the persisted explicit choice).
    2. ``message.from_user.language_code`` first-two chars.
    3. ``"ru"`` (legacy default).
    """
    if message.from_user is not None:
        persisted = await user_settings_repo.get_language(message.from_user.id)
        if persisted in ("ru", "en"):
            return persisted
    return _lang(message)


def _parse_duration_seconds(token: str) -> int | None:
    """Return seconds for ``token`` (e.g. ``"10m"`` → 600), or ``None`` if unparseable."""
    m = _DURATION_RE.match(token.strip())
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2).lower()
    return value * _DURATION_MULTIPLIERS[unit]


def parse_ban_duration(token: str, *, allow_bare_number: bool) -> int | None:
    """Parse a ``/ban`` duration token (RR-4 #39, legacy bot.py:31212).

    Returns seconds for a finite ban, :data:`BAN_PERMANENT` for "no
    expiry", or ``None`` when the token is not a duration at all — that
    third case matters, because the same position may hold a target or
    the first word of a reason, and misreading it would eat part of what
    the admin wrote.

    Accepted: the /mute vocabulary (``30m``, ``2h``, ``7d``, ``1w`` and
    the long forms) and the permanent words in
    :data:`_PERMANENT_BAN_TOKENS`.

    ``allow_bare_number`` additionally reads a unitless number as HOURS
    — see :data:`_BARE_BAN_NUMBER_UNIT_SECONDS` for why hours and not
    legacy's minutes. It is only ever true where nothing else can live
    in that position, i.e. reply form, because a bare number is also
    exactly what a numeric user id looks like: in ``/ban 999999`` that
    number is a person, and legacy guarded the same collision by
    demanding an explicit unit outside reply form (bot.py:31263).

    Trailing punctuation is stripped so ``/ban 24, спам`` still reads as
    a day (legacy ``_moderation_clean_token``, bot.py:31252).
    """
    cleaned = (token or "").strip().strip(",;:.!?").lower()
    if not cleaned:
        return None
    if cleaned in _PERMANENT_BAN_TOKENS:
        return BAN_PERMANENT
    if is_int_token(cleaned):
        # A plain "0" was caught above as permanent; any other all-digit
        # token is an hour count — or a user id, hence the flag.
        return int(cleaned) * _BARE_BAN_NUMBER_UNIT_SECONDS if allow_bare_number else None
    seconds = _parse_duration_seconds(cleaned)
    if seconds is None:
        return None
    # "/ban 0h" — an explicit zero-length ban is meaningless; read it the
    # same way as the bare "0" the admin could have typed instead.
    return seconds if seconds > 0 else BAN_PERMANENT


def effective_ban_seconds(seconds: int) -> int:
    """Clamp a parsed ban length to what Telegram can actually express.

    :data:`BAN_PERMANENT` comes back when the admin asked for it, or
    asked for longer than Telegram's 366-day ceiling — which Telegram
    would have turned into a permanent ban regardless. Saying so is what
    keeps the reply honest instead of promising an expiry that never
    arrives. Anything under the 30-second floor rounds up to a minute
    for the same reason, in the other direction.
    """
    if seconds == BAN_PERMANENT or seconds > _TELEGRAM_MAX_BAN_SECONDS:
        return BAN_PERMANENT
    return max(seconds, _TELEGRAM_MIN_BAN_SECONDS)


def effective_mute_seconds(seconds: int) -> int:
    """Clamp a mute length to what Telegram honours as a TIMED restriction.

    The ceiling has always been here (an over-long ``until_date`` is
    rejected wholesale and surfaces as the unhelpful "couldn't mute"
    copy). #1734 added the floor, which is the dangerous edge: Telegram
    reads an ``until_date`` less than 30 seconds out as PERMANENT, so
    ``/mute 10s`` silenced a member forever while the reply, the audit
    row and the log line all reported ten seconds. Nobody re-checks a
    mute they have been told already expired.

    Unlike :func:`effective_ban_seconds` there is no
    :data:`BAN_PERMANENT` branch: a mute is always finite by
    construction, and ``group_events._restriction_is_captchas``
    discriminates a captcha hold from a moderator's mute on exactly
    that property.
    """
    return min(max(seconds, _TELEGRAM_MIN_BAN_SECONDS), _TELEGRAM_MAX_MUTE_SECONDS)


def parse_warn_expiry_days(token: str, *, allow_bare_number: bool) -> int | None:
    """Parse an optional ``/warn`` duration token into a day count (#252).

    ``None`` means "this token is not a duration at all" — the caller
    then leaves it where it stands, inside the reason, which is exactly
    what legacy did when its own parser answered "invalid"
    (bot.py:31486-31487). ``0`` means "never expires": that is the value
    :meth:`repositories.moderation_repo.ModerationRepo.add_warning`
    turns into a NULL ``expires`` column.

    The vocabulary is :func:`parse_ban_duration`'s, so ``/warn`` and
    ``/ban`` read the same token the same way — including the bare
    number, which this port counts as HOURS where legacy counted it as
    minutes (see :data:`_BARE_BAN_NUMBER_UNIT_SECONDS`). That divergence
    only becomes visible from a bare 24 upwards, since 24 hours is a day
    and 24 minutes is not, and legacy's own ``/warn`` help advertised
    suffixed tokens only ("``/warn [срок 7d/30d/0] [причина]``",
    bot.py:31475).
    """
    seconds = parse_ban_duration(token, allow_bare_number=allow_bare_number)
    if seconds is None:
        return None
    if seconds == BAN_PERMANENT:
        return 0
    days = seconds // 86400
    if days <= 0:
        # Sub-day request: legacy's ``or 30`` fallback, not permanence.
        return _WARN_DEFAULT_EXPIRY_DAYS
    return min(days, _WARN_MAX_EXPIRY_DAYS)


def parse_warning_id(token: str) -> int | None:
    """Parse an optional ``/unwarn`` warning-id token (#252).

    ``None`` means "not an id" — the caller then leaves the token where
    it stands, inside the reason. Legacy used a bare ``.isdigit()``
    (bot.py:31624, :31630) for the argument form; :func:`is_int_token` is
    used instead because #102 established that Unicode decimal digits
    pass ``str.isdigit()`` and then blow up ``int()`` ("²" is the
    canonical case). Ids are row ids, so ``0`` and negatives are never
    valid — a leading ``-`` also fails :func:`is_int_token`, which is
    what keeps ``/unwarn -5`` inside the reason instead of erroring.
    """
    if not is_int_token(token):
        return None
    value = int(token)
    return value if value > 0 else None


def _format_duration(seconds: int, lang: str) -> str:
    """Human-readable duration string for /mute success messages."""
    if seconds >= 86400:
        days = seconds // 86400
        return f"{days}д" if lang == "ru" else f"{days}d"
    if seconds >= 3600:
        hours = seconds // 3600
        return f"{hours}ч" if lang == "ru" else f"{hours}h"
    if seconds >= 60:
        mins = seconds // 60
        return f"{mins}мин" if lang == "ru" else f"{mins}m"
    return f"{seconds}с" if lang == "ru" else f"{seconds}s"


# R-FIX-007: ``_is_group_admin`` previously caught every exception and
# returned ``False``. That made the helper fail-OPEN in the target-check
# path (a Telegram 429/502 storm would silently classify an admin as a
# non-admin and let the issuer ban them) and fail-CLOSED in the actor
# path (correct). The two contexts now use two distinct helpers and a
# Telegram error during either is treated explicitly: actor-side the
# action is rejected; target-side (#249) the command is refused with an
# explicit "retry in a moment" rather than a fabricated "that user is an
# administrator" — see :func:`_probe_chat_member`.
async def _is_user_admin(bot: Bot, chat_id: int, user_id: int) -> bool | None:
    """Return True/False for confirmed moderation authority, ``None`` on API error.

    Since #337 this is the *narrow* predicate: an administrator with no
    moderation right at all answers False and drops into the rank path,
    which is where legacy put them
    (:func:`~telegram_invite_bot.utils.telegram_admin.has_moderation_rights`
    ports ``bot.py:7455-7476``). Do NOT reuse it to answer "is this
    person staff?" — that is
    :func:`~telegram_invite_bot.utils.telegram_admin.is_chat_admin_any`.

    Callers MUST handle ``None`` explicitly — leaking it into a truthy
    branch is the root cause of the fail-open bug.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return has_moderation_rights(member)
    except Exception as exc:  # noqa: BLE001 — re-surface via None contract below
        log.warning(
            "get_chat_member failed (chat={chat}, user={user}): {exc!r}",
            chat=chat_id,
            user=user_id,
            exc=exc,
        )
        return None


async def _probe_chat_member(bot: Bot, chat_id: int, user_id: int) -> ChatMemberUnion | None:
    """Fetch the target's chat membership once, or ``None`` on API error.

    #249: the target guard used to run two independent ``getChatMember``
    round-trips for the same (chat, user) — one asking "is it a bot",
    one asking "is it an admin" — and folded an API error on the second
    into the verdict "the target is an administrator". That reply stated
    something the bot had not established, and the two probes could
    disagree because they observed the chat at two different moments.
    One probe now answers both questions from one snapshot.

    M-M-3: the bot half of that pair exists because ``_resolve_target``
    cannot know whether a resolved @username / numeric id belongs to a
    bot (the users-table row has no such flag and Telegram bots carry
    ordinary usernames), while the legacy policy refuses bot targets in
    every command.

    Callers MUST handle ``None`` explicitly — the tri-state guard in
    ``tests/regression/test_authorization_gates.py`` enforces that with
    ``is`` / ``is not`` discrimination.
    """
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — re-surface via None contract
        log.warning(
            "get_chat_member (target probe) failed (chat={chat}, user={user}): {exc!r}",
            chat=chat_id,
            user=user_id,
            exc=exc,
        )
        return None


async def _require_admin(
    message: Message,
    bot: Bot,
    settings: Settings,
    lang: str | None = None,
) -> bool:
    """Reply with an error and return False if the caller is not an admin.

    R-FIX-011 / R-FIX-011-fp: an anonymous admin of *this* chat — the
    "Remain anonymous" posture Telegram routes through
    ``GroupAnonymousBot`` id=1087968824 with ``sender_chat`` set to the
    chat itself — is accepted IFF ``settings.bot.allow_anonymous_admin``
    is True AND ``getChatAdministrators`` reports at least one admin.
    Anonymous mode is the default posture for most "channel style"
    groups, so refusing it (iter-1 lockdown) is a UX regression vs.
    legacy. The audit row attributes the action to ``sender_chat.id``
    (the chat itself) with an ``anonymous=True`` marker, because
    Telegram intentionally hides which admin made the action.

    #246: a message sent on behalf of *another* chat — any member
    posting as a channel they own, which most supergroups allow — is
    refused outright. It carries a ``sender_chat`` too, but that proves
    only channel ownership; see :func:`_is_anonymous_admin`.

    R-FIX-007: a Telegram-API error during the caller-admin check is
    treated as fail-closed — we tell the user to retry, log the failure,
    and refuse the action.
    """
    tg_user = require_from_user(message)
    user_id = tg_user.id
    # M-M-5: callers thread the persisted-language-aware ``lang`` through;
    # only fall back to the Telegram-locale shortcut for legacy callsites
    # (none remain in-tree).
    if lang is None:
        lang = _lang(message)

    # R-FIX-011: actors that are a chat rather than a person are routed
    # first, because no per-user admin lookup can answer for them. Two
    # unrelated shapes land here and #246 turns on telling them apart:
    # an anonymous admin of *this* chat, and any member posting as a
    # channel they own.
    if _is_chat_backed_actor(message):
        if not _is_anonymous_admin(message):
            # #246: a foreign ``sender_chat`` (or a bare bot actor) is
            # not an admin and never was — see :func:`_is_anonymous_admin`.
            await message.reply(t("h_mod_channel_actor_refused", lang))
            log.bind(
                chat_id=message.chat.id,
                sender_chat_id=message.sender_chat.id if message.sender_chat else None,
                from_id=user_id,
            ).warning("moderation refused: acting on behalf of a foreign chat")
            return False
        if not settings.bot.allow_anonymous_admin:
            await message.reply(t("h_mod_anonymous_admin_refused", lang))
            log.bind(chat_id=message.chat.id, from_id=user_id).info(
                "moderation refused: anonymous admin / sender_chat actor "
                "(allow_anonymous_admin=False)",
            )
            return False
        # #883: this probe carries the #337 narrowing for the anonymous
        # path. Telegram deliberately hides WHICH admin acted, so the
        # only sound question is asked over the SET of possible actors:
        # the administrators of this chat who have "Remain anonymous"
        # switched on. If not one of them holds a moderation right, the
        # actor cannot hold one either, and a title-only administrator
        # is refused here exactly as ``_is_user_admin`` refuses one on
        # the named path below.
        #
        # The converse is not available. One rights-bearing anonymous
        # admin makes the whole set indistinguishable, so a title-only
        # admin in a chat that also has a real one still gets through.
        # That is as tight as the Bot API allows, and it is why the
        # audit row below is attributed to the chat rather than a human.
        #
        # #246 stays upstream and stays decisive for the OTHER shape:
        # ``sender_chat.id == chat.id`` is an identity Telegram offers
        # to admins only, which is what separates an anonymous admin
        # from a member posting as a channel they own.
        try:
            admins = await bot.get_chat_administrators(message.chat.id)
        except Exception as exc:  # noqa: BLE001 — re-surface via retry-later
            log.bind(chat_id=message.chat.id, exc=repr(exc)).warning(
                "get_chat_administrators failed for anonymous-admin verify",
            )
            await message.reply(t("h_mod_retry_later", lang))
            return False
        anonymous_admins = [m for m in admins if getattr(m, "is_anonymous", False)]
        if not any(has_moderation_rights(m) for m in anonymous_admins):
            # Subsumes the old "chat has no admins at all" refusal: an
            # empty list cannot satisfy ``any``. The reply's advice —
            # switch "Remain anonymous" off and retry — is right for
            # both readings, because a named actor is then judged by
            # ``_is_user_admin`` on their own rights.
            await message.reply(t("h_mod_anonymous_admin_refused", lang))
            log.bind(
                chat_id=message.chat.id,
                from_id=user_id,
                anonymous_admins=len(anonymous_admins),
            ).warning(
                "moderation refused: no anonymous admin of this chat holds a moderation right",
            )
            return False
        log.bind(
            chat_id=message.chat.id,
            actor_id=message.sender_chat.id if message.sender_chat else user_id,
            anonymous=True,
        ).info("moderation allowed: anonymous admin (verified via admin list)")
        return True

    if settings.bot.is_developer(user_id):
        return True

    status = await _is_user_admin(bot, message.chat.id, user_id)
    if status is None:
        # R-FIX-007: do NOT silently fall through to "not admin".
        await message.reply(t("h_mod_retry_later", lang))
        return False
    if status:
        return True

    await message.reply(t("h_mod_no_permission", lang))
    return False


async def _require_moderation(
    message: Message,
    bot: Bot,
    settings: Settings,
    ranks: RankService,
    permission: str,
    lang: str,
) -> RankVerdict | None:
    """Rank-aware moderation gate (ranks epic R4). Returns the allow
    verdict, or ``None`` after replying with the denial.

    STRICTLY WIDENING over :func:`_require_admin`: the legacy gate's
    decision sequence (anonymous-admin policy → developer → live
    TG-admin, incl. the R-FIX-007 fail-closed posture on a Telegram-API
    error) runs FIRST and unchanged; only where that gate would have
    refused do we consult ``RankService.check`` — legacy
    ``require_group_moderation`` precedence (bot.py:7555-7577), where a
    ranked user WITHOUT Telegram adminship may act and the BOT applies
    the action with its own admin rights.

    The returned verdict's ``reason`` tells the caller HOW the actor was
    authorised: ``REASON_RANK`` actors must additionally pass the
    :func:`_check_rank_target_ok` guard (legacy ``can_moderate``,
    bot.py:7580-7622); developer / TG-admin / anonymous-admin actors
    keep the pre-R4 target checks only. That last part is a DELIBERATE
    divergence from legacy, not parity — see :func:`_check_rank_target_ok`
    for why (#338).
    """
    tg_user = require_from_user(message)
    user_id = tg_user.id

    # Anonymous-admin actors (sender_chat / GroupAnonymousBot) have no
    # usable per-user rank — delegate to the unchanged legacy policy
    # (R-FIX-011). An allow is equivalent to the TG-admin path.
    if _is_chat_backed_actor(message):
        # _require_admin owns the whole policy, foreign-chat refusal
        # included (#246); this branch only routes to it.
        if not await _require_admin(message, bot, settings, lang):
            return None
        return RankVerdict(True, REASON_TG_ADMIN, RankLevel.USER)

    if settings.bot.is_developer(user_id):
        return RankVerdict(True, REASON_DEVELOPER, RankLevel.DEVELOPER)

    status = await _is_user_admin(bot, message.chat.id, user_id)
    if status is True:
        return RankVerdict(True, REASON_TG_ADMIN, RankLevel.USER)

    # R4 widening: the old gate would refuse here — first ask the rank
    # matrix. ``RankService.check`` is fail-CLOSED on grants (DB error →
    # rank 0 → no permission; its internal TG-admin re-probe never
    # grants on API error).
    verdict = await ranks.check(user_id, message.chat.id, permission, bot)
    if verdict.allowed:
        return verdict

    if status is None:
        # R-FIX-007 preserved verbatim: a Telegram-API error during the
        # caller-admin probe (for an actor the rank path could not
        # authorise either) stays a fail-closed "retry later".
        await message.reply(t("h_mod_retry_later", lang))
        return None

    await message.reply(t("h_mod_no_permission", lang))
    return None


async def _check_rank_target_ok(
    message: Message,
    bot: Bot,
    ranks: RankService,
    verdict: RankVerdict,
    target_id: int,
    lang: str,
) -> bool:
    """``can_moderate`` target-guards for RANK-authorised actors (R4).

    Applies legacy ``can_moderate`` (bot.py:7580-7622): self → chat
    creator → ``target_rank >= actor_rank``.

    #338 — DELIBERATE DIVERGENCE, and the reason it is deliberate.
    Legacy ran ``can_moderate`` for EVERY actor that cleared
    ``require_group_moderation`` (bot.py:31465, 31545, 31710, 31761,
    31858, 31936, 31963); only ``DEVELOPER_IDS`` skipped the rank
    comparison (bot.py:7608-7609). A Telegram admin was NOT exempt.
    We nonetheless exempt them, because a faithful port here would
    break moderation outright rather than tighten it:

    * legacy's rank auto-grant for Telegram admins lived in
      ``sync_ranks_with_telegram_admins`` (bot.py:7479-7519), called
      from exactly ONE place — the staff callback at bot.py:31091;
    * our port's equivalent, :func:`handlers.rank_self.lazy_staff_sync`,
      is likewise lazy (called only from ``/staff_me`` and the ``!rank``
      bang command) AND resolves against the MAIN chat only;
    * so an ordinary group admin has stored rank 0, and
      ``target_rank >= actor_rank`` is ``0 >= 0`` — every ``/ban``,
      ``/mute``, ``/warn`` and ``/kick`` would answer
      ``can_moderate_higher``. Legacy carried that same hole; we do not
      reproduce it.

    Residual accepted with this exemption: a Telegram admin can
    moderate a target holding a HIGHER bot rank, where legacy (for a
    synced admin) refused. It is bounded — :func:`_check_target_ok`
    already refuses any target who holds an admin status in that chat,
    so the reachable case is a ranked user with no Telegram adminship
    being moderated by the chat's own Telegram admin. In that chat, the
    Telegram grant is the stronger authority. Locked in by
    ``test_warn_tg_admin_can_target_higher_rank``.

    Returns ``True`` to proceed; replies with the localized denial
    (``can_moderate_self`` / ``can_moderate_creator`` /
    ``can_moderate_higher`` — the existing legacy-parity yaml keys) and
    returns ``False`` otherwise.
    """
    if verdict.reason != REASON_RANK:
        return True
    tg_user = require_from_user(message)
    mod = await ranks.can_moderate(tg_user.id, target_id, message.chat.id, bot)
    if mod.allowed:
        return True
    await message.reply(
        t(
            mod.reason,
            lang,
            target_name=rank_name(mod.target_rank or 0, lang, in_group=True),
            admin_name=rank_name(mod.actor_rank, lang, in_group=True),
        )
    )
    log.bind(
        chat_id=message.chat.id,
        actor=tg_user.id,
        target=target_id,
        reason=mod.reason,
    ).info("moderation refused by can_moderate target-guard")
    return False


def _actor_id(message: Message) -> int:
    """Return the id to record as the moderation actor.

    R-FIX-011-fp: for anonymous-admin actions we cannot identify which
    human admin acted (Telegram intentionally hides this), so we use
    ``sender_chat.id`` (the chat itself) as the audit-trail attribution
    with the ``anonymous=True`` log marker. For normal admin actions
    this is ``from_user.id``."""
    if message.sender_chat is not None:
        return message.sender_chat.id
    tg_user = require_from_user(message)
    return tg_user.id


# R-FIX-011: Telegram's well-known id for anonymous-admin actions in
# groups. Every anonymous admin in every group shows up as this same
# bot user — so we cannot use it to attribute actions. Documented at
# https://core.telegram.org/bots/api#message (see ``sender_chat``).
GROUP_ANONYMOUS_BOT_ID: int = 1087968824


def _is_chat_backed_actor(message: Message) -> bool:
    """Return True if the message was sent on behalf of a chat, not a person.

    Covers all three shapes: an explicit ``sender_chat``, the
    ``GroupAnonymousBot`` placeholder id, and any bot actor. None of
    them carries a human admin id, so none can be resolved against the
    per-user rank tables — they are routed to the anonymous policy in
    :func:`_require_admin` instead.
    """
    if message.sender_chat is not None:
        return True
    if message.from_user is not None and message.from_user.id == GROUP_ANONYMOUS_BOT_ID:
        return True
    # Bot actor without an explicit sender_chat → still not a human
    # admin; refuse defensively. (Real bots can't issue commands via
    # the message dispatch path anyway.)
    return bool(message.from_user is not None and message.from_user.is_bot)


def _is_anonymous_admin(message: Message) -> bool:
    """Return True only for an anonymous admin of *this* chat.

    #246. Being sent on behalf of a chat is not evidence of anything on
    its own. Telegram sets ``sender_chat`` in two unrelated situations:

    * an admin of this chat with "Remain anonymous" posting — then
      ``sender_chat.id == chat.id``, and Telegram itself has already
      checked that the sender is an admin, because nobody else is
      offered that identity;
    * **any member** posting as a channel they own, which most
      supergroups permit — then ``sender_chat`` is that foreign
      channel and proves only that the sender owns a channel.

    The old predicate accepted both and then "verified" the actor by
    asking whether the chat has any admins at all — a question that is
    true in every chat. A regular member could pick their channel in
    the "send as" chooser and issue ``/ban`` in reply to anyone.

    Note that ``from_user`` is present in both cases: the Bot API fills
    in a fake sender user for on-behalf-of-a-chat messages in groups,
    so the caller's ``from_user is not None`` assertion never caught
    this.
    """
    return message.sender_chat is not None and message.sender_chat.id == message.chat.id


def _extract_reason(message: Message, *, from_reply: bool, skip_arg_tokens: int = 0) -> str:
    """Extract the free-form reason from a /ban /kick /mute /warn /unwarn text.

    M-M-4: previously these commands discarded everything after the
    target argument; the audit log always wrote ``reason=""`` even when
    the operator typed one. Convention:

    * reply form: ``/ban <reason...>`` — everything after the command.
    * arg form:  ``/ban @user <reason...>`` — everything after the
      consumed target argument (and any earlier tokens, e.g. the
      duration token /mute consumes).

    ``skip_arg_tokens`` is the count of tokens *after* the command name
    that the caller has already consumed (1 for "@user", 2 for
    "10m @user", etc.). The remainder is joined and stripped.

    M-M-4 follow-up: the extracted reason is clamped to
    :data:`_REASON_MAX_LENGTH` characters. Telegram's body cap is
    4096 chars — an admin pasting a 4 KB blob (PII, accidental log
    paste) would otherwise land verbatim into ``moderation_log.reason``
    and stay there forever. The cap policy lives here, at the
    extraction layer, rather than in the schema (the column stays
    unbounded TEXT for forward-compat with a future longer policy).
    """
    parts = command_body(message).split(maxsplit=1 + skip_arg_tokens)
    # parts[0] is the command itself; parts[1..skip_arg_tokens] are the
    # already-consumed positional args. The reason is the trailing
    # remainder, which split's ``maxsplit`` left intact as the final
    # element if present.
    if from_reply:
        # No positional target arg, but there may still be consumed
        # leading tokens — RR-4 #39's ``/ban 7d спам`` is the first
        # caller to pass a non-zero count in reply form, and before it
        # did, the duration token leaked into the reason ("Причина: 7d")
        # and into the audit row.
        raw = parts[1 + skip_arg_tokens].strip() if len(parts) > 1 + skip_arg_tokens else ""
    elif len(parts) > 1 + skip_arg_tokens:
        # Arg form: reason starts after the consumed tokens.
        raw = parts[1 + skip_arg_tokens].strip()
    else:
        raw = ""
    return _clamp_reason(raw)


_REASON_MAX_LENGTH = 256
"""Hard cap on the persisted moderation reason. 256 chars is comfortably
beyond a legitimate single-line justification ("spam in #general for the
third time") and decisively below the Telegram body cap (4096), so a
careless paste of a chat log or PII blob is truncated at the extraction
boundary instead of being preserved verbatim in the audit log."""


def _clamp_reason(raw: str) -> str:
    """Truncate ``raw`` to :data:`_REASON_MAX_LENGTH` chars, append ``…``
    when truncated.

    The ellipsis (single ``…`` codepoint, not ``...``) sits *inside*
    the cap — the returned string is ``<= _REASON_MAX_LENGTH`` chars,
    so a downstream column sized to exactly the cap won't overflow.
    Truncation by codepoint, not bytes, because the persistence layer
    is unicode-native; a 256-char Cyrillic / emoji reason stays
    intact.
    """
    if len(raw) <= _REASON_MAX_LENGTH:
        return raw
    # Reserve one char for the ellipsis so total length stays <= cap.
    return raw[: _REASON_MAX_LENGTH - 1] + "…"


def _target_from_reply(message: Message) -> tuple[int, str, bool] | None:
    """Extract (user_id, display_name, is_bot) from a replied-to message.

    Returns ``None`` if there is no reply or the reply has no ``from_user``.
    """
    reply = message.reply_to_message
    if not reply or not reply.from_user:
        return None
    u = reply.from_user
    name = (u.first_name or "").strip() or str(u.id)
    return u.id, name, bool(u.is_bot)


async def _resolve_target(
    message: Message,
    users_repo: UsersRepo,
    lang: str,
    *,
    arg_index: int = 1,
) -> tuple[int, str] | None:
    """Resolve target from reply or @username arg.

    Returns ``(user_id, display_name)`` or replies with an error and returns None.

    ``arg_index`` is which post-command token holds the target. It is 1
    for every caller that takes no leading option; ``/ban 7d @user``
    (RR-4 #39) has already eaten a duration token and passes 2.
    """

    reply = message.reply_to_message
    if reply and reply.from_user:
        u = reply.from_user
        name = (u.first_name or "").strip() or str(u.id)
        return u.id, name

    # Try @username or raw user_id from command args
    parts = command_body(message).split()
    if len(parts) <= arg_index:
        await message.reply(t("h_mod_no_reply", lang))
        return None

    arg = parts[arg_index].lstrip("@")
    # Numeric user_id
    if is_int_token(arg):
        user_id = int(arg)
        row = await users_repo.get(user_id)
        name = (row.first_name or "").strip() if row else str(user_id)
        return user_id, name or str(user_id)

    # @username lookup
    row = await users_repo.get_by_username(arg)
    if row is None:
        await message.reply(t("h_mod_user_not_found", lang))
        return None
    name = (row.first_name or "").strip() or arg
    return row.user_id, name


async def _check_target_ok(
    message: Message,
    bot: Bot,
    target_id: int,
    target_is_bot: bool,
    lang: str,
) -> bool:
    """Guard the common edge cases and reply with i18n errors on violations.

    Returns True when it is safe to proceed.
    """
    tg_user = require_from_user(message)

    # Cheap local checks first: neither needs a network round-trip, and
    # both must still answer correctly while the Bot API is unreachable.
    #
    # M-M-3: ``target_is_bot`` carries the reply-path's known-truth (the
    # replied-to message's ``from_user.is_bot``). It is hardcoded to
    # ``False`` for the @username / numeric-id resolution paths because
    # the users-table row has no is_bot flag, so those paths lean on the
    # membership probe below instead.
    if target_is_bot:
        await message.reply(t("h_mod_target_is_bot", lang))
        return False

    # R-FIX-011-fp: self-check only applies to non-anonymous actors —
    # an anonymous admin's ``from_user.id`` is the GroupAnonymousBot
    # placeholder, which would never equal a real target id, so the
    # check is a no-op for them. Spelled out for readability.
    if message.sender_chat is None and target_id == tg_user.id:
        await message.reply(t("h_mod_target_is_self", lang))
        return False

    # #249: one probe answers both remaining questions — is the target a
    # bot, and is it protected. A Telegram-API error is NOT a permission
    # verdict: the old code replied "the target is an administrator",
    # which stated a fact the bot had never established and left the
    # issuer unable to tell a real admin from a 429. Refuse honestly and
    # invite a retry instead.
    #
    # #1544: collapsing every ``None`` cause (transport error, deleted
    # account, a user this chat has never seen) into one retry line was
    # checked against production before being left as is. The whole
    # retained journal of the service (2026-08-12 through 2026-09-06)
    # holds ZERO "get_chat_member failed" warnings, so this branch has
    # never fired once. Splitting it would add copy no issuer has read.
    member = await _probe_chat_member(bot, message.chat.id, target_id)
    if member is None:
        await message.reply(t("h_mod_retry_later", lang))
        return False

    if member.user.is_bot:
        await message.reply(t("h_mod_target_is_bot", lang))
        return False

    # Deliberately BROADER than legacy, and deliberately not the actor
    # predicate. Legacy protected only the creator here (bot.py:7601
    # refuses on ``status == "creator"`` and nothing else), so a plain
    # administrator was a legal target. Refusing every admin status
    # keeps the legacy creator-guard (a creator always reports
    # "creator") and additionally stops admin-on-admin moderation,
    # which is the safe direction: the cost of a false refusal is one
    # retry, the cost of a false allow is an admin banned by a peer.
    # This is why #337 narrowed ``_is_user_admin`` but left this on the
    # bare status set — the two checks fail in opposite directions.
    if member.status in _ADMIN_STATUSES:
        await message.reply(t("h_mod_target_is_admin", lang))
        return False

    return True


# ---------------------------------------------------------------------------
# /ban
# ---------------------------------------------------------------------------


async def handle_ban(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    ranks: RankService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Ban a user — ``/ban [duration] [@user] [reason]`` (RR-4 #39).

    The port had dropped legacy's ``[duration]`` argument (bot.py:31875)
    and every ban became permanent, which is a blunter instrument than
    the one the group's own FAQ describes. The token is back, parsed by
    :func:`parse_ban_duration`, in either position /mute accepts it —
    leading, or straight after the target — because those two commands
    sit next to each other in an admin's muscle memory.

    Deliberate divergence from legacy: no token still means PERMANENT,
    where legacy defaulted to 7 days. The port has shipped permanent-by-
    default since the cutover, and quietly turning every future bare
    ``/ban`` into a ban that lapses in a week is the dangerous direction
    of that change — the troll comes back and nobody is told. The FAQ
    already documents this default ("``/ban`` без числа — навсегда",
    ru.yaml:883), so the copy and the behaviour agree.

    Duration and reason both land in the reply; legacy printed only the
    duration and kept the reason to itself.
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    # R4: TG-admin OR rank-with-can_ban (legacy bot.py:31841).
    verdict = await _require_moderation(message, bot, settings, ranks, "can_ban", lang)
    if verdict is None:
        return

    parts = command_body(message).split()
    reply_info = _target_from_reply(message)
    in_reply = reply_info is not None

    # A leading duration token is consumed before anything else, so the
    # target resolver never sees it. A unitless number counts as one
    # only in reply form, where no target argument competes for that
    # slot — see :func:`parse_ban_duration`.
    requested: int | None = None
    arg_offset = 1
    if len(parts) > 1:
        requested = parse_ban_duration(parts[1], allow_bare_number=in_reply)
        if requested is not None:
            arg_offset = 2

    consumed_after_command = arg_offset - 1

    if reply_info is not None:
        target_id, target_name, is_bot = reply_info
    else:
        resolved = await _resolve_target(message, users_repo, lang, arg_index=arg_offset)
        if resolved is None:
            return
        target_id, target_name = resolved
        is_bot = False
        consumed_after_command += 1  # the target arg

        # Trailing form: /ban @user 7d — same courtesy /mute extends.
        # Still unit-only: a reason may perfectly well open with a
        # number ("/ban @user 3 раза спамил"), and eating it would both
        # shorten the ban and truncate the record.
        if requested is None and len(parts) > arg_offset + 1:
            trailing = parse_ban_duration(parts[arg_offset + 1], allow_bare_number=False)
            if trailing is not None:
                requested = trailing
                consumed_after_command += 1

    # M-M-4: everything the duration and the target did not consume.
    reason = _extract_reason(
        message,
        from_reply=reply_info is not None,
        skip_arg_tokens=consumed_after_command,
    )

    if not await _check_target_ok(message, bot, target_id, is_bot, lang):
        return
    if not await _check_rank_target_ok(message, bot, ranks, verdict, target_id, lang):
        return

    seconds = effective_ban_seconds(BAN_PERMANENT if requested is None else requested)
    until_date = (
        None if seconds == BAN_PERMANENT else datetime.now(UTC) + timedelta(seconds=seconds)
    )

    try:
        await bot.ban_chat_member(chat_id, target_id, until_date=until_date)
    except Exception as exc:
        log.warning("ban_chat_member failed: {exc!r}", exc=exc)
        await message.reply(t("h_mod_ban_fail", lang))
        return

    mention = html_user_mention(target_id, target_name)
    await moderation_repo.record_action(
        action="ban",
        user_id=target_id,
        admin_id=_actor_id(message),
        chat_id=chat_id,
        reason=reason,
        details=f"duration_seconds={seconds}",
    )
    # #1964: the ban is applied by Telegram and no rollback reaches it,
    # but the row that records who did it and why is still sitting in
    # the per-update transaction. A reply that raises (FloodWait, lost
    # send rights, the chat deleted under us) sends the middleware down
    # its rollback path (``middlewares/base.py``) while ``handlers/
    # errors.py`` consumes the exception and answers 200 — sanction
    # applied, audit row gone, nothing retries. Commit before the
    # round-trip: nothing after this line writes. Same placement as
    # /unban (#1866), /unwarn (#1875) and /fine (#493).
    if checkpoint is not None:
        await checkpoint()

    await message.reply(
        t(
            "h_mod_ban_success",
            lang,
            mention=mention,
            duration=(
                t("h_mod_ban_forever", lang)
                if seconds == BAN_PERMANENT
                else _format_duration(seconds, lang)
            ),
            reason=(t("h_mod_ban_reason", lang, reason=html.escape(reason)) if reason else ""),
        )
    )
    log.bind(
        chat_id=chat_id,
        target=target_id,
        admin=_actor_id(message),
        secs=seconds,
    ).info("/ban")


# ---------------------------------------------------------------------------
# /kick
# ---------------------------------------------------------------------------


async def handle_kick(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    ranks: RankService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Kick a user (ban + immediate unban so they can rejoin if invited).

    Four outcomes, four replies (#343, #2031): ``REMOVED`` is the clean
    kick, ``FAILED`` never removed anyone, ``LEFT_BANNED`` removed them
    but left the bounded ban standing, and ``ALREADY_BANNED`` refused
    because the target was banned before this command ran — see
    :mod:`utils.telegram_kick` for why each case exists and why none of
    them may be reported as a plain success.
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    # R4: TG-admin OR rank-with-can_kick (legacy bot.py:31957).
    verdict = await _require_moderation(message, bot, settings, ranks, "can_kick", lang)
    if verdict is None:
        return

    reply_info = _target_from_reply(message)
    if reply_info is not None:
        target_id, target_name, is_bot = reply_info
        reason = _extract_reason(message, from_reply=True)
    else:
        resolved = await _resolve_target(message, users_repo, lang)
        if resolved is None:
            return
        target_id, target_name = resolved
        is_bot = False
        reason = _extract_reason(message, from_reply=False, skip_arg_tokens=1)

    if not await _check_target_ok(message, bot, target_id, is_bot, lang):
        return
    if not await _check_rank_target_ok(message, bot, ranks, verdict, target_id, lang):
        return

    # #269: the ban+unban pair used to share one ``try``, so a lost
    # unban after a landed ban turned the kick into a permanent ban that
    # nothing here ever lifts. ``kick_member`` bounds the ban with an
    # expiry, retries the unban and reports the three cases apart.
    outcome = await kick_member(bot, chat_id, target_id)
    if outcome is KickOutcome.FAILED:
        await message.reply(t("h_mod_kick_fail", lang))
        return

    mention = html_user_mention(target_id, target_name)
    # #2031: the target was already banned, so the pair never ran. This
    # returns before the audit row on purpose: nothing happened, and a
    # row saying ``action="kick"`` for a no-op is the same misreport the
    # unban used to be. ``can_kick`` is not ``can_ban`` (``core/ranks.py``)
    # — the rank that reaches this line may have no business lifting the
    # standing ban, so point at the command that is gated on the right.
    if outcome is KickOutcome.ALREADY_BANNED:
        await message.reply(t("h_mod_kick_already_banned", lang, mention=mention))
        log.bind(
            chat_id=chat_id,
            target=target_id,
            admin=_actor_id(message),
            outcome=outcome.value,
        ).info("/kick refused — target already banned")
        return

    # M-M-4: capture trailing reason in audit log. #343: the outcome goes
    # in ``details`` too — ``LEFT_BANNED`` is the case the audit row has to
    # be able to answer later, because the ban outlived the command.
    await moderation_repo.record_action(
        action="kick",
        user_id=target_id,
        admin_id=_actor_id(message),
        chat_id=chat_id,
        reason=reason,
        details=outcome.value,
    )
    # #1964: durable before either confirmation branch — see /ban above.
    if checkpoint is not None:
        await checkpoint()

    # #343: ``LEFT_BANNED`` means the ban landed and all three unban
    # attempts did not — the user is out *and still banned*. Reporting the
    # plain success line here would defeat the third defence
    # ``utils.telegram_kick`` claims to provide ("the outcome is reported
    # honestly"): the admin would be told nothing needs fixing.
    if outcome is KickOutcome.LEFT_BANNED:
        await message.reply(t("h_mod_kick_left_banned", lang, mention=mention))
    else:
        await message.reply(t("h_mod_kick_success", lang, mention=mention))
    log.bind(
        chat_id=chat_id,
        target=target_id,
        admin=_actor_id(message),
        outcome=outcome.value,
    ).info("/kick")


# ---------------------------------------------------------------------------
# /mute
# ---------------------------------------------------------------------------


async def handle_mute(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    group_mod_config_repo: GroupModConfigRepo,
    settings: Settings,
    ranks: RankService,
    registry: EngineRegistry,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Restrict send permissions, optionally for a duration (e.g. /mute 10m).

    L-43: when no duration token is given the per-group default
    ``group_mod_config.mute_minutes`` applies (legacy ``MUTE_DURATION``,
    bot.py:3120 — legacy mutes were always finite). The previous
    "no token = indefinite" behaviour is gone.
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    # R4: TG-admin OR rank-with-can_mute (legacy bot.py:31688).
    verdict = await _require_moderation(message, bot, settings, ranks, "can_mute", lang)
    if verdict is None:
        return

    parts = command_body(message).split()

    # Parse optional duration from first arg (if it looks like a duration token)
    duration_seconds: int | None = None
    arg_offset = 1
    if len(parts) > 1:
        maybe_dur = _parse_duration_seconds(parts[1])
        if maybe_dur is not None:
            duration_seconds = maybe_dur
            arg_offset = 2

    # M-M-4: track how many post-command tokens were consumed so the
    # trailing remainder can be recorded as ``reason`` in the audit log.
    consumed_after_command = arg_offset - 1  # leading-duration tokens

    reply_info = _target_from_reply(message)
    if reply_info is not None:
        target_id, target_name, is_bot = reply_info
    else:
        if len(parts) <= arg_offset:
            await message.reply(t("h_mod_no_reply", lang))
            return
        arg = parts[arg_offset].lstrip("@")
        if not arg:
            await message.reply(t("h_mod_no_reply", lang))
            return
        consumed_after_command += 1  # the target arg

        # Try duration again in case user did /mute @user 10m (wrong order)
        if duration_seconds is None and len(parts) > arg_offset + 1:
            maybe_dur2 = _parse_duration_seconds(parts[arg_offset + 1])
            if maybe_dur2 is not None:
                duration_seconds = maybe_dur2
                consumed_after_command += 1  # trailing duration token

        if is_int_token(arg):
            target_id = int(arg)
            row = await users_repo.get(target_id)
            target_name = ((row.first_name or "").strip() if row else "") or str(target_id)
        else:
            row = await users_repo.get_by_username(arg)
            if row is None:
                await message.reply(t("h_mod_user_not_found", lang))
                return
            target_id, target_name = row.user_id, (row.first_name or "").strip() or arg
        is_bot = False

    if not await _check_target_ok(message, bot, target_id, is_bot, lang):
        return
    if not await _check_rank_target_ok(message, bot, ranks, verdict, target_id, lang):
        return
    # L-21 mute-protection item (legacy bot.py:31724/31766): an active
    # mute_protection privilege blocks the bot-side mute. Fails closed
    # (not protected) on an economy-DB read error.
    if await has_mute_protection(registry, target_id):
        await message.reply(t("h_mod_mute_protected", lang))
        return

    # M-M-4: extract trailing free-form reason after all consumed tokens.
    reason = _extract_reason(
        message,
        from_reply=reply_info is not None,
        skip_arg_tokens=consumed_after_command,
    )

    # L-43: no explicit duration → the group's configured default
    # (``mute_minutes``, legacy MUTE_DURATION 24h — bot.py:2530/3120)
    # instead of an indefinite restriction.
    if duration_seconds is None:
        cfg = await group_mod_config_repo.get_or_default(chat_id)
        duration_seconds = cfg.mute_minutes * 60

    # Clamp to both edges of what Telegram honours as timed: an
    # over-long mute is rejected wholesale and reports a generic
    # failure, and one under the 30s floor becomes PERMANENT (#1734).
    # ``dur_str`` below is computed from the same variable, so the
    # reply reports the clamped length rather than the asked-for one.
    duration_seconds = effective_mute_seconds(duration_seconds)
    until_date = datetime.now(UTC) + timedelta(seconds=duration_seconds)

    try:
        await bot.restrict_chat_member(
            chat_id,
            target_id,
            permissions=MUTED_PERMS,
            until_date=until_date,
        )
    except Exception as exc:
        log.warning("restrict_chat_member failed: {exc!r}", exc=exc)
        await message.reply(t("h_mod_mute_fail", lang))
        return

    mention = html_user_mention(target_id, target_name)
    await moderation_repo.record_action(
        action="mute",
        user_id=target_id,
        admin_id=_actor_id(message),
        chat_id=chat_id,
        reason=reason,
        details=f"duration_seconds={duration_seconds}",
    )
    # #1964: the restriction is already in force on Telegram's side.
    if checkpoint is not None:
        await checkpoint()

    dur_str = _format_duration(duration_seconds, lang)
    await message.reply(t("h_mod_mute_success", lang, mention=mention, duration=dur_str))
    log.bind(  # noqa: E501
        chat_id=chat_id,
        target=target_id,
        admin=_actor_id(message),
        secs=duration_seconds,
    ).info("/mute")


# ---------------------------------------------------------------------------
# /unmute
# ---------------------------------------------------------------------------


async def handle_unmute(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    ranks: RankService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Lift a mute by restoring send permissions. Reversal of /mute.

    Legacy ``cmd_unmute`` (bot.py:31782) is admin-gated
    (``require_group_moderation(can_unmute)``) and group-only. The new
    pipeline already gates /mute the same way via ``_require_admin`` and
    the router-level group filter, so /unmute mirrors that contract — a
    user the new /mute silenced would otherwise have no reversal command
    once the legacy bridge was deleted.

    R4 rank permission: ``can_mute`` — the default matrix
    (bot.py:2611-2712) carries no ``can_unmute`` row, so legacy's
    ``require_group_moderation("can_unmute")`` (bot.py:31788) resolved
    to False for every pure-rank actor; mapping the reversal to the
    escalation's own permission (per the approved R4 spec) is the
    sane widening — whoever may mute may unmute.

    #1679: there is deliberately no :func:`_check_rank_target_ok` call
    on this path, and it is legacy parity rather than an omission.
    ``bot.py`` calls ``can_moderate`` at exactly seven sites (31465,
    31545, 31710, 31761, 31858, 31936, 31963 — warn, mute, ban, kick);
    ``cmd_unmute`` (bot.py:31784) is not one of them, and neither are
    ``cmd_unban`` (31978) or ``cmd_unwarn`` (31585). Worth spelling out
    because the guard is easy to expect here: ``can_moderate``
    (bot.py:7580-7622) compares the actor's rank against the TARGET's
    rank, never against the rank of whoever imposed the restriction, so
    adding it would not stop a junior from lifting a senior's mute — it
    would only refuse unmuting a target who outranks the caller, which
    is the lenient direction. Self, the chat creator and every chat
    admin are already refused by :func:`_check_target_ok`.
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    verdict = await _require_moderation(message, bot, settings, ranks, "can_mute", lang)
    if verdict is None:
        return

    reply_info = _target_from_reply(message)
    if reply_info is not None:
        target_id, target_name, is_bot = reply_info
        reason = _extract_reason(message, from_reply=True)
    else:
        resolved = await _resolve_target(message, users_repo, lang)
        if resolved is None:
            return
        target_id, target_name = resolved
        is_bot = False
        reason = _extract_reason(message, from_reply=False, skip_arg_tokens=1)

    if not await _check_target_ok(message, bot, target_id, is_bot, lang):
        return

    try:
        # Grant every permission, with no ``until_date`` — that is the
        # documented way to lift a restriction outright and hand the
        # user back plain ``member`` status. The exact inverse of the
        # set /mute applies; see utils/chat_permissions.py for why both
        # spell out all sixteen fields.
        await bot.restrict_chat_member(
            chat_id,
            target_id,
            permissions=UNRESTRICTED_PERMS,
        )
    except Exception as exc:
        log.warning("unmute restrict_chat_member failed: {exc!r}", exc=exc)
        await message.reply(t("h_mod_unmute_fail", lang))
        return

    mention = html_user_mention(target_id, target_name)
    await moderation_repo.record_action(
        action="unmute",
        user_id=target_id,
        admin_id=_actor_id(message),
        chat_id=chat_id,
        reason=reason,
    )
    # #1964: the lift is already in force on Telegram's side.
    if checkpoint is not None:
        await checkpoint()

    await message.reply(t("h_mod_unmute_success", lang, mention=mention))
    log.bind(chat_id=chat_id, target=target_id, admin=_actor_id(message)).info("/unmute")


# ---------------------------------------------------------------------------
# /unban
# ---------------------------------------------------------------------------


async def _notify_unbanned(
    bot: Bot,
    chat_id: int,
    target_id: int,
    user_settings_repo: UserSettingsRepo,
    fallback_lang: str,
) -> None:
    """DM the unbanned user where to rejoin. Best effort, never fatal.

    Ported from legacy ``remove_ban`` (bot.py:9040-9068), link gate
    included: without a way back in, "you were unbanned" is an
    announcement the reader can do nothing with, so no link means no
    message. The legacy string is reused verbatim — it is already
    translated in both locales and says exactly this.

    The link comes from a plain ``get_chat`` read and nothing else: the
    public ``@username`` if the group has one, otherwise the primary
    ``invite_link`` the group's own admins created. No
    ``export_chat_invite_link`` — it *revokes* the primary link and
    mints a replacement, breaking every copy already pasted elsewhere
    (the same trade ``handlers/rating.py:291-295`` declined) — and no
    minting either: lifting a ban must not have the side effect of
    opening a new door into the group.

    ``chat.title`` is escaped. It is written by the group's own admins,
    the bot's default parse mode is HTML, and a title containing ``<``
    would otherwise silently drop the rest of the notice.

    The language is the target's own persisted choice rather than the
    moderator's, the same rule the /fine notice follows below.
    """
    try:
        chat = await bot.get_chat(chat_id)
        username = (chat.username or "").strip()
        link = f"https://t.me/{username}" if username else (chat.invite_link or "").strip()
        if not link:
            return
        persisted = await user_settings_repo.get_language(target_id)
        lang = persisted if persisted in ("ru", "en") else fallback_lang
        await bot.send_message(
            target_id,
            t(
                "unban_notify_body",
                lang,
                group_name=html.escape(chat.title or str(chat_id)),
                group_link=link,
            ),
            disable_web_page_preview=True,
        )
    except Exception as dm_exc:  # noqa: BLE001 — courtesy DM, never fatal
        log.debug("unban DM to {uid} failed: {exc!r}", uid=target_id, exc=dm_exc)


async def handle_unban(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    ranks: RankService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Lift a ban so the user can rejoin. Reversal of /ban.

    Legacy ``cmd_unban`` (bot.py:31976) is admin-gated
    (``require_group_moderation(can_unban)``) and group-only — the same
    contract /ban enforces in the new pipeline. A banned user can't reply
    in the chat, so resolution is by reply (rare) or @username / numeric
    id. ``only_if_banned=True`` keeps the call a no-op (rather than an
    error) if the target wasn't actually banned.

    #1780: a successful unban also clears the target's active warnings
    in this chat, so the reversal is a real reversal rather than one with
    a delayed fuse. See :meth:`ModerationRepo.clear_warnings`.

    R4 rank permission: ``can_ban`` — the default matrix has no
    ``can_unban`` row (same situation as /unmute above): the reversal
    maps to the escalation's permission per the approved R4 spec.

    #1866: the audit row and the warning clear are committed before the
    reply. The Telegram unban above them is irreversible and has
    already happened, so a rollback here is not symmetric — it puts the
    target back in the chat still carrying the warnings that got them
    banned, with nothing in ``moderation.db`` recording who lifted it,
    and the automod escalation then re-bans them permanently on their
    next filtered message with no grace (see the ``#1780`` note below).
    ``message.reply`` can fail benignly, and a benign reject tells the
    moderator nothing. :func:`handle_fine` in this module has taken the
    same checkpoint since #493.

    #1679: no :func:`_check_rank_target_ok` here either, for the reason
    written out at :func:`handle_unmute` — legacy's ``cmd_unban``
    (bot.py:31978) carries no ``can_moderate`` call, and the guard keys
    on the target's rank rather than on who imposed the ban.
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    verdict = await _require_moderation(message, bot, settings, ranks, "can_ban", lang)
    if verdict is None:
        return

    reply_info = _target_from_reply(message)
    if reply_info is not None:
        target_id, target_name, is_bot = reply_info
        reason = _extract_reason(message, from_reply=True)
    else:
        resolved = await _resolve_target(message, users_repo, lang)
        if resolved is None:
            return
        target_id, target_name = resolved
        is_bot = False
        reason = _extract_reason(message, from_reply=False, skip_arg_tokens=1)

    if not await _check_target_ok(message, bot, target_id, is_bot, lang):
        return

    # #252(15): was Telegram actually holding a ban? ``only_if_banned``
    # below makes the call a silent no-op when it was not, and legacy
    # notified the target regardless (bot.py:9040 has no such check).
    # That is a divergence on purpose: an unconditional notice both
    # confuses someone who was never banned and turns /unban into a way
    # to make the bot deliver the group's invite link to any user id an
    # admin names. A probe failure reads as "not banned" — the unban
    # itself still runs, only the courtesy DM is skipped.
    probed = await _probe_chat_member(bot, chat_id, target_id)
    was_banned = probed is not None and probed.status == ChatMemberStatus.KICKED

    try:
        await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
    except Exception as exc:
        log.warning("unban_chat_member failed: {exc!r}", exc=exc)
        await message.reply(t("h_mod_unban_fail", lang))
        return

    mention = html_user_mention(target_id, target_name)
    await moderation_repo.record_action(
        action="unban",
        user_id=target_id,
        admin_id=_actor_id(message),
        chat_id=chat_id,
        reason=reason,
    )
    # #1780: the warning slate goes with the ban. Leaving it means the
    # target rejoins still sitting at ``max_warns``, and the automod
    # escalation's re-ban branch (``wordfilter.py`` ``_escalate``) fires on
    # ``count >= max_warns`` *without* writing a new warning — so their very
    # next filtered message is a permanent re-ban with no grace, and nothing
    # in the DB explains it. The moderator has no reason to look: they just
    # "cleared" the ban. Legacy never cleared either; that divergence is the
    # point. Scoped to this chat, per :meth:`ModerationRepo.clear_warnings`.
    cleared = await moderation_repo.clear_warnings(
        user_id=target_id,
        chat_id=chat_id,
        admin_id=_actor_id(message),
        reason=reason,
    )
    # #1866: both writes above opened ``BEGIN IMMEDIATE`` on
    # ``moderation.db``; below are two Telegram round-trips. Commit
    # before them — nothing after this line writes, so there is no work
    # left to unwind (see :class:`db.session.Checkpoint`).
    if checkpoint is not None:
        await checkpoint()

    if cleared:
        await message.reply(
            t("h_mod_unban_success_warns_cleared", lang, mention=mention, count=cleared)
        )
    else:
        await message.reply(t("h_mod_unban_success", lang, mention=mention))
    if was_banned:
        await _notify_unbanned(bot, chat_id, target_id, user_settings_repo, lang)
    log.bind(chat_id=chat_id, target=target_id, admin=_actor_id(message)).info("/unban")


# ---------------------------------------------------------------------------
# /warn
# ---------------------------------------------------------------------------


async def handle_warn(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    group_mod_config_repo: GroupModConfigRepo,
    settings: Settings,
    ranks: RankService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Issue a warning — ``/warn [duration] [@user] [reason]`` (#252).

    The port had dropped legacy's ``[duration]`` argument
    (bot.py:31474-31487 in reply form, bot.py:31524-31531 in args form)
    and every warning expired on the repository's 30-day default, with
    the token the admin typed silently swallowed into the reason
    ("Причина: 7d"). It is back, in legacy's own leading position, and
    ``/warn 0`` once again means a warning that never lapses. See
    :func:`parse_warn_expiry_days` for the day conversion.

    L-43: the threshold and the autoban toggle come from the group's
    ``group_mod_config`` row (``/modcfg warns`` / ``/modcfg autoban``).
    With autoban off, the threshold-reaching warn is still recorded and
    reported, but no sanction is applied — mirroring legacy
    ``cmd_warn`` (bot.py:31498: ``if AUTO_BAN_ON_MAX_WARNINGS and
    count >= max_w`` falls through to the plain ``warn_success``
    reply when the toggle is off).

    #272.1: the ban is permanent, exactly as legacy's was in practice.
    Legacy passed ``duration_minutes=24 * 7 * 60`` to ``add_ban``
    (bot.py:31506, and again at bot.py:31574 in the args-form twin of
    ``cmd_warn``) and advertised "7 days" in its reply copy, but
    ``add_ban`` calls ``bot.ban_chat_member(chat_id, user_id)`` with no
    ``until_date`` (bot.py:8970) and the only expiry sweeper,
    ``cleanup_expired_bans`` (bot.py:9150), is dead code — it is never
    scheduled and never called. So legacy *advertised* a week and
    *delivered* forever; the port delivers forever and advertises
    nothing, which is the honest half of that pair. Making the auto-ban
    time-boxed is a product decision, not a port fix (#282).
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    # R4: TG-admin OR rank-with-can_warn (legacy bot.py:31452).
    verdict = await _require_moderation(message, bot, settings, ranks, "can_warn", lang)
    if verdict is None:
        return

    parts = command_body(message).split()
    reply_info = _target_from_reply(message)
    in_reply = reply_info is not None

    # #252: the leading duration token is consumed before the target
    # resolver ever sees it — legacy read it from exactly this position
    # in both forms. A unitless number counts as a duration only in
    # reply form, where no target argument competes for the slot.
    expires_days = _WARN_DEFAULT_EXPIRY_DAYS
    arg_offset = 1
    if len(parts) > 1:
        requested = parse_warn_expiry_days(parts[1], allow_bare_number=in_reply)
        if requested is not None:
            expires_days = requested
            arg_offset = 2

    consumed_after_command = arg_offset - 1

    if reply_info is not None:
        target_id, target_name, is_bot = reply_info
    else:
        resolved = await _resolve_target(message, users_repo, lang, arg_index=arg_offset)
        if resolved is None:
            return
        target_id, target_name = resolved
        is_bot = False
        consumed_after_command += 1  # the target arg

        # Trailing form: /warn @user 7d — the same courtesy /ban and
        # /mute extend. Unit-only, because a reason may perfectly well
        # open with a number ("/warn @user 3 раза спамил") and eating it
        # would both shorten the warning and truncate the record.
        if arg_offset == 1 and len(parts) > arg_offset + 1:
            trailing = parse_warn_expiry_days(parts[arg_offset + 1], allow_bare_number=False)
            if trailing is not None:
                expires_days = trailing
                consumed_after_command += 1

    # M-M-4: everything the duration and the target did not consume.
    reason = _extract_reason(
        message,
        from_reply=in_reply,
        skip_arg_tokens=consumed_after_command,
    )

    if not await _check_target_ok(message, bot, target_id, is_bot, lang):
        return
    if not await _check_rank_target_ok(message, bot, ranks, verdict, target_id, lang):
        return

    # L-43: per-group threshold/autoban from group_mod_config (defaults
    # view for unconfigured groups == the old hardcoded behaviour).
    cfg = await group_mod_config_repo.get_or_default(chat_id)
    threshold = cfg.max_warns

    current = await moderation_repo.get_warning_count(user_id=target_id, chat_id=chat_id)
    if current >= threshold:
        await message.reply(t("h_mod_warn_at_limit", lang, max=threshold))
        return

    try:
        # M-M-1: ``add_warning`` returns the post-insert count from the
        # same transaction. Using this returned value (instead of a
        # follow-up ``get_warning_count``) ensures only one of two
        # concurrent admins observes the threshold-crossing count,
        # so the auto-ban branch fires exactly once.
        # M-M-4: persist the trailing reason in both the warning row
        # and the audit-log row (``add_warning`` writes both).
        _wid, new_count = await moderation_repo.add_warning(
            user_id=target_id,
            chat_id=chat_id,
            admin_id=_actor_id(message),
            reason=reason,
            expires_days=expires_days,
        )
    except Exception as exc:
        # #1948: swallowing and returning is only honest because the repo
        # takes a SAVEPOINT around the whole write (R15). Without it the
        # ``warnings`` INSERT had already been flushed, and the session
        # middleware — which rolls back on a RAISED exception, never on a
        # return — committed it: the user was warned, no audit row said
        # so, and the admin read "couldn't warn". The other half was as
        # bad: a failure in the flush itself deactivates the transaction,
        # so the middleware's commit raised on the way out of an update
        # this branch had already reported cleanly.
        log.error("add_warning failed: {exc!r}", exc=exc)
        await message.reply(t("h_mod_warn_fail", lang))
        return

    # #1964: the worst of the seven, and the reason the commit lands
    # here rather than next to the reply. Everything below is a Telegram
    # round-trip — ``ban_chat_member`` at the threshold, then one of
    # three confirmations — and any of them may raise. The middleware
    # would then roll back the warning ITSELF, so the user ends up
    # banned at the threshold with a ``warnings`` table that says they
    # were never warned: the next moderator sees no history and no
    # reason for the ban. The count must stand once it is issued.
    if checkpoint is not None:
        await checkpoint()

    mention = html_user_mention(target_id, target_name)

    # #272.1: logged before the branch, not after it. The failed-auto-ban
    # path returns early, and that is precisely the case an operator
    # most needs to reconstruct — leaving the record behind the
    # ``return`` gave it the least context, not the most.
    log.bind(
        chat_id=chat_id,
        target=target_id,
        admin=_actor_id(message),
        count=new_count,
    ).info("/warn")

    # M-M-1: equality (not ``>=``) so only the warn that *crossed* the
    # threshold fires auto-ban. Under the read-decide-write race two
    # concurrent ``add_warning`` calls return counts ``N`` and ``N+1``;
    # equality at the threshold ensures the auto-ban path runs at
    # most once.
    # L-43: the sanction only applies when the group has autoban on;
    # otherwise the warn is recorded and reported without a ban
    # (legacy bot.py:31498).
    if new_count >= threshold and cfg.autoban_enabled:
        # Auto-ban — but only the warn that *crossed* the threshold
        # actually issues ``ban_chat_member``. A racing admin whose
        # add_warning lands second observes ``new_count > threshold``
        # and gets the same "user banned" reply but skips the API call
        # (the threshold-crossing call already banned them). That racer
        # reports the crossing call's *intent* without observing its
        # outcome — knowingly, and only in a window that needs two
        # admins inside the same instant.
        if new_count == threshold:
            # #272.1: the reply used to sit outside this ``try``, so a
            # bot without ban rights told the admin "user has been
            # banned" while nothing was banned. Legacy pre-checked with
            # ``bot_has_ban_rights`` and fell back to the plain warn
            # copy (bot.py:31499-31511); naming the failure is strictly
            # more useful than silently downgrading the reply.
            try:
                await bot.ban_chat_member(chat_id, target_id)
            except Exception as exc:
                log.warning("auto-ban after warn threshold failed: {exc!r}", exc=exc)
                await message.reply(
                    t(
                        "h_mod_warn_ban_failed",
                        lang,
                        mention=mention,
                        count=new_count,
                        max=threshold,
                    )
                )
                return
            # #272.1: this was the only ``/``-command sanction path in
            # the port that wrote no audit row — /ban, /kick, /mute,
            # /unmute and /unban all do. The two *automatic* sanction
            # paths have caught up since: antiflood's auto-mute writes
            # one (antiflood.py:332-375) and so does the captcha kick
            # (group_events.py:526-566). The captcha's opening restrict
            # (group_events.py:766-768) still writes none — it is a hold
            # pending a challenge, not a sanction.
            # Legacy's ``add_ban`` logged one too (bot.py:8982-8989).
            # Written only after the API call succeeds, so the ledger
            # records bans that happened.
            await moderation_repo.record_action(
                action="ban",
                user_id=target_id,
                admin_id=_actor_id(message),
                chat_id=chat_id,
                reason=reason,
                details=f"auto_ban_after_warns={new_count}",
            )
            # #1964 again: the auto-ban's own audit row, written after
            # the checkpoint above, needs the same treatment.
            if checkpoint is not None:
                await checkpoint()
        await message.reply(t("h_mod_warn_auto_ban", lang, mention=mention))
    else:
        await message.reply(
            t("h_mod_warn_success", lang, mention=mention, count=new_count, max=threshold)
        )


# ---------------------------------------------------------------------------
# /unwarn
# ---------------------------------------------------------------------------


async def handle_unwarn(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    group_mod_config_repo: GroupModConfigRepo,
    settings: Settings,
    ranks: RankService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Remove one warning from the target user — the last, or one by id.

    #252(7): ``/warnings`` prints ``#<id>`` for every row
    (:func:`handle_warnings`), so the UI has always advertised
    id-addressing; before this change ``/unwarn @user 5`` silently
    lifted the *most recent* warning instead of #5. Legacy accepted the
    id in both forms — reply (bot.py:31601-31608) and argument
    (bot.py:31622-31631) — and this restores both.

    Deliberate divergence from legacy's reply form: legacy did
    ``reason = parts[2:]`` unconditionally (bot.py:31608), so a
    non-numeric first word was swallowed and ``/unwarn извинился``
    logged the reason as "" — a token was lost whether or not it was an
    id. Here the token is only consumed when :func:`parse_warning_id`
    actually recognises it, so a wordy reason survives intact.

    R4 rank permission: ``can_remove_warn`` — the legacy default-matrix
    spelling (bot.py:2611-2712 carries ``can_remove_warn``; the
    ``can_unwarn`` name from the PERMISSIONS vocabulary at
    bot.py:6566-6603 has no matrix row, so gating on it would have made
    /unwarn unreachable for every pure-rank actor). Group admin (4) and
    above hold it by default; ranks 1-3 do not (bot.py:2611-2712).

    #1679: no :func:`_check_rank_target_ok` here either, for the reason
    written out at :func:`handle_unmute` — legacy's ``cmd_unwarn``
    (bot.py:31585) carries no ``can_moderate`` call, and the guard keys
    on the target's rank rather than on who issued the warning.

    #1875: the SUCCESS path is checkpointed, the refusal deliberately
    is not. :meth:`ModerationRepo.remove_warning_by_id` and
    :meth:`~ModerationRepo.remove_last_warning` are SELECT-first — they
    load the row and ``return False`` before running any write — so
    the ``not removed`` branch never opens ``BEGIN IMMEDIATE`` and has
    nothing to commit. On success the row is flipped to ``active=0``
    and an ``unwarn`` audit row is written, then two more reads and an
    unwrapped ``message.reply`` follow; a reject there used to put the
    warning back with the moderator told nothing at all, so the count
    they read next still carried the warning they had just lifted.
    :func:`handle_unban` above takes the same checkpoint (#1866).
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    verdict = await _require_moderation(message, bot, settings, ranks, "can_remove_warn", lang)
    if verdict is None:
        return

    parts = command_body(message).split()
    reply_info = _target_from_reply(message)
    if reply_info is not None:
        target_id, target_name, is_bot = reply_info
        # Reply form: ``/unwarn [id] [reason...]`` (bot.py:31601-31608).
        warning_id = parse_warning_id(parts[1]) if len(parts) > 1 else None
        reason = _extract_reason(
            message, from_reply=True, skip_arg_tokens=1 if warning_id is not None else 0
        )
    else:
        resolved = await _resolve_target(message, users_repo, lang)
        if resolved is None:
            return
        target_id, target_name = resolved
        is_bot = False
        # Arg form: ``/unwarn <@user|id> [id] [reason...]``
        # (bot.py:31622-31631). parts[1] is the target, already consumed
        # by :func:`_resolve_target`, so the id can only be parts[2].
        warning_id = parse_warning_id(parts[2]) if len(parts) > 2 else None
        reason = _extract_reason(
            message, from_reply=False, skip_arg_tokens=2 if warning_id is not None else 1
        )

    # R-FIX-006: ``/unwarn`` previously skipped the standard target
    # validation, letting admins flip ``active=False`` on a peer admin's
    # warning row (and on their own / on bots). Mirror /warn's guard so
    # all five state-mutating commands share the same admin/self/bot
    # refusal contract.
    if not await _check_target_ok(message, bot, target_id, is_bot, lang):
        return

    # M-M-4: trailing reason persists in the audit log so the operator
    # can record *why* the warning was lifted.
    if warning_id is not None:
        removed = await moderation_repo.remove_warning_by_id(
            warning_id=warning_id,
            user_id=target_id,
            chat_id=chat_id,
            admin_id=_actor_id(message),
            reason=reason,
        )
    else:
        removed = await moderation_repo.remove_last_warning(
            user_id=target_id,
            chat_id=chat_id,
            admin_id=_actor_id(message),
            reason=reason,
        )
    if not removed:
        # #252(7): distinguish "nothing to lift" from "no such id" — legacy
        # had a dedicated string for the id case (``unwarn_fail`` in
        # ``translations.py``: "нет такого варна или неверный ID"), and merging
        # the two would tell an operator who mistyped an id that the user is
        # clean when they are not.
        key = "h_mod_unwarn_no_such_id" if warning_id is not None else "h_mod_unwarn_none"
        await message.reply(t(key, lang))
        return

    # #1875: the removal and its audit row are the whole of the write;
    # everything below is two reads and a reply. Commit here so the
    # lifted warning cannot come back on a failed confirmation.
    if checkpoint is not None:
        await checkpoint()

    # L-43: report the count against the group's configured threshold.
    cfg = await group_mod_config_repo.get_or_default(chat_id)
    remaining = await moderation_repo.get_warning_count(user_id=target_id, chat_id=chat_id)
    await message.reply(t("h_mod_unwarn_success", lang, count=remaining, max=cfg.max_warns))
    log.bind(chat_id=chat_id, target=target_id, admin=_actor_id(message)).info("/unwarn")


# ---------------------------------------------------------------------------
# /warnings
# ---------------------------------------------------------------------------


async def handle_warnings(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    group_mod_config_repo: GroupModConfigRepo,
    settings: Settings,
    ranks: RankService,
) -> None:
    """List active warnings — your own, or a target's.

    R4 rank permission: ``can_warn`` — whoever may issue warnings may
    inspect a warning list (read-only companion of /warn). The legacy
    catalog agrees: the ``warnings`` row carries ``"default_rank": 2``
    (bot.py:42402), and that rank really was enforced —
    ``check_command_access`` (bot.py:42858-42894) reads it through
    ``get_command_required_rank`` (bot.py:42808) and is called from
    ``safe_handler`` (bot.py:1270), which decorates ``cmd_warnings``
    (bot.py:31658). An earlier revision of this docstring cited
    bot.py:42375-42385 for that claim; those ten rows are
    ``stats``/``chatinfo``/``chatstats``/``top_activity``/``ai``/
    ``support``/``feedback``/``ad``/``marry``/``marriage``, all
    ``default_rank: 0``, and contain no ``warnings`` entry at all. The
    conclusion held; the citation did not.

    Legacy took no target: ``cmd_warnings`` read ``tg_user.id``
    and never looked at an argument (bot.py:31657-31677), so
    ``/warnings`` meant "my own record". The port required a target and
    answered a bare ``/warnings`` with ``h_mod_no_reply`` — the one form
    legacy actually shipped was the one form that stopped working. The
    bare command is self-service again.

    Deliberate divergence: naming a target (reply or @username/id) is
    kept. Legacy never implemented it — its own /cmdcfg blurb
    (bot.py:42619) advertised "или у пользователя (ответом на
    сообщение)" that the handler ignored — and it stays behind the same
    ``can_warn`` gate the catalog put the whole command behind, so this
    widens what a moderator can read, never what a member can.
    """
    tg_user = require_from_user(message)
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id
    actor_id = tg_user.id

    verdict = await _require_moderation(message, bot, settings, ranks, "can_warn", lang)
    if verdict is None:
        return

    reply_info = _target_from_reply(message)
    if reply_info is not None:
        target_id, target_name = reply_info[0], reply_info[1]
    elif len(command_body(message).split()) > 1:
        resolved = await _resolve_target(message, users_repo, lang)
        if resolved is None:
            return
        target_id, target_name = resolved
    else:
        # Bare /warnings: legacy's only form (bot.py:31663).
        target_id = actor_id
        target_name = (tg_user.first_name or "").strip() or str(actor_id)

    rows = await moderation_repo.list_warnings(user_id=target_id, chat_id=chat_id)
    count = len(rows)
    mention = html_user_mention(target_id, target_name)

    if count == 0:
        # ``h_mod_warnings_none`` is third-person ("у пользователя"), which
        # reads as a bug when you asked about yourself.
        key = "h_mod_warnings_none_self" if target_id == actor_id else "h_mod_warnings_none"
        await message.reply(t(key, lang))
        return

    # L-43: header shows the group's configured threshold.
    cfg = await group_mod_config_repo.get_or_default(chat_id)
    # ``list_warnings`` returns up to 20 rows and each reason is clamped
    # to ``_REASON_MAX_LENGTH`` (256) — a full list of verbose warnings
    # is ~5700 characters, i.e. past Telegram's 4096 ceiling, and the
    # reply came back a 400 the moderator never saw. ``max_warns`` goes
    # up to 20 and auto-ban can be off, so that many active warnings is
    # a configuration, not an abuse case.
    pages = paginate_lines(
        t("h_mod_warnings_header", lang, mention=mention, count=count, max=cfg.max_warns),
        [
            t(
                "h_mod_warnings_row",
                lang,
                id=w.id,
                reason=html.escape(w.reason or "—"),
                date=w.date.strftime("%Y-%m-%d"),
            )
            for w in rows
        ],
        more_line=lambda left: t("h_mod_warnings_more", lang, count=left),
    )
    await message.reply(pages[0])
    # Continuations are plain messages: only the first page quotes the
    # command, same as /filter_list.
    for page in pages[1:]:
        await message.answer(page)


# ---------------------------------------------------------------------------
# /pin
# ---------------------------------------------------------------------------


async def handle_pin(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    ranks: RankService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Pin the replied-to message.

    R4 rank permission: ``can_pin`` (legacy bot.py:32207).
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    verdict = await _require_moderation(message, bot, settings, ranks, "can_pin", lang)
    if verdict is None:
        return

    reply = message.reply_to_message
    if not reply:
        await message.reply(t("h_mod_pin_no_reply", lang))
        return

    try:
        await bot.pin_chat_message(chat_id, reply.message_id)
    except Exception as exc:
        log.warning("pin_chat_message failed: {exc!r}", exc=exc)
        await message.reply(t("h_mod_pin_fail", lang))
        return

    # M-M-2: /pin acts on a *message*, not a user. Writing the reply
    # author's id as ``user_id`` mixed "this user was targeted" semantics
    # into the audit log, and writing ``0`` when ``reply.from_user``
    # was None collided with /unpin's sentinel (both wrote ``user_id=0``,
    # making it impossible to distinguish them in queries). NULL is the
    # honest representation; the actual pin target lives in ``details``.
    await moderation_repo.record_action(
        action="pin",
        user_id=None,
        admin_id=_actor_id(message),
        chat_id=chat_id,
        details=f"message_id={reply.message_id}",
    )
    # #1964: the message is already pinned — see /ban above.
    if checkpoint is not None:
        await checkpoint()

    await message.reply(t("h_mod_pin_success", lang))
    log.bind(chat_id=chat_id, msg=reply.message_id, admin=_actor_id(message)).info("/pin")


# ---------------------------------------------------------------------------
# /unpin
# ---------------------------------------------------------------------------


async def handle_unpin(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    ranks: RankService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Unpin the replied-to message (or the latest pinned if no reply).

    R4 rank permission: ``can_pin`` — legacy gates /unpin on the same
    permission as /pin (bot.py:32230).
    """
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    verdict = await _require_moderation(message, bot, settings, ranks, "can_pin", lang)
    if verdict is None:
        return

    reply = message.reply_to_message
    try:
        if reply:
            await bot.unpin_chat_message(chat_id, message_id=reply.message_id)
        else:
            await bot.unpin_chat_message(chat_id)
    except Exception as exc:
        log.warning("unpin_chat_message failed: {exc!r}", exc=exc)
        await message.reply(t("h_mod_unpin_fail", lang))
        return

    # M-M-2: /unpin has no human target. NULL replaces the previous
    # ``user_id=0`` sentinel, which collided with /pin's "lost reply
    # author" sentinel (see /pin above).
    await moderation_repo.record_action(
        action="unpin",
        user_id=None,
        admin_id=_actor_id(message),
        chat_id=chat_id,
        details=f"message_id={reply.message_id}" if reply else None,
    )
    # #1964: the message is already unpinned — see /ban above.
    if checkpoint is not None:
        await checkpoint()

    await message.reply(t("h_mod_unpin_success", lang))
    log.bind(chat_id=chat_id, admin=_actor_id(message)).info("/unpin")


# ---------------------------------------------------------------------------
# /fine
# ---------------------------------------------------------------------------


async def handle_fine(
    message: Message,
    bot: Bot,
    moderation_repo: ModerationRepo,
    economy_repo: EconomyRepo,
    transactions_repo: TransactionsRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Economy fine: deduct coins from a user's wallet.

    Only available to bot developers (not generic group admins) —
    mirrors legacy ``bot.py:3674`` which gates on ``DEVELOPER_IDS``.
    Requires the reply-to form; @username/ID form is supported too.
    """
    tg_user = require_from_user(message)
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id
    caller_id = tg_user.id

    # /fine is developer-only, not generic admin
    if not settings.bot.is_developer(caller_id):
        await message.reply(t("h_mod_no_permission", lang))
        return

    parts = command_body(message).split()

    reply_info = _target_from_reply(message)
    if reply_info is not None:
        target_id, target_name, is_bot = reply_info
        # Amount is the next token after the command name
        if len(parts) < 2 or not is_int_token(parts[1]):
            await message.reply(t("h_mod_fine_invalid_amount", lang, max=FINE_MAX_AMOUNT))
            return
        amount = int(parts[1])
        reason = " ".join(parts[2:]).strip()
    else:
        # /fine @user <amount> <reason> OR /fine <user_id> <amount> <reason>
        if len(parts) < 4:
            await message.reply(t("h_mod_fine_invalid_amount", lang, max=FINE_MAX_AMOUNT))
            return
        arg = parts[1].lstrip("@")
        if is_int_token(arg):
            target_id = int(arg)
            row = await users_repo.get(target_id)
            target_name = ((row.first_name or "").strip() if row else "") or str(target_id)
        else:
            row = await users_repo.get_by_username(arg)
            if row is None:
                await message.reply(t("h_mod_user_not_found", lang))
                return
            target_id, target_name = row.user_id, (row.first_name or "").strip() or arg
        if not is_int_token(parts[2]):
            await message.reply(t("h_mod_fine_invalid_amount", lang, max=FINE_MAX_AMOUNT))
            return
        amount = int(parts[2])
        reason = " ".join(parts[3:]).strip()
        is_bot = False

    if is_bot:
        await message.reply(t("h_mod_fine_target_is_bot", lang))
        return

    if settings.bot.is_developer(target_id):
        await message.reply(t("h_mod_fine_target_is_dev", lang))
        return

    if amount < 1 or amount > FINE_MAX_AMOUNT:
        await message.reply(t("h_mod_fine_invalid_amount", lang, max=FINE_MAX_AMOUNT))
        return

    if not reason:
        await message.reply(t("h_mod_fine_no_reason", lang))
        return

    wallet = await economy_repo.get(target_id)
    if wallet is None or wallet.balance <= 0:
        await message.reply(t("h_mod_fine_no_wallet", lang))
        return

    deducted = min(amount, wallet.balance)
    updated = await economy_repo.debit(target_id, deducted)
    if updated is None:
        # #1879: ``debit`` is a guarded UPDATE, so it took the economy
        # write lock in order to tell us it matched no row. Nothing was
        # written and there is nothing to unwind, but the lock is real —
        # let go of it before the refusal reply instead of holding it
        # across a Telegram round-trip.
        if checkpoint is not None:
            await checkpoint()
        await message.reply(t("h_mod_fine_fail", lang))
        return

    # Ledger row. ``to_id`` is NULL because a fine BURNS the coins —
    # nothing was credited to anyone, least of all the admin who typed
    # the command. Naming them here would show the fine as income on
    # their ``/profile`` finances panel and in their weekly "received"
    # total; the admin who imposed it is recorded in the moderation
    # log below, which is where that belongs.
    await transactions_repo.record(
        from_id=target_id,
        to_id=None,
        amount=deducted,
        reason=f"fine: {reason}",
        type="fine",
    )

    # Audit detail: when the wallet couldn't cover the full fine the
    # deduction is clamped to the balance. Record BOTH the actually-
    # deducted amount and the admin's *requested* amount so the audit
    # trail shows intent vs. outcome — otherwise a "fine 100000, only
    # 200 taken" event is indistinguishable from a deliberate 200 fine.
    if deducted < amount:
        fine_details = f"amount={deducted} requested={amount} clamped_to_balance"
    else:
        fine_details = f"amount={deducted}"
    await moderation_repo.record_action(
        action="fine",
        user_id=target_id,
        admin_id=caller_id,
        chat_id=chat_id,
        reason=reason,
        details=fine_details,
    )

    # #493: everything above this line is written, and both writes
    # opened ``BEGIN IMMEDIATE`` — the debit and ledger row on
    # ``economy.db``, the audit row on ``moderation.db``. Below it are
    # two Telegram round-trips (the reply, then the target's DM), and
    # without this commit both locks would be held across them until the
    # session middleware unwinds (``middlewares/base.py:157-158``). A
    # blocked target or a FloodWait would then stall every other user's
    # economy write until ``busy_timeout`` expired — the exact failure
    # ``db/session.py`` describes. The checkpoint is correct here in the
    # sense that class requires: the fine has landed and must stand
    # regardless of whether the notifications get through.
    if checkpoint is not None:
        await checkpoint()

    mention = html_user_mention(target_id, target_name)
    await message.reply(
        t(
            "h_mod_fine_success",
            lang,
            amount=deducted,
            noun=plural(deducted, "h_plural_coins", lang),
            mention=mention,
            reason=html.escape(reason),
            balance=updated.balance,
        )
    )

    # Notify the target user in their DM (best-effort, silent on failure).
    # M-M-5: use the target's persisted ``user_settings.language`` so the
    # fine notice arrives in the language they explicitly chose via /lang,
    # not in the issuing admin's locale.
    try:
        persisted_target_lang = await user_settings_repo.get_language(target_id)
        target_lang = persisted_target_lang if persisted_target_lang in ("ru", "en") else lang
        notify_text = t(
            "h_mod_fine_notify",
            target_lang,
            amount=deducted,
            noun=plural(deducted, "h_plural_coins", target_lang),
            reason=html.escape(reason),
            balance=updated.balance,
        )
        await bot.send_message(target_id, notify_text)
    except Exception as dm_exc:  # noqa: BLE001
        log.debug("fine DM to {uid} failed: {exc!r}", uid=target_id, exc=dm_exc)

    log.bind(chat_id=chat_id, target=target_id, admin=caller_id, amount=deducted).info("/fine")


# ---------------------------------------------------------------------------
# Session middleware (moderation.db) — injects GroupModConfigRepo
# ---------------------------------------------------------------------------


class _GroupModConfigMiddleware(BaseSessionMiddleware):
    """Open one ``moderation`` session per update; expose the config repo.

    L-43: mirrors the private middleware in :mod:`handlers.modcfg` (same
    DB, same lifecycle) — duplicated here because modcfg's class is
    private to its module and both files are separately owned. This
    middleware runs alongside :class:`ModerationMiddleware` on the same
    router, so two ``moderation.db`` sessions exist per update; the
    config session only ever READS (``get_or_default``), so there is no
    write-lock or cross-session-consistency concern under SQLite WAL.
    """

    def __init__(self, registry: EngineRegistry) -> None:
        super().__init__(registry, DBName.MODERATION)

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        data["group_mod_config_repo"] = GroupModConfigRepo(session)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the moderation router.

    Middlewares:
    * :class:`ModerationMiddleware` — injects ``moderation_repo`` (moderation.db).
    * :class:`_GroupModConfigMiddleware` — injects ``group_mod_config_repo``
      (moderation.db, read-only here) for the L-43 per-group config consumed
      by /warn, /unwarn, /warnings and /mute.
    * :class:`SessionMiddleware` — injects ``users_repo`` (for @username lookups).
    * :class:`EconomyMiddleware` — injects ``economy_repo`` etc. (for /fine only;
      zero-cost for the other eight commands because aiogram still runs the
      middleware on every update matched to this router, but the overhead is
      a single SQLite connection open/close, which is acceptable given that
      moderation commands are rare).

    Group-only filter: private-chat invocations get the #123 refusal twin.
    /fine is the one exception — see its registration below.

    Handlers are registered as inner closures that capture ``settings`` from this
    scope rather than relying on aiogram DI — the same pattern used by all other
    handler modules in this codebase (see ``referral.py``, ``admin/cpu.py``, etc.).
    """
    router = Router(name="moderation")
    router.message.middleware(ModerationMiddleware(registry))
    router.message.middleware(_GroupModConfigMiddleware(registry))
    router.message.middleware(SessionMiddleware(registry))
    router.message.middleware(EconomyMiddleware(registry))

    group_filter = F.chat.type.in_(GROUP_TYPES)

    # R4: one stateless RankService over the APP-scoped registry,
    # captured by every closure (it opens its own short sessions
    # internally — see services/rank_service.py module docstring).
    ranks = RankService(registry, settings)

    # /ban
    async def _ban(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_ban(
            message,
            bot,
            moderation_repo,
            users_repo,
            user_settings_repo,
            settings,
            ranks,
            checkpoint,
        )

    router.message.register(
        _ban,
        # ``kom_ban``: the multi-bot spelling legacy registered and the
        # guide still documents. ``kom_unban`` below already had it —
        # the positive halves were simply missed (#116).
        Command("ban", "бан", "kom_ban", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /kick
    async def _kick(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_kick(
            message,
            bot,
            moderation_repo,
            users_repo,
            user_settings_repo,
            settings,
            ranks,
            checkpoint,
        )

    router.message.register(
        _kick,
        Command("kick", "кик", "kom_kick", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /mute
    async def _mute(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        group_mod_config_repo: GroupModConfigRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_mute(
            message,
            bot,
            moderation_repo,
            users_repo,
            user_settings_repo,
            group_mod_config_repo,
            settings,
            ranks,
            registry,
            checkpoint,
        )

    router.message.register(
        _mute,
        Command("mute", "мут", "kom_mute", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /unmute
    async def _unmute(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_unmute(
            message,
            bot,
            moderation_repo,
            users_repo,
            user_settings_repo,
            settings,
            ranks,
            checkpoint,
        )

    router.message.register(
        _unmute,
        Command("unmute", "размут", "kom_unmute", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /unban
    async def _unban(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_unban(
            message,
            bot,
            moderation_repo,
            users_repo,
            user_settings_repo,
            settings,
            ranks,
            checkpoint,
        )

    router.message.register(
        _unban,
        Command("unban", "разбан", "kom_unban", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /warn
    async def _warn(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        group_mod_config_repo: GroupModConfigRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_warn(
            message,
            bot,
            moderation_repo,
            users_repo,
            user_settings_repo,
            group_mod_config_repo,
            settings,
            ranks,
            checkpoint,
        )

    router.message.register(
        _warn,
        Command("warn", "варн", "предупреждение", "kom_warn", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /unwarn
    async def _unwarn(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        group_mod_config_repo: GroupModConfigRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_unwarn(
            message,
            bot,
            moderation_repo,
            users_repo,
            user_settings_repo,
            group_mod_config_repo,
            settings,
            ranks,
            checkpoint,
        )

    router.message.register(
        _unwarn,
        Command(
            "unwarn",
            "снять_варн",
            "разварн",
            "снять_предупреждение",
            "kom_unwarn",
            ignore_case=True,
        ),
        F.from_user,
        group_filter,
    )

    # /warnings
    async def _warnings(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        group_mod_config_repo: GroupModConfigRepo,
    ) -> None:
        await handle_warnings(
            message,
            bot,
            moderation_repo,
            users_repo,
            user_settings_repo,
            group_mod_config_repo,
            settings,
            ranks,
        )

    router.message.register(
        _warnings,
        Command("warnings", "warns", "предупреждения", "варны", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /pin
    async def _pin(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_pin(
            message, bot, moderation_repo, user_settings_repo, settings, ranks, checkpoint
        )

    router.message.register(
        _pin,
        Command("pin", "закрепить", "kom_pin", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /unpin
    async def _unpin(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_unpin(
            message, bot, moderation_repo, user_settings_repo, settings, ranks, checkpoint
        )

    router.message.register(
        _unpin,
        Command("unpin", "открепить", "kom_unpin", ignore_case=True),
        F.from_user,
        group_filter,
    )

    # /fine
    async def _fine(
        message: Message,
        bot: Bot,
        moderation_repo: ModerationRepo,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_fine(
            message,
            bot,
            moderation_repo,
            economy_repo,
            transactions_repo,
            users_repo,
            user_settings_repo,
            settings,
            checkpoint=checkpoint,
        )

    # #252(16): the only registration here with no ``group_filter``.
    # Legacy registered /fine with no chat-type filter at all
    # (bot.py:3672) and the handler never reads the chat: it takes a
    # user id or @username, moves coins in the global economy wallet and
    # DMs the target. A developer fining someone by id had to walk into
    # a group to do it, which the legacy contract never asked of them.
    # ``penalty`` is the third legacy spelling, dropped in the port.
    #
    # ``chat_id`` still reaches the moderation log, and in a DM that is
    # the developer's own chat — an honest record of where the command
    # was typed, which is all that column has ever claimed.
    #
    # The words are listed in ``chat_scope._TWO_SIDED_COMMANDS`` so the
    # wrapper below does not register a "group only" refusal that would
    # shadow this handler in a DM.
    router.message.register(
        _fine,
        Command("fine", "штраф", "penalty", ignore_case=True),
        F.from_user,
    )

    return with_chat_type_refusal(router, scope="group")
