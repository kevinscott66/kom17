"""Group onboarding — bot-added notice, new-member welcome, join captcha (L-55).

Passive, group-only handlers — the ones that fire without anybody
typing anything. (``build_router`` also registers the ``/welcome*``
admin commands and the captcha button; those are not passive.)

* **The bot's own membership changed** — fires on every
  ``my_chat_member`` update about the bot account. Joining (or being
  promoted) records the chat in ``users.bot_groups``; being removed
  clears that row's ``is_active`` flag, so a group the bot was kicked
  out of stops being something users can buy *for* (#111). On an actual
  join we additionally post a short group notice (greeting + an
  admin-rights / moderation nudge + a deep-link "open in private"
  button) and best-effort DM the member who added us. Any send failure
  (no rights to post, adder blocked the bot, anonymous adder) is
  swallowed — onboarding must never raise, and the row is written
  *before* the sends so a silenced notice cannot cost us the group.

* **New member(s) joined** — fires on ``message.new_chat_members``. We
  greet the humans with a single welcome card carrying a DM deep-link
  button. Bots in the join batch are skipped; if the batch is *all* bots
  there is nothing to greet and we return without posting.

* **…and the joins that never produce that service message** — fires on
  ``chat_member`` (#245(d)). Telegram posts a ``new_chat_members``
  service message only when someone is *added* by another member. A user
  who follows an invite link, or whose join request an admin approves,
  arrives as a bare ``chat_member`` transition and nothing else — so
  until this handler existed the two most common ways of joining a
  public group were also the two that skipped both the membership record
  and the captcha. Both paths now converge on :func:`_onboard_joiners`.

  ``chat_member`` is one of the update types Telegram withholds unless
  the webhook asks for it by name. Nothing here has to ask: the
  subscription is derived from the registered handlers
  (``webhook/lifespan.py`` passes ``resolve_used_update_types()`` as
  ``allowed_updates``), so the registration below widens it on its own.
  It is also delivered only while the bot is an administrator — which is
  a precondition of the captcha anyway, since muting takes rights.

  A normal add fires *both* updates, in no guaranteed order, so
  :data:`_RECENT_ONBOARDS` lets whichever arrives first claim the joiner
  and the loser return without doing anything twice.

* **Someone left** — and a departure reaches us the same two ways a
  join does, for the same reasons (#605). ``left_chat_member`` is the
  service message, and a chat that suppresses the join notice
  suppresses this one too; a "delete and leave" from the client
  produces nothing *but* the ``chat_member`` transition. So the
  departure side is registered on both transports as well and
  deduplicated through :data:`_RECENT_OFFBOARDS` exactly like the join
  side. Both converge on :func:`_offboard_leaver`, which stamps the
  join row as left, says goodbye, honours the marriage's
  ``auto_divorce`` mode and resets the global rank.

  A ban is a departure, and the captcha kick (:func:`_expire_captcha`)
  is a ban — so a joiner who ignores the captcha gets the farewell and
  the rank reset. That is exactly what an admin kick already produced
  through the service-message transport; widening to ``chat_member``
  extends the same treatment to the chats where Telegram never posted
  the service message, it does not invent a new one.

Language is resolved from the relevant user's Telegram ``language_code``
only — these handlers run before any ``UserService`` touch and must not
depend on a ``users`` row existing (mirrors ``handlers/cancel.py``).

Multi-join UX: Telegram delivers one update per join event but that
event can carry several ``new_chat_members``. We greet them
*collectively* in one message (names joined with ", ") rather than
spamming the chat with one card per arrival — quieter for the common
"someone added 3 friends" case while still naming everyone.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from telegram_invite_bot.core.deep_links import dm_start_url
from telegram_invite_bot.core.moderation_reasons import CAPTCHA_FAILED
from telegram_invite_bot.core.ranks import RankLevel
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.moderation import _require_admin
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.captcha import CaptchaConfirm
from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
from telegram_invite_bot.repositories.bot_groups_repo import BotGroupsRepo
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
from telegram_invite_bot.repositories.user_group_joins_repo import UserGroupJoinsRepo
from telegram_invite_bot.repositories.welcome_config_repo import WelcomeConfigRepo
from telegram_invite_bot.services.rank_service import RankService
from telegram_invite_bot.utils.aiogram import command_body
from telegram_invite_bot.utils.chat_permissions import MUTED_PERMS, UNRESTRICTED_PERMS
from telegram_invite_bot.utils.language import lang_from_code, resolve_lang
from telegram_invite_bot.utils.telegram_kick import KickOutcome, kick_member

log = logger.bind(component="handlers.group_events")

if TYPE_CHECKING:
    from aiogram.types import (
        CallbackQuery,
        ChatMemberUnion,
        ChatMemberUpdated,
        Message,
    )
    from aiogram.types import User as TgUser

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


_FALLBACK_USERNAME = "this_bot"

_ADMIN_STATUSES = frozenset({ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR})

# The two statuses that mean "not in the chat". Everything else is some
# flavour of membership — see :func:`_is_member` for the ``RESTRICTED``
# subtlety.
_OUT_STATUSES = frozenset({ChatMemberStatus.LEFT, ChatMemberStatus.KICKED})

# Hard cap on a stored welcome template — comfortably beyond a real
# multi-line greeting yet far below Telegram's 4096 body cap, so an
# accidental paste of a chat log doesn't land verbatim in the table.
_WELCOME_TEMPLATE_MAX_LENGTH = 1000

#: ``(chat_id, user_id)`` -> ``time.monotonic()`` of the onboarding that
#: claimed it (#245(d)).
#:
#: A plain "someone added a friend" join reaches us twice — once as the
#: ``new_chat_members`` service message, once as a ``chat_member``
#: transition — and Telegram promises nothing about which lands first.
#: The record write is idempotent (the repo keeps the first sighting),
#: but the captcha is not: arming twice posts two notices in the group
#: for one arrival. So the first handler to see a joiner claims it here
#: and the second one drops it.
#:
#: Process-local on purpose. This is deduplication of two updates about
#: one event, both of which arrive at the same worker within a second of
#: each other; it is not a distributed lock and nothing about
#: correctness rests on it surviving a restart. Losing the map costs at
#: worst one duplicate notice for a join that was in flight across it.
_RECENT_ONBOARDS: dict[tuple[int, int], float] = {}

#: The departure half of the same trick (#605), keyed and swept
#: identically. Kept as a *separate* map rather than one shared
#: namespace on purpose: a leave must not be able to consume a join's
#: claim (or the reverse), and a genuine leave-and-rejoin inside the
#: window has to be able to claim both ends.
_RECENT_OFFBOARDS: dict[tuple[int, int], float] = {}

#: How long a claim suppresses the twin update. Generous next to the
#: sub-second gap the two really arrive in, and short enough that a
#: leave-and-rejoin a minute apart still onboards normally.
#:
#: A rejoin *inside* the window does lose its captcha, not just its
#: welcome card — the joiner is claimed, so :func:`_onboard_joiners`
#: never runs and never arms it. That matters where the bot itself
#: caused the exit: :func:`_expire_captcha` kicks with a ban+unban,
#: which drops the mute along with the membership, and
#: ``captcha_timeout_sec`` goes as low as 10s (``handlers/modcfg.py``).
#: Tracked as its own bug rather than papered over with a longer
#: window, which would only move the boundary.
_DEDUP_SEC: Final = 60.0


#: The only two tokens a welcome template may carry (#1958).
#:
#: Matched literally, with no format mini-language behind them: a stored
#: template is admin-typed DATA, and ``str.format_map`` read it as CODE.
#: ``{user:99999999}`` is a *width* — the renderer padded the joiner's
#: name out to a hundred million characters, ``html.escape`` left the
#: digits untouched, and the 1000-char cap still bought some sixty-six
#: repeats per template. ``{user.__class__}`` is an attribute access
#: whose result was substituted RAW, i.e. after the escaping pass that
#: L-57 exists for. Neither the resulting ``MemoryError`` nor the
#: ``AttributeError`` from ``{user.foo}`` was in the except clause the
#: renderer carried, and the template is *stored*, so a single
#: ``/setwelcome`` armed every subsequent join.
_WELCOME_TOKENS: Final = re.compile(r"\{(user|chat)\}")


def render_welcome_template(template: str, *, user: str, chat: str) -> str:
    """Render a per-group welcome template into an HTML-safe string.

    SECURITY (L-57): the stored template is treated as **plain text** —
    an admin must not be able to smuggle live HTML (``<a href=...>``,
    ``<b>``) into the card, and the substituted ``{user}`` / ``{chat}``
    values (a joiner's display name, a chat title) are fully
    attacker-controlled. So we:

    1. HTML-escape the entire template first (any ``<`` an admin typed
       renders as literal ``&lt;``).
    2. HTML-escape the substitution values.
    3. Substitute the now-escaped placeholder tokens (``{user}`` /
       ``{chat}`` survive ``html.escape`` unchanged — they have no
       HTML-special chars) in a **single left-to-right pass** so a value
       that itself contains a placeholder token (a joiner literally named
       ``{chat}``) cannot trigger a second substitution and leak the chat
       title. :func:`re.sub` with a replacement *function* does exactly
       that: it never rescans what it just substituted.

    Because both the template and the values are escaped before
    substitution, the result contains no live markup the bot didn't
    intend, regardless of what the admin or the joiner supplied.

    #1958: everything that is not one of the two bare tokens — a typo'd
    ``{foo}``, a stray brace, a format spec, a conversion, an attribute
    access — now survives as the literal text the admin typed. That is
    both the safe answer and the honest one: ``h_welcome_set_usage``
    advertises exactly two placeholders and never promised the format
    mini-language behind them.
    """
    values = {"user": html.escape(user), "chat": html.escape(chat)}
    return _WELCOME_TOKENS.sub(lambda m: values[m[1]], html.escape(template))


async def _bot_username(bot: Bot) -> str:
    """Resolve the bot's @username for the deep-link button.

    ``get_me`` is cached by aiogram per-bot, so this is not a per-call
    network round-trip. Falls back to a placeholder if Telegram ever
    returns a bot with no username (shouldn't happen).
    """
    me = await bot.get_me()
    return (me.username or "").strip() or _FALLBACK_USERNAME


def _dm_button(label: str, username: str, chat_id: int) -> InlineKeyboardBuilder:
    """The "open in private" button, carrying its own chat (#1926).

    ``chat_id`` is not optional here on purpose: both call sites are
    posting *into* a group, so there is always a chat to name, and a
    default would only make it easy to add a third one that forgets.
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=label, url=dm_start_url(username, group_chat_id=chat_id)))
    return builder


async def handle_bot_added(event: ChatMemberUpdated, bot: Bot) -> None:
    """The bot itself was added to a group (``my_chat_member`` JOIN).

    Posts a group notice with a private-chat deep-link button and
    best-effort DMs the user who added us. Both sends are fail-soft —
    a missing post permission or a blocked adder must not raise.
    """
    me = await bot.me()
    # ``my_chat_member`` is about the bot, but guard anyway so a future
    # re-registration on ``chat_member`` can't misfire on a human join.
    if event.new_chat_member.user.id != me.id:
        return

    adder: TgUser = event.from_user
    # ``my_chat_member`` updates never pass through the message/callback
    # LanguageMiddleware, so there is no data["lang"] here — derive from
    # the adder's Telegram locale (their stored pref may not even exist
    # yet; this is their first contact with the bot).
    lang = lang_from_code(adder.language_code)
    username = await _bot_username(bot)

    # Group notice — greeting + admin-rights / moderation nudge + DM button.
    try:
        await bot.send_message(
            event.chat.id,
            t("h_bot_added_group_notice", lang),
            reply_markup=_dm_button(
                t("h_bot_added_dm_open_btn", lang), username, event.chat.id
            ).as_markup(),
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.warning("bot_added group notice failed in {cid}: {e!r}", cid=event.chat.id, e=exc)

    # Best-effort DM to the adder — they may have blocked the bot or
    # never started it; suppress and move on.
    try:
        await bot.send_message(
            adder.id,
            t("h_bot_added_dm_notice", lang, title=html.escape(event.chat.title or "")),
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.warning("bot_added DM to adder {uid} failed: {e!r}", uid=adder.id, e=exc)

    log.bind(chat_id=event.chat.id, adder=adder.id, lang=lang).info("bot added to group")


def _is_member(member: ChatMemberUnion) -> bool:
    """Does this chat-member record mean "currently in the chat"?

    ``RESTRICTED`` is the one status that answers both ways: a muted
    member is still a member, and Telegram carries the difference in
    ``is_member`` rather than in the status. Everything that is not an
    explicit exit counts as present, so a status this code has never
    heard of errs towards "the bot is here" — the direction that keeps a
    group visible rather than silently retiring it.
    """
    if member.status in _OUT_STATUSES:
        return False
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", True))
    return True


async def handle_bot_membership(
    event: ChatMemberUpdated, bot: Bot, registry: EngineRegistry
) -> None:
    """The bot's own membership in a group changed.

    One handler for every ``my_chat_member`` transition rather than a
    join-filtered and a leave-filtered pair: aiogram stops at the first
    matching handler, so two registrations on the same event would make
    the outcome depend on registration order, and a transition matching
    neither filter (``member`` → ``administrator``, the moment the bot
    is finally given rights) would fall through both. Branching here
    keeps all of it in one readable place.

    Fail-soft throughout — a ``my_chat_member`` update has nobody to
    apologise to, and an exception escaping here would only fill the log
    with a traceback aiogram already cannot act on.
    """
    me = await bot.me()
    # ``my_chat_member`` is about the bot by definition, but guard anyway
    # so a future re-registration on ``chat_member`` can't misfire on a
    # human's join and register the group under the wrong owner.
    if event.new_chat_member.user.id != me.id:
        return

    was_in = _is_member(event.old_chat_member)
    now_in = _is_member(event.new_chat_member)
    bound = log.bind(chat_id=event.chat.id, status=event.new_chat_member.status)

    if not now_in:
        # Removed. The row survives with ``is_active = 0`` rather than
        # being deleted: it carries the payout attribution, and a row
        # that can be deleted can be recreated naming whoever re-added
        # the bot — see the ``0009_bot_groups_is_active`` migration.
        try:
            async with session_for(registry, DBName.USERS) as session:
                changed = await BotGroupsRepo(session).deactivate(event.chat.id)
        except Exception as exc:  # noqa: BLE001 — must never raise
            bound.opt(exception=exc).error("failed to deactivate group on bot removal")
            return
        if changed:
            bound.info("bot removed from group; group deactivated")
        return

    # Present: a join, a re-add, or a promotion. ``register`` is an
    # upsert, so all three land as "the bot is in this chat, with these
    # rights, under the owner it already had".
    try:
        async with session_for(registry, DBName.USERS) as session:
            await BotGroupsRepo(session).register(
                event.chat.id,
                added_by_user_id=event.from_user.id,
                chat_title=event.chat.title,
                has_admin_rights=event.new_chat_member.status in _ADMIN_STATUSES,
            )
    except Exception as exc:  # noqa: BLE001 — must never raise
        bound.opt(exception=exc).error("failed to register group on bot join")

    # Only an actual arrival is worth announcing. A promotion is not a
    # join, and re-greeting the chat every time an admin toggles the
    # bot's rights would be noise.
    if not was_in:
        await handle_bot_added(event, bot)


async def _custom_template_for(registry: EngineRegistry, group_id: int) -> str | None:
    """Return the live custom welcome template for ``group_id``, or ``None``.

    ``None`` means "use the default i18n card" — either no row exists, the
    row is disabled (``/welcome_off``), or its template is empty. The read
    is best-effort: a DB error must never block the welcome path, so we
    swallow it and fall back to the default card.
    """
    try:
        async with session_for(registry, DBName.MODERATION) as session:
            row = await WelcomeConfigRepo(session).get(group_id)
    except Exception as exc:  # noqa: BLE001 — welcome must never raise
        log.warning("welcome_config read failed for {gid}: {e!r}", gid=group_id, e=exc)
        return None
    if row is None or not row.enabled:
        return None
    template = (row.template or "").strip()
    return template or None


# ---------------------------------------------------------------------------
# Join captcha (L-55) — restrict-until-button verification for new members
# ---------------------------------------------------------------------------

# Pending captcha timers, keyed by (chat_id, user_id). Module-level and
# in-memory: a process restart drops pending timers, leaving any
# unconfirmed joiner restricted-but-unkicked until an admin notices.
# There is no legacy precedent either way — ``bot.py`` has no captcha at
# all (grepping it for "captcha" returns nothing), so L-55 is a new
# feature and this restart gap is an accepted shortcut, not parity.
# The button keeps working after a restart because the confirm handler
# lifts the restriction even when no timer entry exists. That used to
# mean it lifted restrictions this flow never imposed; #342 narrowed it
# with :func:`_restriction_is_captchas`, which reads the live member
# instead of this dict, so a restart still cannot strand a joiner. The
# persistent table of #242/#243 would let the entry itself survive and
# make the fallback unnecessary.
_PENDING_CAPTCHA: dict[tuple[int, int], asyncio.Task[None]] = {}

#: Episodes whose deadline has fired and whose kick is still in flight.
#:
#: Popping :data:`_PENDING_CAPTCHA` is how both sides claim an episode,
#: so the dict cannot tell "the timeout owns this one" apart from "a
#: restart dropped the timer" — and those two want opposite answers from
#: a press (say nothing / lift anyway). :func:`_expire_captcha` holds a
#: key here for exactly as long as it is acting on it (#2017).
_EXPIRING_CAPTCHA: set[tuple[int, int]] = set()

# The captcha restriction and its lift are the same two sets /mute and
# /unmute use, imported rather than respelled: three hand-written copies
# is how the pre-Bot-API-6.5 media flag survived here long after aiogram
# stopped modelling it (see utils/chat_permissions.py).
_CAPTCHA_RESTRICT_PERMS = MUTED_PERMS
_CAPTCHA_DEFAULT_PERMS = UNRESTRICTED_PERMS

#: How long past the kick deadline the captcha mute stays on by itself.
#:
#: The mute used to carry no expiry, and Telegram spells "no expiry" as
#: ``until_date = 0`` — which is also how it reports a moderator's
#: "Restrict user → Forever". That collision is what made
#: :func:`_restriction_is_captchas` unable to tell the two apart (#2027).
#: Giving our own mute a real deadline breaks the tie from the other
#: side: whatever is permanent is now provably not ours.
#:
#: The margin only has to outlast the kick, which fires at
#: ``timeout_sec``; Telegram treats anything under 30 seconds from now
#: as "forever", so the floor below matters for the smallest configurable
#: window (10 s, modcfg.py:179).
_CAPTCHA_MUTE_MARGIN_SEC = 60
_CAPTCHA_MUTE_FLOOR_SEC = 60

#: Anything at or before this is Telegram's "forever", not a date.
#:
#: ``until_date = 0`` reaches aiogram as the Unix epoch. No real
#: restriction expires in 1970, so one day of slack is plenty of room to
#: read the sentinel as what it is.
_FOREVER_BEFORE = datetime(1970, 1, 2, tzinfo=UTC)

#: The deadline this process set on each captcha mute, keyed like
#: :data:`_PENDING_CAPTCHA`.
#:
#: This is the proof of ownership. ``until_date`` alone cannot carry it:
#: a moderator's mute is a restriction like ours, and asking only
#: "permanent?" or only "in the future?" answers a question about the
#: *shape* of a restriction when the question is *whose* it is. An exact
#: deadline we can show we chose answers the right one — a moderator
#: landing a mute on a joiner mid-window overwrites ours and changes the
#: expiry, so #671's case stays refused, and a permanent restriction
#: matches nothing here at all (#2027).
#:
#: In memory, like the timers, and for the same reason: a restart is
#: covered by the mute's own expiry rather than by a fallback that lifts.
_CAPTCHA_MUTE_UNTIL: dict[tuple[int, int], datetime] = {}

#: Slack when comparing the two deadlines. Telegram stores ``until_date``
#: as whole Unix seconds, so the round trip is exact but for truncation.
_CAPTCHA_UNTIL_TOLERANCE = timedelta(seconds=5)


async def _captcha_config_for(registry: EngineRegistry, group_id: int) -> tuple[bool, int]:
    """Return ``(captcha_enabled, captcha_timeout_sec)`` for the group.

    Best-effort: a DB error must never block the join path, so it
    degrades to "captcha off" (the pre-feature behaviour).
    """
    try:
        async with session_for(registry, DBName.MODERATION) as session:
            cfg = await GroupModConfigRepo(session).get_or_default(group_id)
    except Exception as exc:  # noqa: BLE001 — join path must never raise
        log.warning("captcha config read failed for {gid}: {e!r}", gid=group_id, e=exc)
        return (False, 120)
    return (cfg.captcha_enabled, cfg.captcha_timeout_sec)


async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Best-effort "is this joiner already an admin?" probe.

    Failure degrades to ``False`` (treat as regular member) — restricting
    an actual admin is a harmless no-op for Telegram anyway, while
    skipping captcha on a probe error would be a bypass vector.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — probe is advisory only
        log.warning(
            "captcha admin probe failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=user_id,
            e=exc,
        )
        return False
    return member.status in _ADMIN_STATUSES


async def _record_captcha_kick(
    registry: EngineRegistry, bot_id: int, chat_id: int, user_id: int, *, details: str
) -> None:
    """Write the captcha kick to ``moderation_log``. Best-effort.

    Bot-initiated moderation is logged with ``admin_id`` set to the bot's
    own id — the convention ``handlers/wordfilter.py`` established for
    automod bans. Until #269 this path wrote nothing at all, so the only
    trace a kicked joiner left was a log line, and a "why was I
    removed?" complaint could not be checked against anything.

    #253: the action is the plain ``"kick"``, not a ``"captcha_kick"``
    of its own. ``handlers/groupadmin.py`` renders the audit log through
    a fixed label map and counts a fixed action list, so an unknown slug
    would have shown a Russian-speaking admin the raw English word
    ``captcha_kick`` and left "Кики: 0" next to it. What made this one
    automatic lives in ``details``.

    #1346: ``reason`` is a slug for the same reason the label map exists.
    It used to be the Russian literal "Капча: не пройдена", which both
    renderers print verbatim — so an English-speaking operator read one
    Russian line on an otherwise English card. The wording now lives in
    the locale files and is picked by the reader's language.
    """
    try:
        async with session_for(registry, DBName.MODERATION) as session:
            await ModerationRepo(session).record_action(
                action="kick",
                user_id=user_id,
                admin_id=bot_id,
                chat_id=chat_id,
                reason=CAPTCHA_FAILED,
                details=f"captcha_timeout:{details}",
            )
    except Exception as exc:  # noqa: BLE001 — the audit must not break the kick
        log.warning(
            "captcha kick audit failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=user_id,
            e=exc,
        )


async def _expire_captcha(
    bot: Bot, registry: EngineRegistry, chat_id: int, user_id: int, notice_message_id: int
) -> None:
    """Timeout body: kick the unconfirmed joiner, remove the notice.

    Pops the pending entry first — if the entry is already gone the user
    confirmed in the race window and we do nothing. Every Telegram call
    is best-effort; this runs inside a fire-and-forget task and must
    never raise.

    The kick goes through :func:`kick_member`, which distinguishes the
    two halves of the legacy ban+unban pair (#269). Both of its failure
    modes matter here:

    * ban landed, unban did not — the joiner is out but banned until the
      expiry runs down. Logged loudly by the helper; still a kick.
    * the ban never landed — and then the mute :func:`_restrict_for_captcha`
      took is still on, with no ``until_date`` and no timer left to
      retry it, because the pending entry was popped above. That is
      #285: a joiner silenced forever by a kick that never happened. Give
      the permissions back. Someone who ignored a captcha and stayed is a
      smaller problem than someone who can never speak again.

    A kick that *did* land also releases the joiner's onboarding claim
    (#672) — see the comment on that line.
    """
    key = (chat_id, user_id)
    if _PENDING_CAPTCHA.pop(key, None) is None:
        return
    # #2032: the recorded deadline used to be dropped right here, one
    # line into the act. It is the only evidence that the mute in front
    # of us is ours (:func:`_restriction_is_captchas`), and the branch
    # below that lifts a mute needs exactly that evidence — so dropping
    # it up front made the question unanswerable and the answer always
    # "not ours". Each ending drops it for itself now.
    # #2017: the pop above is this function's claim, and from here to the
    # last line the joiner is being removed. A press landing in that
    # window finds no pending entry and, reading that as "a restart
    # dropped my timer", used to lift the restriction and answer "you
    # passed" to someone already gone. Say so out loud for the length of
    # the act instead of leaving the confirm handler to guess.
    _EXPIRING_CAPTCHA.add(key)
    try:
        await _run_captcha_expiry(bot, registry, chat_id, user_id, notice_message_id)
    finally:
        _EXPIRING_CAPTCHA.discard(key)


async def _run_captcha_expiry(
    bot: Bot, registry: EngineRegistry, chat_id: int, user_id: int, notice_message_id: int
) -> None:
    """Body of :func:`_expire_captcha`, run under its claim."""
    # #2031: ``ALREADY_BANNED`` lands in every non-``FAILED`` branch
    # below, and correctly. A joiner banned by a moderator while their
    # captcha was still pending is gone for good, which is a stronger
    # version of what the timeout wanted: the episode is over, the
    # onboard claim should be released, and the row records who ended
    # it. What must not happen is the kick's unban half running and
    # handing that moderator's ban back — which is what this timer did
    # before the probe, on a path nobody had to trigger deliberately.
    outcome = await kick_member(bot, chat_id, user_id)
    if outcome is not KickOutcome.FAILED:
        # #672: the kick is a ban+unban, so it takes the mute away with
        # the membership. If the joiner comes straight back while the
        # dedup claim is still live, ``_claim_joiners`` would drop them
        # and they would walk in with no mute, no captcha and no notice —
        # reachable whenever ``captcha_timeout_sec`` is set below
        # :data:`_DEDUP_SEC`. The episode is over; release the claim.
        # Safe against the twin-update race the claim exists for: the
        # shortest configurable timeout is 10s, and the twin updates
        # arrive within a second of each other.
        _RECENT_ONBOARDS.pop((chat_id, user_id), None)
    if outcome is KickOutcome.FAILED:
        await _lift_captcha_expiry_mute(bot, chat_id, user_id)
    else:
        # The membership is gone and the mute went with it; the deadline
        # we recorded for it is spent (#2032 moved this off the top of
        # :func:`_expire_captcha`).
        _CAPTCHA_MUTE_UNTIL.pop((chat_id, user_id), None)
        await _record_captcha_kick(registry, bot.id, chat_id, user_id, details=outcome.value)
    try:
        await bot.delete_message(chat_id, notice_message_id)
    except Exception as exc:  # noqa: BLE001 — notice may already be gone
        log.warning("captcha notice delete failed (chat={c}): {e!r}", c=chat_id, e=exc)
    if outcome is KickOutcome.FAILED:
        # Saying "kicked" here would be the same lie the audit row used
        # to leave unsaid: nothing was removed, and the group still has
        # a member who never confirmed.
        log.bind(chat_id=chat_id, user=user_id).warning(
            "captcha timeout — kick did not land, mute lifted"
        )
    elif outcome is KickOutcome.ALREADY_BANNED:
        log.bind(chat_id=chat_id, user=user_id).info(
            "captcha timeout — the joiner was already banned, so the kick was "
            "skipped rather than lifting that ban"
        )
    else:
        log.bind(chat_id=chat_id, user=user_id, outcome=outcome.value).info(
            "captcha timeout — user kicked"
        )


async def _captcha_timeout(
    bot: Bot,
    registry: EngineRegistry,
    chat_id: int,
    user_id: int,
    notice_message_id: int,
    timeout_sec: int,
) -> None:
    """Sleep out the captcha window, then expire (kick) if unconfirmed."""
    try:
        await asyncio.sleep(timeout_sec)
    except asyncio.CancelledError:
        return  # confirmed — the callback cancelled us
    await _expire_captcha(bot, registry, chat_id, user_id, notice_message_id)


async def _lift_captcha_restriction(bot: Bot, chat_id: int, user_id: int) -> None:
    """Restore default member permissions. Best-effort, never raises.

    Shared by every path that has to undo a captcha mute it took: the
    notice-send rollback in :func:`_arm_captcha` and the admin false
    positive in :func:`handle_new_members` (#245(e)).
    """
    _CAPTCHA_MUTE_UNTIL.pop((chat_id, user_id), None)
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=_CAPTCHA_DEFAULT_PERMS)
    except Exception as exc:  # noqa: BLE001 — best-effort lift
        log.warning(
            "captcha restrict rollback failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=user_id,
            e=exc,
        )


async def _lift_captcha_expiry_mute(bot: Bot, chat_id: int, user_id: int) -> None:
    """#285's lift, narrowed to a mute this episode actually placed (#2032).

    The ban failing is the one ending that leaves the joiner in the chat,
    still muted, with no timer left to retry — #285, a joiner silenced
    forever by a kick that never happened. The remedy was an
    unconditional :func:`_lift_captcha_restriction`, which is the same
    shape #2027 took out of the confirm handler: inferring ownership of a
    restriction from the fact that we are the ones looking at it. A
    moderator who restricts a joiner mid-window, in a chat where the bot
    has since lost the right to ban, got their sanction lifted by a timer.

    Reachability widened when the pair grew its membership probe: a lost
    probe is a ``FAILED`` too, so this path no longer requires the ban
    itself to be refused. Hence the gate now rather than later.

    ``_restriction_is_captchas`` fails open on an unreadable member when
    there is no live timer, and there is none here — we popped it. That
    is deliberate and unchanged from #285: what this adds is a refusal
    for the case we *can* read and can see is somebody else's.
    """
    if await _restriction_is_captchas(bot, chat_id, user_id, has_timer=False) is False:
        # Not ours to lift. Drop our own record of the episode anyway —
        # it describes a mute that no longer exists, since matching is
        # what just failed.
        _CAPTCHA_MUTE_UNTIL.pop((chat_id, user_id), None)
        log.bind(chat_id=chat_id, user=user_id).warning(
            "captcha timeout — kick did not land and the restriction is not ours, so it stays on"
        )
        return
    await _lift_captcha_restriction(bot, chat_id, user_id)


async def _restrict_for_captcha(bot: Bot, chat_id: int, user: TgUser, timeout_sec: int) -> bool:
    """Mute one joiner. Returns True iff the mute actually landed.

    Split out of :func:`_start_captcha` for #245(e): this is the only
    half that has to happen *fast*, and separating it lets the join
    handler take the mute first and do its bookkeeping afterwards.
    Fail-soft — no rights means no captcha, not a broken join path.

    The mute carries an expiry a little past the kick deadline (#2027).
    Two things follow, and both are the point. It is now distinguishable
    from a moderator's permanent restriction, which is what
    :func:`_restriction_is_captchas` needs in order to refuse clearing
    one. And a process restart no longer strands the joiner: the timer
    that would have freed them dies with the process, but Telegram
    expires the mute on its own a minute after the window they were
    given. The old answer to that case — lift on any press with no timer
    — was the self-service unmute itself.
    """
    until = datetime.now(UTC) + timedelta(
        seconds=max(timeout_sec + _CAPTCHA_MUTE_MARGIN_SEC, _CAPTCHA_MUTE_FLOOR_SEC)
    )
    try:
        await bot.restrict_chat_member(
            chat_id, user.id, permissions=_CAPTCHA_RESTRICT_PERMS, until_date=until
        )
    except Exception as exc:  # noqa: BLE001 — no rights → no captcha
        log.warning(
            "captcha restrict failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=user.id,
            e=exc,
        )
        return False
    _CAPTCHA_MUTE_UNTIL[(chat_id, user.id)] = until
    return True


async def _arm_captcha(
    bot: Bot, registry: EngineRegistry, chat_id: int, user: TgUser, timeout_sec: int
) -> bool:
    """Post the button notice for an already-muted joiner, arm the timer.

    Caller must have taken the mute (:func:`_restrict_for_captcha`)
    first. Returns True iff the captcha is armed; if the notice fails to
    send the restriction is lifted again, so the joiner is never left
    muted with no button to press.
    """
    lang = lang_from_code(user.language_code)
    name = html.escape(user.first_name or user.username or str(user.id))
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=t("h_cap_btn", lang),
            callback_data=CaptchaConfirm(user_id=user.id).pack(),
        )
    )
    try:
        notice = await bot.send_message(
            chat_id,
            t("h_cap_notice", lang, name=name, sec=timeout_sec),
            reply_markup=builder.as_markup(),
        )
    except Exception as exc:  # noqa: BLE001 — lift again, never strand the joiner
        log.warning("captcha notice send failed (chat={c}): {e!r}", c=chat_id, e=exc)
        await _lift_captcha_restriction(bot, chat_id, user.id)
        return False

    # A rejoin inside the timeout window re-arms the same
    # ``(chat_id, user_id)`` key. Overwriting without cancelling leaves the
    # previous timer running, and ``_expire_captcha``'s pop is keyed only —
    # not identity-checked — so the stale timer consumes the *new* entry and
    # kicks a joiner whose window has not run out yet. Cancel first:
    # ``_captcha_timeout`` returns on ``CancelledError`` without touching the
    # registry, so the fresh timer below is the only one left armed.
    previous = _PENDING_CAPTCHA.pop((chat_id, user.id), None)
    if previous is not None:
        previous.cancel()
        log.bind(chat_id=chat_id, user=user.id).info("captcha re-armed — old timer cancelled")

    _PENDING_CAPTCHA[(chat_id, user.id)] = asyncio.create_task(
        _captcha_timeout(bot, registry, chat_id, user.id, notice.message_id, timeout_sec)
    )
    log.bind(chat_id=chat_id, user=user.id, timeout=timeout_sec).info("captcha armed")
    return True


async def _start_captcha(
    bot: Bot, registry: EngineRegistry, chat_id: int, user: TgUser, timeout_sec: int
) -> bool:
    """Restrict one joiner, post the button notice, arm the timer.

    The two halves composed. Returns True iff the captcha was armed.
    :func:`handle_new_members` calls the halves separately so it can put
    its own work between them (#245(e)); this stays as the single-call
    form for anything that has no reason to.
    """
    if not await _restrict_for_captcha(bot, chat_id, user, timeout_sec):
        return False
    return await _arm_captcha(bot, registry, chat_id, user, timeout_sec)


async def _restriction_is_captchas(
    bot: Bot, chat_id: int, user_id: int, *, has_timer: bool
) -> bool | None:
    """Is this user's live restriction the captcha's, or someone's sanction?

    ``True`` lift it, ``False`` refuse it, ``None`` "could not read,
    and a live timer will arbitrate" — the ``bool | None`` contract
    ``utils/telegram_admin.py`` uses for the same kind of probe, so
    the caller can word its refusal honestly.

    Consulted on every press (#342, #671). The unconditional lift it
    replaces existed for a real reason — a process restart drops
    :data:`_PENDING_CAPTCHA` while the notice and its button keep
    rendering, and a joiner must not be stranded by that — so this
    answers the same question from live state rather than from the dict.
    Asking only when the dict entry was missing left the whole captcha
    window unguarded, which is the window a sanction is likeliest to
    land in.

    The discriminator used to be ``until_date`` alone, and it read
    "permanent means ours" — :func:`_restrict_for_captcha` passed no
    expiry, while every sanction this bot imposes is timed (``/mute``
    always computes one in ``moderation.handle_mute``, antiflood sends a
    ``flood_mute_minutes`` delta at handlers/antiflood.py:290/292-297).
    The premise was true about *this bot's* restrictions and the test was
    applied to *all* of them. A human moderator restricting someone
    through Telegram's own UI gets "Forever" by default, Telegram reports
    that as ``until_date = 0``, aiogram surfaces it as the Unix epoch —
    and so a member under a moderator's permanent restriction could press
    ``cap:<their own id>`` on any stale captcha notice, or craft the
    payload outright, and hand themselves back full permissions (#2027).
    It worked in groups where the captcha was never switched on.

    So ownership is no longer *inferred* from the restriction's shape; it
    is matched against the deadline this process chose. #2027 gives every
    captcha mute an explicit ``until_date`` and records it in
    :data:`_CAPTCHA_MUTE_UNTIL`, so the probe can ask the only question
    that actually means "ours": does the live expiry equal the one we
    set? A moderator's permanent restriction matches nothing here, and a
    moderator's *timed* mute landing on a joiner mid-window overwrites
    the row and changes its expiry — so #671's case is refused too,
    rather than being the residue it used to be.

    A live timer is deliberately NOT taken as proof. It says an episode
    is running, not that the restriction in front of us belongs to it,
    and conflating the two is exactly how a moderator's sanction became
    liftable by the user it was aimed at. ``has_timer`` survives only for
    the API-error branch below, where the question is a different one.

    The live probe is still worth its round trip for the two states where
    the lift is a no-op either way — not restricted at all, or restricted
    until a moment already past — because answering ``True`` there is
    what lets a user who was freed some other way still get the "passed"
    reply instead of a refusal.

    What this deliberately gives up is the post-restart press. The dict
    dies with the process, so a genuine joiner pressing the button after
    a deploy is now refused — and is not stranded by it, because since
    #2027 the mute carries its own expiry and Telegram lifts it a minute
    after their window. Refusing a real joiner for that minute is the
    smaller harm; the fallback that spared them is the hole itself.

    On an API error the answer depends on ``has_timer`` — whether the
    caller is holding a live timer for this episode (#1945). The caller
    passes it rather than this function reading :data:`_PENDING_CAPTCHA`
    itself, because since #2017 the caller takes the entry out of that
    dict *before* the probe: reading it here would see the caller's own
    claim as "no timer" and fail open on every press.

    The fail-open this replaces was written for ONE
    case — the post-restart press, where :data:`_PENDING_CAPTCHA` is
    empty and refusing would strand a legitimate joiner permanently,
    since the timer that would have freed them died with the old
    process. It applied to every state, including the one where a timer
    IS alive; there a transient Bot API hiccup handed back the very
    self-service unmute #342 and #671 exist to refuse, over a
    moderator's ``/mute`` or an antiflood automute.

    With a live timer nobody is stranded: :func:`handle_captcha_confirm`
    leaves a refused press's timer untouched, so the captcha keeps its
    normal life and the joiner can simply press again once the API
    answers. So refuse there, and fail open only where the docstring's
    own argument actually holds.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        # No timer means nothing else will ever free this user, which is
        # the one state the fail-open was reasoned for. A live timer
        # arbitrates on its own, so a failed probe must not decide.
        stranded = not has_timer
        log.warning(
            "captcha lift probe failed (chat={c}, user={u}, lifting={l}): {e!r}",
            c=chat_id,
            u=user_id,
            l=stranded,
            e=exc,
        )
        return True if stranded else None
    if getattr(member, "status", "") != ChatMemberStatus.RESTRICTED:
        # Not restricted at all — the lift would be a no-op either way.
        return True
    # Duck-typed like the rest of the member probes in this codebase
    # (utils/telegram_admin.py:80) — the annotation is what keeps the
    # comparison out of ``Any``.
    until: datetime | None = getattr(member, "until_date", None)
    if until is not None and _FOREVER_BEFORE < until <= datetime.now(UTC):
        # Already expired. Telegram can still report the member as
        # RESTRICTED for a beat afterwards, and the lift changes nothing
        # — so say yes and let the presser have the "passed" reply.
        #
        # The lower bound is not cosmetic: ``until_date = 0`` arrives as
        # the Unix epoch, which is also "in the past", and reading THAT
        # as an expired restriction is precisely the bug this function
        # was rewritten for. Permanent falls through to the timer below.
        return True
    # The deadline we set for this episode, if this process set one.
    # Equality is the whole test: an expiry we can show we chose is the
    # only evidence that separates our mute from a moderator's. Telegram
    # stores whole seconds, hence the tolerance rather than ``==``.
    expected = _CAPTCHA_MUTE_UNTIL.get((chat_id, user_id))
    if expected is not None and until is not None:
        return abs(until - expected) <= _CAPTCHA_UNTIL_TOLERANCE
    return False


async def handle_captcha_confirm(
    callback: CallbackQuery, callback_data: CaptchaConfirm, bot: Bot, lang: str
) -> None:
    """ "I'm not a bot" pressed — lift the restriction for the right user.

    Anyone other than the joiner the notice belongs to gets a
    callback-answer rejection. For the right user: cancel the timer,
    restore default member permissions, and edit the notice into a
    "passed" line (which also removes the button). The lift still runs
    when no timer entry exists — a process restart drops timers but must
    not strand a restricted joiner whose button still renders.

    Every press is checked against live state first:
    :func:`_restriction_is_captchas` has to agree the restriction really
    is the captcha's own and not a moderator's mute or an antiflood
    automute (#342, #671). A refusal leaves the pending timer untouched,
    so the captcha still expires and kicks on its own schedule — it is
    taken out of :data:`_PENDING_CAPTCHA` for the length of the probe and
    put straight back (#2017), so the press and the deadline cannot both
    believe they won.
    """
    presser = callback.from_user
    if presser.id != callback_data.user_id:
        try:
            await callback.answer(t("h_cap_wrong_user", lang), show_alert=True)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            log.warning("captcha reject answer failed: {e!r}", e=exc)
        return

    message = callback.message
    if message is None:
        # Inaccessible/absent message — can't locate the chat; just ack.
        with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
            await callback.answer()
        return
    chat_id = message.chat.id

    # #671: the probe runs on every press, not only when the in-memory
    # timer is gone. A moderator can sanction a joiner while the captcha
    # window is still open — that is in fact the likeliest moment — and
    # #342's guard was unreachable in exactly that window, so the button
    # restored ``UNRESTRICTED_PERMS`` over a live ``/mute``.
    #
    # #2017: the claim is taken *before* the probe. ``_expire_captcha``
    # claims the same entry with a single synchronous pop, so while this
    # handler popped only after its Telegram round trip, a press landing
    # within one round trip of the deadline lost nothing and won
    # everything: the timer kicked the joiner while this handler restored
    # their permissions, edited the notice into "passed", and answered
    # ``h_cap_confirm_ok`` — congratulating someone on their way out, with
    # a ``moderation_log`` kick row against a user who did press.
    #
    # A refusal still has to leave the captcha its normal life (expire ->
    # kick), which is what #671 promised and what #1945's refuse-on-a-bad-
    # probe leans on. So the claim is handed back below rather than held.
    pending_key = (chat_id, presser.id)
    task = _PENDING_CAPTCHA.pop(pending_key, None)
    if task is None and pending_key in _EXPIRING_CAPTCHA:
        # The deadline got here first and the kick is in flight. Both
        # things this bot could say are false — "you passed" most of all,
        # and "a moderator restricted you" no less — so say nothing. The
        # notice the timeout is about to delete is the answer the joiner
        # actually gets.
        with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
            await callback.answer()
        log.bind(chat_id=chat_id, user=presser.id).info(
            "captcha press ignored — the timeout already claimed this episode"
        )
        return

    live_timer = task is not None and not task.done()
    verdict = await _restriction_is_captchas(bot, chat_id, presser.id, has_timer=live_timer)
    if verdict is not True:
        if task is not None and not task.done():
            # Hand the claim back. ``setdefault`` rather than assignment
            # because a re-arm can slip in during the probe (the joiner
            # left and came back); its timer owns the episode now and
            # ours is a corpse, which must not be left in the dict.
            holder = _PENDING_CAPTCHA.setdefault(pending_key, task)
            if holder is not task:
                task.cancel()
        # #1945: an unreadable probe is not the same answer as "a
        # moderator restricted you". Telling the joiner the latter would
        # stop them retrying, which is the whole reason refusing on a
        # failed probe is safe in the first place.
        unreadable = verdict is None
        key = "h_cap_probe_failed" if unreadable else "h_cap_not_captcha"
        with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
            await callback.answer(t(key, lang), show_alert=True)
        log.bind(chat_id=chat_id, user=presser.id).info(
            "captcha lift refused: {why}",
            why="the restriction could not be read"
            if unreadable
            else "the live restriction is not the captcha's",
        )
        return

    if task is not None:
        task.cancel()

    # The episode is over, so the deadline recorded for it is spent.
    # Dropped only here, never before the probe: the probe is the only
    # reader, and taking it away first would make every press look like
    # a stranger's restriction.
    _CAPTCHA_MUTE_UNTIL.pop(pending_key, None)
    try:
        await bot.restrict_chat_member(chat_id, presser.id, permissions=_CAPTCHA_DEFAULT_PERMS)
    except Exception as exc:  # noqa: BLE001 — best-effort lift
        log.warning(
            "captcha lift failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=presser.id,
            e=exc,
        )

    name = html.escape(presser.first_name or presser.username or str(presser.id))
    try:
        await bot.edit_message_text(
            t("h_cap_passed", lang, name=name),
            chat_id=chat_id,
            message_id=message.message_id,
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.warning("captcha notice edit failed (chat={c}): {e!r}", c=chat_id, e=exc)

    with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
        await callback.answer(t("h_cap_confirm_ok", lang))
    log.bind(chat_id=chat_id, user=presser.id).info("captcha confirmed")


async def _record_joins(
    registry: EngineRegistry,
    chat_id: int,
    joiners: list[TgUser],
    *,
    title: str | None,
) -> None:
    """Persist "these users are in this chat, since now" (RR-1 #3).

    Backs the group profile card's "in this group since" date and its
    "messages since joining" counter — neither of which any other table
    can answer, because message counts only start when someone speaks
    and a lurker may join months before that.

    Best-effort by design: a greeting must not be withheld because a
    bookkeeping write failed, and the repo keeps the *first* sighting on
    conflict, so a later observation of the same membership is harmless.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with session_for(registry, DBName.USERS) as session:
            repo = UserGroupJoinsRepo(session)
            for joiner in joiners:
                await repo.record_join(
                    joiner.id,
                    chat_id,
                    joined_at=now,
                    source="join_event",
                    group_title=title,
                )
    except Exception:  # noqa: BLE001 — never let bookkeeping eat the welcome
        log.opt(exception=True).warning("join bookkeeping failed (chat={c})", c=chat_id)


def _claim_fresh(seen: dict[tuple[int, int], float], chat_id: int, user_ids: list[int]) -> set[int]:
    """Claim the users no twin update has claimed yet (#245(d), #605).

    Returns the subset this caller owns, and marks them claimed. Expiry
    is swept here rather than on a timer: a map is only ever read on the
    path that writes it, so that path is the only place growth can
    happen and the only place it needs pruning.
    """
    now = time.monotonic()
    for key, stamp in list(seen.items()):
        if now - stamp > _DEDUP_SEC:
            del seen[key]

    fresh: set[int] = set()
    for user_id in user_ids:
        key = (chat_id, user_id)
        if key in seen:
            continue
        seen[key] = now
        fresh.add(user_id)
    return fresh


def _claim_joiners(chat_id: int, joiners: list[TgUser]) -> list[TgUser]:
    """The join-side face of :func:`_claim_fresh` (#245(d))."""
    fresh = _claim_fresh(_RECENT_ONBOARDS, chat_id, [j.id for j in joiners])
    return [j for j in joiners if j.id in fresh]


def _claim_leaver(chat_id: int, user_id: int) -> bool:
    """The leave-side face of :func:`_claim_fresh` (#605)."""
    return bool(_claim_fresh(_RECENT_OFFBOARDS, chat_id, [user_id]))


async def _onboard_joiners(
    bot: Bot,
    registry: EngineRegistry,
    *,
    chat_id: int,
    chat_title: str | None,
    humans: list[TgUser],
) -> None:
    """Everything that happens when humans join, whichever update said so.

    Shared by :func:`handle_new_members` (the ``new_chat_members``
    service message) and :func:`handle_member_joined` (the ``chat_member``
    transition an invite-link or approved-request join produces instead)
    — #245(d). ``humans`` must already be bot-free.

    L-57: if the group has a custom, enabled welcome template stored, it
    is rendered (with ``{user}`` / ``{chat}`` placeholders, HTML-escaped)
    instead of the default i18n card. The DM deep-link button is kept in
    both paths so the onboarding affordance survives a custom template.

    RR-1 #3: the join is also *recorded*. Everything after that can bail
    early — an armed captcha, a send failure — and the membership fact is
    true regardless of whether we managed to say hello.

    L-55 join captcha: when enabled for this group, each non-admin human
    joiner is muted and gets a per-user button notice INSTEAD of the
    welcome card (the "passed" edit doubles as the greeting; a welcome
    card under a muted user would be noise). Admins are excused via a
    best-effort probe. If no captcha was actually armed (all admins, or
    the bot lacks restrict rights), the normal welcome follows.
    """
    # #245(e): the config read and the mutes come FIRST, ahead of both
    # the join-recording write and the admin probe. Everything standing
    # between the join and the mute is a window the joiner can post a
    # payload in, and neither of those two had to be in it.
    #
    # The probe is the expensive half: a live ``get_chat_member`` per
    # joiner, a full network round trip, spent to answer a question the
    # mute does not depend on. Telegram already refuses to restrict an
    # admin, so the probe only decides whether to *show* a notice — and
    # that decision keeps just as well after the mute as before it. A
    # false positive now costs one lift; asking first cost the group the
    # whole window, every single join.
    #
    # Restricts stay sequential rather than gathered: ``new_chat_members``
    # is one user in practice, and a gather would reorder the warnings a
    # rights failure logs for no measurable gain on a batch of one.
    captcha_enabled, captcha_timeout = await _captcha_config_for(registry, chat_id)
    restricted: list[TgUser] = []
    if captcha_enabled:
        for joiner in humans:
            if await _restrict_for_captcha(bot, chat_id, joiner, captcha_timeout):
                restricted.append(joiner)

    await _record_joins(registry, chat_id, humans, title=chat_title)

    armed = False
    for joiner in restricted:
        if await _is_chat_admin(bot, chat_id, joiner.id):
            # Excused after the fact. On an admin the mute above was
            # already a no-op for Telegram, so this lift is one too —
            # it exists for the case where it wasn't.
            await _lift_captcha_restriction(bot, chat_id, joiner.id)
            continue
        if await _arm_captcha(bot, registry, chat_id, joiner, captcha_timeout):
            armed = True
    if armed:
        return

    # Language from the first human joiner — the card is one collective
    # message so we pick a single locale; the first arrival is as good a
    # choice as any and keeps the UX deterministic.
    lang = lang_from_code(humans[0].language_code)
    # #1344: the stand-in for a nameless joiner follows ``lang`` too.
    # A hardcoded "друг" here reached the English card and rendered as
    # "Hi, друг! Welcome to the chat".
    fallback_name = t("h_default_name", lang)
    names = ", ".join(html.escape(u.first_name or u.username or fallback_name) for u in humans)
    username = await _bot_username(bot)

    custom = await _custom_template_for(registry, chat_id)
    if custom is not None:
        # SECURITY: render via the escaping helper — ``names`` is already
        # html-escaped above, but the helper re-escapes the *template* and
        # the values, so passing raw names here is also safe. We pass the
        # raw joiner display string (unescaped) so the helper owns all
        # escaping in one place.
        raw_names = ", ".join(u.first_name or u.username or fallback_name for u in humans)
        text = render_welcome_template(custom, user=raw_names, chat=chat_title or "")
    else:
        text = t("h_group_welcome_member", lang, name=names)

    try:
        # ``bot.send_message`` rather than ``message.answer``: the
        # ``chat_member`` path has no message to answer, and the two are
        # the same call anyway — ``answer`` is sugar for this with the
        # chat id filled in.
        await bot.send_message(
            chat_id,
            text,
            reply_markup=_dm_button(
                t("h_group_welcome_dm_btn", lang), username, chat_id
            ).as_markup(),
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.warning("group welcome failed in {cid}: {e!r}", cid=chat_id, e=exc)
        return

    log.bind(chat_id=chat_id, count=len(humans), lang=lang, custom=custom is not None).info(
        "welcomed new member(s)"
    )


async def handle_new_members(message: Message, bot: Bot, registry: EngineRegistry) -> None:
    """One or more members joined (``message.new_chat_members``).

    The "someone was added by someone" arrival. Bots in the batch are
    skipped; an all-bot batch produces no message. Everything past the
    filtering is :func:`_onboard_joiners`, shared with the
    ``chat_member`` path (#245(d)).
    """
    humans = [u for u in (message.new_chat_members or []) if not u.is_bot]
    fresh = _claim_joiners(message.chat.id, humans) if humans else []
    if not fresh:
        return
    await _onboard_joiners(
        bot,
        registry,
        chat_id=message.chat.id,
        chat_title=message.chat.title,
        humans=fresh,
    )


async def handle_member_joined(
    event: ChatMemberUpdated, bot: Bot, registry: EngineRegistry
) -> None:
    """A human's membership went from "out" to "in" (#245(d)).

    The arrival Telegram does *not* announce with a service message: an
    invite-link follow, or an admin approving a join request. Both were
    invisible to this module until now, which meant the two easiest ways
    into a public group were also the two that skipped the captcha.

    The transition test is deliberately narrow — the previous status has
    to be an explicit exit (``left`` / ``kicked``), not merely "not a
    member". ``chat_member`` also fires on every *within*-membership
    change, and two of those are ones this file causes itself: the
    captcha mute (``member`` -> ``restricted``) and the lift back
    (``restricted`` -> ``member``). Treating "became a member" as a join
    would make the lift re-trigger onboarding, and the loop would be ours.

    Bots are skipped for the same reason they are in the service-message
    path; the bot's own membership is a ``my_chat_member`` update and
    never reaches here.
    """
    joiner = event.new_chat_member.user
    if joiner.is_bot:
        return
    if event.old_chat_member.status not in _OUT_STATUSES:
        return
    if not _is_member(event.new_chat_member):
        return

    fresh = _claim_joiners(event.chat.id, [joiner])
    if not fresh:
        return
    log.bind(chat_id=event.chat.id, user=joiner.id).info(
        "join seen via chat_member (no service message)"
    )
    await _onboard_joiners(
        bot,
        registry,
        chat_id=event.chat.id,
        chat_title=event.chat.title,
        humans=fresh,
    )


async def _record_leave(registry: EngineRegistry, chat_id: int, user_id: int) -> None:
    """Persist "this user is no longer in this chat" (#244).

    The mirror image of :func:`_record_joins`, and legacy's
    bot.py:44186-44193 verbatim in intent: flag the row, keep the row.
    ``joined_at`` is what the group profile card's "in this group since"
    line reads, so deleting on departure would silently reset that date
    to the *re*-join for anyone who ever left and came back.

    ``left_at`` is written naive-LOCAL because legacy writes the same
    column that way — ``bot.py:44190`` passes a bare ``datetime.now()``
    — and both implementations run against the same ``users.db``. This
    used to be naive-UTC, i.e. 10 800 s behind legacy's rows on the MSK
    production host, silently mixing two frames in one column. Nothing
    reads ``left_at`` today, which is the only reason that was
    harmless; #482's ``left_at < threshold`` sweep is exactly the
    comparison that would have ended bonds three hours early for every
    departure the port recorded.

    Best-effort like its counterpart: a farewell must not be withheld
    because a bookkeeping write failed. Legacy swallowed this write too
    (bot.py:44192-44193, ``logger.debug``).
    """
    now = datetime.now()  # noqa: DTZ005 — mirrors legacy naive-local (bot.py:44190)
    try:
        async with session_for(registry, DBName.USERS) as session:
            repo = UserGroupJoinsRepo(session)
            await repo.mark_left(user_id, chat_id, left_at=now)
    except Exception:  # noqa: BLE001 — never let bookkeeping eat the farewell
        log.opt(exception=True).warning("leave bookkeeping failed (chat={c})", c=chat_id)


async def _partner_has_left(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Is ``user_id`` gone from ``chat_id`` too? (#233, mode ``two``).

    Fail-closed on any probe error, exactly like legacy's bare
    ``except: pass`` around the same call (bot.py:44234-44239): a chat
    we cannot read says nothing, and dissolving a marriage on a network
    hiccup is the one outcome that cannot be undone by waiting.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — probe is advisory only
        log.warning(
            "auto-divorce partner probe failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=user_id,
            e=exc,
        )
        return False
    return member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)


async def _auto_divorce_on_leave(
    bot: Bot, registry: EngineRegistry, chat_id: int, user_id: int
) -> None:
    """Apply the marriage's ``auto_divorce`` setting to a departure (#233).

    ``/marry_auto_divorce`` has been storing the mode since T-019 and
    nothing has ever read it back — legacy's only consumer is this leave
    path (bot.py:44212-44241). Mode ``one`` dissolves the marriage the
    moment either spouse leaves; ``two`` waits until both are gone;
    anything else (including the ``off`` default and legacy's ``NULL``)
    leaves the marriage alone.

    Legacy scans every marriage in the chat and keeps the rows where the
    leaver is a spouse; the port asks for the leaver's own marriage
    instead, which is the same set — the schema allows a user only one
    active marriage per chat, and every other reader in the codebase
    already assumes that (``get_marriage`` is ``LIMIT 1``).

    The ``get_chat_member`` round-trip deliberately happens *between*
    the two sessions rather than inside one: holding the single users.db
    writer slot across a network call is what #220 and #234 were about.
    ``soft_divorce`` re-reads the marriage under its own session, so a
    divorce that lands in the gap makes this a no-op rather than a
    double write.
    """
    try:
        async with session_for(registry, DBName.USERS) as session:
            marriage = await BondsWriteRepo(session).get_marriage(chat_id, user_id)
            if marriage is None:
                return
            mode = (marriage.auto_divorce or "").strip().lower()
            partner = marriage.user2_id if marriage.user1_id == user_id else marriage.user1_id

        if mode not in ("one", "two"):
            return
        if mode == "two" and not await _partner_has_left(bot, chat_id, partner):
            return

        async with session_for(registry, DBName.USERS) as session:
            await BondsWriteRepo(session).soft_divorce(chat_id, user_id)
        log.bind(chat_id=chat_id, user=user_id).info("auto-divorce on leave ({m})", m=mode)
    except Exception:  # noqa: BLE001 — legacy swallowed this too (bot.py:44240-44241)
        log.opt(exception=True).warning("auto-divorce on leave failed (chat={c})", c=chat_id)


async def _reset_rank_on_leave(
    registry: EngineRegistry, settings: Settings, chat_id: int, user_id: int
) -> None:
    """Leaving the main chat drops the global rank back to 0 (#278-4).

    Legacy gates this on ``chat_id == CHAT_ID or chat_id in
    ALLOWED_GROUPS`` (bot.py:44252). ``ALLOWED_GROUPS`` is settings.json's
    ``allowed_groups`` (bot.py:3827-3830); its default is ``[]``
    (bot.py:2753) and legacy *empties* it whenever it holds nothing but
    ``CHAT_ID``. Production ships no settings.json at all, so the live
    gate has always reduced to ``chat_id == CHAT_ID`` — which is exactly
    what this port checks. The list has no counterpart anywhere in
    ``src/``, and inventing one here would be new behaviour rather than a
    port.

    Ranks are GLOBAL (``users.rank``, users_repo.py:211-227), so this
    demotion reaches every chat, not just the one that was left. That is
    legacy's semantics too: it calls the same global ``set_user_rank``.
    Restricting it to the main chat is what keeps a random group from
    being able to strip a moderator.

    Developers are skipped first, as legacy skips them (bot.py:44253).
    ``RankService.set_rank`` would refuse the change anyway, but it would
    refuse it with a warning about an attempted developer demotion — a
    log line that would fire on every owner who ever leaves.
    """
    main_chat_id = settings.bot.main_chat_id
    if not main_chat_id or chat_id != main_chat_id:
        return
    if settings.bot.is_developer(user_id):
        return
    try:
        ranks = RankService(registry, settings)
        # Legacy short-circuits on ``get_user_rank(...) > 0`` so an
        # ordinary member's departure costs no write at all
        # (bot.py:44253); the cached read makes that nearly free.
        if await ranks.get_rank(user_id) <= RankLevel.USER:
            return
        # ``by`` is pure audit attribution (users_repo.py:222 discards
        # it); the leaver is the honest actor — nobody else asked for
        # this. Legacy logged nothing at all here (admin_id defaults to
        # None at bot.py:44254, so log_moderation_action is skipped).
        await ranks.set_rank(user_id, RankLevel.USER, by=user_id)
    except Exception:  # noqa: BLE001 — a lost demotion must not eat the update
        log.opt(exception=True).warning(
            "rank reset on leave failed (chat={c}, user={u})", c=chat_id, u=user_id
        )


async def _offboard_leaver(
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    *,
    chat_id: int,
    leaver: TgUser,
) -> None:
    """Everything that happens when a human leaves, whichever update said so.

    Shared by :func:`handle_left_member` (the ``left_chat_member``
    service message) and :func:`handle_member_left` (the ``chat_member``
    transition a suppressed-service-message departure produces instead)
    — #605. ``leaver`` must already be bot-free and claimed.

    The farewell goes out with ``bot.send_message`` rather than
    ``message.answer`` for the same reason :func:`_onboard_joiners` does:
    the ``chat_member`` path has no message to answer, and the two are
    the same call anyway.

    Ports legacy's ``left_chat_member`` handler (bot.py:44157-44255) for
    the effects that outlived the strangler cutover: stamping
    ``user_group_joins``, saying goodbye, honouring the marriage's
    ``auto_divorce`` mode, and resetting the global rank.
    """
    await _record_leave(registry, chat_id, leaver.id)

    # Legacy hardcoded the Russian farewell (bot.py:44203-44207) and never
    # called its own translator; routing through ``t()`` here is a
    # deliberate improvement, not a divergence — it also un-orphans the
    # ``left_chat`` key, which legacy shipped and never used.
    lang = lang_from_code(leaver.language_code)
    # #1344: same nameless-user stand-in as the welcome card, and the
    # same reason to resolve it through ``t()`` — ``left_chat`` has an
    # English rendering that a literal "друг" would spoil.
    name = html.escape(leaver.first_name or leaver.username or t("h_default_name", lang))
    with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
        await bot.send_message(chat_id, t("left_chat", lang, name=name))

    # Legacy runs the auto-divorce block after the farewell
    # (bot.py:44212 follows bot.py:44207); keeping that order means a
    # slow ``get_chat_member`` probe never delays the goodbye.
    await _auto_divorce_on_leave(bot, registry, chat_id, leaver.id)

    # Legacy resets the rank last, after the auto-divorce block
    # (bot.py:44250 follows bot.py:44241).
    await _reset_rank_on_leave(registry, settings, chat_id, leaver.id)

    log.bind(chat_id=chat_id, user=leaver.id).info("member left")


async def handle_member_left(
    event: ChatMemberUpdated, bot: Bot, registry: EngineRegistry, settings: Settings
) -> None:
    """A human's membership went from "in" to "out" (#605).

    The departure Telegram does *not* always announce with a service
    message. A supergroup configured to hide join/leave notices hides
    both of them, and "delete and leave chat" from the client produces
    only this transition — so on the transports that matter most, the
    ``left_chat_member`` handler alone never fired and the join row was
    never stamped as left.

    The transition test is the symmetric ``was a member, is not one now``
    rather than the join half's narrower "came from an explicit exit
    status". The asymmetry is deliberate: ``_OUT_STATUSES`` membership
    would double-fire on ``restricted(is_member=False)`` -> ``kicked``,
    which is one departure delivered as two updates, while
    :func:`_is_member` already folds the ``restricted`` split correctly.
    The join half stays narrow for its own documented reason — so that
    the captcha lift cannot re-trigger onboarding.
    """
    leaver = event.new_chat_member.user
    if leaver.is_bot:
        return
    if not _is_member(event.old_chat_member):
        return
    if _is_member(event.new_chat_member):
        return
    if not _claim_leaver(event.chat.id, leaver.id):
        return

    log.bind(chat_id=event.chat.id, user=leaver.id).info(
        "leave seen via chat_member (no service message)"
    )
    await _offboard_leaver(bot, registry, settings, chat_id=event.chat.id, leaver=leaver)


async def handle_member_changed(
    event: ChatMemberUpdated, bot: Bot, registry: EngineRegistry, settings: Settings
) -> None:
    """Fan a group ``chat_member`` update out to the join/leave halves (#605).

    One branching handler rather than two registrations, for the reason
    :func:`handle_bot_membership` spells out: aiogram stops at the first
    handler whose *filters* match, and a handler that returns ``None``
    still counts as having handled the update. Two registrations sharing
    the ``_group`` filter would therefore make the outcome depend on
    registration order — the first would swallow every transition and
    the second would never run.

    The two branches are exactly disjoint. :func:`handle_member_joined`
    only acts when the previous status was an explicit exit, which
    implies the member was out; :func:`handle_member_left` only acts when
    they were in. A within-membership change (the captcha mute, an admin
    promotion) satisfies neither and falls through both.
    """
    if _is_member(event.old_chat_member):
        await handle_member_left(event, bot, registry, settings)
    else:
        await handle_member_joined(event, bot, registry)


async def handle_left_member(
    message: Message, bot: Bot, registry: EngineRegistry, settings: Settings
) -> None:
    """Someone left the chat (``message.left_chat_member``) — #244, #233, #278-4.

    The announced half of a departure. Everything past the filtering is
    :func:`_offboard_leaver`, shared with the ``chat_member`` path
    (#605).

    Every mutation keys off ``left_chat_member``, never ``from_user``:
    on a kick the latter is the *admin* who did the kicking, and using it
    would mark the wrong person gone (bot.py:44186 keys the same way).

    Bots are skipped exactly as legacy skips them (bot.py:44183-44184) —
    they never had a join row to flag and nobody misses them. The bot's
    *own* departure arrives as ``my_chat_member`` and is handled by
    :func:`handle_bot_membership`, so the self-branch at
    bot.py:44167-44178 needs no second home here.
    """
    left = message.left_chat_member
    if left is None or left.is_bot:
        return
    if not _claim_leaver(message.chat.id, left.id):
        return

    await _offboard_leaver(bot, registry, settings, chat_id=message.chat.id, leaver=left)


# ---------------------------------------------------------------------------
# Admin commands — configurable welcome template (L-57)
# ---------------------------------------------------------------------------


async def handle_set_welcome(
    message: Message,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
    data: dict[str, Any],
) -> None:
    """``/setwelcome <text>`` — store a custom welcome template (admin-gated).

    Placeholders ``{user}`` (joiner name) and ``{chat}`` (group title) are
    substituted at render time. The template is stored verbatim; escaping
    happens when a member joins, never at write time.
    """
    lang = resolve_lang(data, message.from_user)
    if not await _require_admin(message, bot, settings, lang):
        return

    parts = command_body(message).split(maxsplit=1)
    template = parts[1].strip() if len(parts) > 1 else ""
    if not template:
        await message.reply(t("h_welcome_set_usage", lang))
        return
    if len(template) > _WELCOME_TEMPLATE_MAX_LENGTH:
        await message.reply(t("h_welcome_set_too_long", lang, max=_WELCOME_TEMPLATE_MAX_LENGTH))
        return

    async with session_for(registry, DBName.MODERATION) as session:
        await WelcomeConfigRepo(session).set_template(message.chat.id, template)

    preview = render_welcome_template(
        template,
        user=(message.from_user.first_name if message.from_user else "User"),
        chat=message.chat.title or "",
    )
    await message.reply(t("h_welcome_set_ok", lang, preview=preview))
    log.bind(chat_id=message.chat.id).info("custom welcome template set")


async def handle_welcome_off(
    message: Message,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
    data: dict[str, Any],
) -> None:
    """``/welcome_off`` — disable the custom welcome (default card resumes)."""
    lang = resolve_lang(data, message.from_user)
    if not await _require_admin(message, bot, settings, lang):
        return
    async with session_for(registry, DBName.MODERATION) as session:
        await WelcomeConfigRepo(session).set_enabled(message.chat.id, enabled=False)
    await message.reply(t("h_welcome_off_ok", lang))
    log.bind(chat_id=message.chat.id).info("custom welcome disabled")


async def handle_welcome_on(
    message: Message,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
    data: dict[str, Any],
) -> None:
    """``/welcome_on`` — re-enable a previously-set custom welcome."""
    lang = resolve_lang(data, message.from_user)
    if not await _require_admin(message, bot, settings, lang):
        return
    async with session_for(registry, DBName.MODERATION) as session:
        repo = WelcomeConfigRepo(session)
        await repo.set_enabled(message.chat.id, enabled=True)
        row = await repo.get(message.chat.id)
    if row is None or not (row.template or "").strip():
        await message.reply(t("h_welcome_on_no_template", lang))
        return
    await message.reply(t("h_welcome_on_ok", lang))
    log.bind(chat_id=message.chat.id).info("custom welcome enabled")


async def handle_welcome_test(
    message: Message,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
    data: dict[str, Any],
) -> None:
    """``/welcome_test`` — preview the welcome the group will send.

    Renders the stored custom template (if set+enabled) against the
    caller's own name and the chat title, otherwise previews the default
    card. Admin-gated to avoid leaking config to non-admins.
    """
    lang = resolve_lang(data, message.from_user)
    if not await _require_admin(message, bot, settings, lang):
        return

    custom = await _custom_template_for(registry, message.chat.id)
    caller_name = message.from_user.first_name if message.from_user else "User"
    if custom is not None:
        preview = render_welcome_template(custom, user=caller_name, chat=message.chat.title or "")
        await message.reply(t("h_welcome_test_custom", lang, preview=preview))
    else:
        default = t("h_group_welcome_member", lang, name=html.escape(caller_name))
        await message.reply(t("h_welcome_test_default", lang, preview=default))


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — router for group-onboarding events + the welcome-config
    admin commands (L-57).

    No session middleware: the passive onboarding handlers resolve
    language off the Telegram payload, and the welcome-config commands
    open a short-lived MODERATION session on demand via ``session_for``
    (the welcome-config repo is the only table they touch, and these
    commands are rare). ``registry`` and ``settings`` are captured by the
    inner closures rather than injected via aiogram DI — the same pattern
    ``handlers/moderation.py`` uses.
    """
    router = Router(name="group_events")
    _group = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

    # The bot's own membership — ``my_chat_member`` carries status
    # changes about the bot account. Deliberately unfiltered beyond "is a
    # group": leaves and promotions have to reach the handler too, and
    # the join / leave / promotion branch lives inside it (#111).
    async def _bot_membership(event: ChatMemberUpdated, bot: Bot) -> None:
        await handle_bot_membership(event, bot, registry)

    router.my_chat_member.register(_bot_membership, _group)

    # --- welcome-config admin commands (registered before the catch-all
    # new-member handler; commands are message events with text, distinct
    # from the ``new_chat_members`` service message filter below). ---
    #
    # The wrappers take ``**data`` and not ``data: dict[str, Any]``.
    # aiogram injects handler arguments *by name* out of the middleware
    # data dict, and nothing ever puts a key literally called ``data``
    # in there — a named parameter therefore raises ``TypeError`` before
    # the body runs, which is exactly what these four did until #215.
    # ``**data`` is the codebase's idiom for asking for the whole dict
    # (cf. ``handlers/errors.py``); the inner handlers still receive it
    # positionally, so only the signature changes.

    async def _set_welcome(message: Message, bot: Bot, **data: Any) -> None:
        await handle_set_welcome(message, bot, settings, registry, data)

    router.message.register(
        _set_welcome,
        Command("setwelcome", "приветствие_текст", "set_welcome", ignore_case=True),
        F.from_user,
        _group,
    )

    async def _welcome_off(message: Message, bot: Bot, **data: Any) -> None:
        await handle_welcome_off(message, bot, settings, registry, data)

    router.message.register(
        _welcome_off,
        Command("welcome_off", "w_off", "приветствие_выкл", ignore_case=True),
        F.from_user,
        _group,
    )

    async def _welcome_on(message: Message, bot: Bot, **data: Any) -> None:
        await handle_welcome_on(message, bot, settings, registry, data)

    router.message.register(
        _welcome_on,
        Command("welcome_on", "w_on", "приветствие_вкл", ignore_case=True),
        F.from_user,
        _group,
    )

    async def _welcome_test(message: Message, bot: Bot, **data: Any) -> None:
        await handle_welcome_test(message, bot, settings, registry, data)

    router.message.register(
        _welcome_test,
        Command("welcome_test", "w_test", "приветствие_тест", ignore_case=True),
        F.from_user,
        _group,
    )

    # Human join(s) arrive as a service message with ``new_chat_members``.
    async def _new_members(message: Message, bot: Bot) -> None:
        await handle_new_members(message, bot, registry)

    router.message.register(
        _new_members,
        _group,
        F.new_chat_members,
    )

    # #245(d) + #605: the joins and the departures that produce no
    # service message — invite links, approved join requests, a chat with
    # join/leave notices switched off, "delete and leave". Registering
    # this is what puts ``chat_member`` into the resolved
    # ``allowed_updates`` set, which is pinned by
    # ``tests/integration/test_main_router_wiring.py``. One branching
    # handler, not two registrations — see
    # :func:`handle_member_changed`.
    async def _member_changed(event: ChatMemberUpdated, bot: Bot) -> None:
        await handle_member_changed(event, bot, registry, settings)

    router.chat_member.register(_member_changed, _group)

    # The announced departures: a service message carrying
    # ``left_chat_member``, already in the resolved ``allowed_updates``
    # set. Whichever transport lands first claims the leaver
    # (:data:`_RECENT_OFFBOARDS`) and the other one drops it.
    async def _left_member(message: Message, bot: Bot) -> None:
        await handle_left_member(message, bot, registry, settings)

    router.message.register(
        _left_member,
        _group,
        F.left_chat_member,
    )

    # L-55 captcha confirm button. ``lang`` is injected by the root
    # LanguageMiddleware (attached on ``callback_query`` too) — unlike
    # the service-message handlers above, callbacks DO pass through it.
    async def _captcha_confirm(
        callback: CallbackQuery,
        callback_data: CaptchaConfirm,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_captcha_confirm(callback, callback_data, bot, lang)

    router.callback_query.register(_captcha_confirm, CaptchaConfirm.filter())
    return with_chat_type_refusal(router, scope="group")
