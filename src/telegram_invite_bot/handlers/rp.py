"""Romance "RP-action" handler (FEAT-RP).

In a group, a user performs a roleplay action toward a partner by
REPLYING to the partner's message and writing a prefixed verb
(``.обнять``, ``бот поцеловать``, ``ком hug`` …). If the reply target is
the user's marriage/relationship partner the action grants XP to the
pair and renders a flavoured ``rel_rp_done_<name>`` message; if the verb
is "universal" and the target is a stranger it renders the 0-XP
``rel_rp_done_<name>_general`` variant; otherwise an error.

Plus ``/rp_commands`` (+ ``рп_команды``) — a private-or-group action
discovery list grouped by universal actions and per-level relationship
actions.

The whole intercept hinges on the filter (:func:`_is_rp_action`):
ONLY a group message whose text :func:`parse_rp_trigger` accepts reaches
``handle_rp_action``. Ordinary chatter parses to ``None`` and flows
through untouched — the no-chatter-theft rule (see ``core/rp_actions``).

18+ gate decision (AUD-4): the ported ``GroupSettings`` model now carries
``rp_18_enabled`` (master per-group toggle, default OFF) and
``rp_18_prompt_sent`` (one-time admin-enable affordance flag) — see
db/models/users.py and migration ``0006_rp_18_gate``. The gate is not a
helper of its own: :func:`_read_rp18_flags` reads both live flags and the
decision is taken inline in :func:`handle_rp_action`, ahead of the rate
limiter, so an action at ``RP_18_MIN_LEVEL`` or above in a non-enabled
group is REFUSED before a limiter slot is spent. The first such hit by a group admin shows a
one-time inline prompt (``rp18_prompt_text`` + ``rp18_enable_btn``); the
admin-gated ``rp18_enable`` callback flips ``rp_18_enabled`` on, after
which subsequent 18+ actions are permitted (subject to the usual relation
level gate). This restores legacy parity (``bot.py:21768-21783``) — the
feature is no longer default-allow.

VIP-outside decision (#270): a relationship-only verb aimed at someone
the caller has no bond with used to end at ``rel_rp_no_pair``, even for
a VIP — while ``/rp_commands`` printed ``rp_vip_outside`` promising the
opposite. The port had carried the advertisement across and left the
feature behind (legacy ``bot.py:21847-21853``). :func:`handle_rp_action`
now has the branch back, gated on ``RP_VIP_OUTSIDE_MIN_LEVEL`` and the
per-group ``rp_vip_outside_enabled`` flag (default **ON**, migration
``0010_rp_vip_outside``), and rendering the 0-XP ``_general`` variant.
The one deliberate divergence is which VIP grant is read — see
:func:`_has_active_vip`.

HTML parse_mode: first_names are attacker-controlled, so every mention
is built via :func:`~telegram_invite_bot.utils.names.mention`, which
escapes. No bare name is interpolated anywhere in this module.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.core.rp_actions import (
    RP_18_MIN_LEVEL,
    RP_ACTION_SPEC,
    RP_UNIVERSAL,
    RP_VIP_OUTSIDE_MIN_LEVEL,
    TRIGGERS_BY_NAME,
    parse_rp_trigger,
    rp_activity_key,
)
from telegram_invite_bot.db.models.users import GroupSettings
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.moderation import _is_user_admin
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.html import legacy_md_to_html
from telegram_invite_bot.utils.names import mention
from telegram_invite_bot.utils.numbers import format_number

log = logger.bind(component="handlers.rp")

# Callback data for the one-time 18+ enable button. Flat (no payload) —
# the group id is read from ``callback.message.chat.id`` so a stale button
# can never target a different chat than the one it was sent in.
_RP18_ENABLE_CB = "rp18_enable"

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo


class _Target:
    """Minimal resolved-target shape (id + first_name + is_bot).

    Unifies the two target sources: a replied-to ``message.from_user``
    and a sex-verb ``@username`` resolved through ``UsersRepo``. Both
    expose only the fields the render path needs.
    """

    __slots__ = ("id", "first_name", "is_bot")

    def __init__(self, *, id: int, first_name: str | None, is_bot: bool) -> None:  # noqa: A002
        self.id = id
        self.first_name = first_name
        self.is_bot = is_bot


# Relationship level names (mirrors handlers/marriage._RELATIONSHIP_LEVEL_NAMES).
_RELATIONSHIP_LEVEL_NAMES: dict[int, tuple[str, str]] = {
    1: ("Знакомые", "Acquaintances"),
    2: ("Дружеские отношения", "Friendly"),
    3: ("Тёплые отношения", "Warm"),
    4: ("Симпатия", "Crush"),
    5: ("Флирт", "Flirting"),
    6: ("Ухаживания", "Courtship"),
    7: ("Отношения", "Relationship"),
    8: ("Серьёзные отношения", "Serious"),
    9: ("Любовь", "Love"),
    10: ("Великая любовь", "Great love"),
    11: ("Любовь всей жизни", "Love of a lifetime"),
}

# Canonical (min_level, xp, name) per action ``name`` — de-duplicated
# from RP_ACTION_SPEC (which has ru + en triggers mapping to the same
# tuple) so the /rp_commands listing shows each action once.
_ACTIONS_BY_NAME: dict[str, tuple[int, int]] = {
    name: (min_level, xp) for (min_level, xp, name) in RP_ACTION_SPEC.values()
}


# ---------------------------------------------------------------------------
# In-memory rate limiter — 20 actions / 60s per (chat_id, user_id)
# ---------------------------------------------------------------------------


class RpRateLimiter:
    """Per-(chat, user) sliding-window limiter, clock injected for tests.

    ``time_fn`` defaults to ``time.monotonic`` in prod; tests swap it
    for a fake clock. This used to be described as mirroring a house
    style, which it no longer does in either direction: the in-memory
    roulette limiter it was modelled on was deleted by the L-25 cutover,
    and its persistent replacement
    (:class:`~telegram_invite_bot.services.game_limit_service.GameLimitService`)
    deliberately took the opposite posture — the caller passes ``now``
    per call, so there is no clock to inject. ``time_fn`` is the only
    injected clock left in the package; copy it only on purpose.

    Each deque is bounded by the window cap, but the NUMBER of deques is
    not — the limiter is module-level and lives for the whole process, so
    without :meth:`_sweep` it would keep one entry per (chat, user) pair
    that ever ran an RP action. Sweeping is exact here, so no LRU cap is
    needed: see :meth:`_sweep`.
    """

    _WINDOW_SEC = 60.0
    _MAX_IN_WINDOW = 20

    #: Calls between sweeps. The sweep is O(keys), so it must not run per
    #: call; at RP traffic rates this is minutes apart, and the table can
    #: only grow by this many keys between two sweeps.
    _SWEEP_EVERY = 256

    def __init__(self, *, time_fn: Callable[[], float]) -> None:
        self._time = time_fn
        self._hits: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._calls_since_sweep = 0

    def allow(self, chat_id: int, user_id: int) -> bool:
        """Record an attempt; return ``True`` if within the cap, else ``False``.

        Prunes stamps older than the window on every call (bounds the
        deque to ``_MAX_IN_WINDOW`` entries per key). A blocked attempt is
        NOT recorded, so a user who hits the cap can act again as soon as
        the oldest stamp ages out — not 60s after their last *rejected*
        try.
        """
        now = self._time()
        self._calls_since_sweep += 1
        if self._calls_since_sweep >= self._SWEEP_EVERY:
            self._sweep(now)
        stamps = self._hits[(chat_id, user_id)]
        floor = now - self._WINDOW_SEC
        while stamps and stamps[0] < floor:
            stamps.popleft()
        if len(stamps) >= self._MAX_IN_WINDOW:
            return False
        stamps.append(now)
        return True

    def _sweep(self, now: float) -> None:
        """Drop keys whose newest stamp has aged out of the window.

        Such a key is inert: every stamp in it is already older than the
        window, so the next :meth:`allow` would prune the deque to empty
        and admit — which is exactly what a MISSING key does. Dropping it
        therefore cannot change any decision, only the memory the table
        holds. That exactness is why this is a sweep and not an LRU cap:
        a cap would have to evict live entries too, and an evicted live
        entry hands a flooder a fresh allowance.
        """
        self._calls_since_sweep = 0
        floor = now - self._WINDOW_SEC
        stale = [key for key, stamps in self._hits.items() if not stamps or stamps[-1] < floor]
        for key in stale:
            del self._hits[key]


# Module-level limiter — shared across requests for the process lifetime.
# Tests swap ``handlers.rp._limiter`` for one wired to a fake clock.
_limiter = RpRateLimiter(time_fn=time.monotonic)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rel_level_name(level: int, lang: str) -> str:
    names = _RELATIONSHIP_LEVEL_NAMES.get(level, ("Отношения", "Relationship"))
    return names[0] if lang == "ru" else names[1]


async def _read_rp18_flags(session: AsyncSession, chat_id: int) -> tuple[bool, bool]:
    """Return ``(rp_18_enabled, rp_18_prompt_sent)`` for ``chat_id``.

    A missing ``group_settings`` row means the group has never been
    configured → both flags default OFF (legacy parity: 18+ disabled, no
    prompt shown yet). Integer 0/1 columns are coerced to bool.
    """
    row = (
        await session.execute(
            select(
                GroupSettings.rp_18_enabled,
                GroupSettings.rp_18_prompt_sent,
            ).where(GroupSettings.group_id == chat_id)
        )
    ).first()
    if row is None:
        return False, False
    enabled, prompt_sent = row
    return bool(enabled), bool(prompt_sent)


async def _set_rp18_flags(
    session: AsyncSession,
    chat_id: int,
    *,
    enabled: bool | None = None,
    prompt_sent: bool | None = None,
) -> None:
    """Upsert the 18+ flags for ``chat_id`` on the shared users session.

    INSERT ... ON CONFLICT(group_id) DO UPDATE touching ONLY the flags the
    caller passed (mirrors handlers/rules._store_rules — only the modelled
    columns are written; prod's other ``group_settings`` columns keep their
    values). Runs on the per-update users session the SessionMiddleware
    opened so the write shares that commit/rollback boundary (a second
    connection would deadlock SQLite — see the rules handler note).
    """
    values: dict[str, int] = {"group_id": chat_id}
    update_set: dict[str, int] = {}
    if enabled is not None:
        values["rp_18_enabled"] = int(enabled)
        update_set["rp_18_enabled"] = int(enabled)
    if prompt_sent is not None:
        values["rp_18_prompt_sent"] = int(prompt_sent)
        update_set["rp_18_prompt_sent"] = int(prompt_sent)
    stmt = sqlite_insert(GroupSettings).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[GroupSettings.group_id],
        set_=update_set,
    )
    await session.execute(stmt)
    await session.flush()


async def _rp_vip_outside_enabled(session: AsyncSession, chat_id: int) -> bool:
    """Return the group's ``rp_vip_outside_enabled`` flag (#270).

    A missing ``group_settings`` row means the group has never been
    configured, and the answer is then **True** — the opposite of
    :func:`_read_rp18_flags`. That asymmetry is legacy's and it is
    deliberate: the 18+ gate ships OFF because it opens content nobody
    asked for, while VIP-outside ships ON because it is a perk somebody
    paid for (``bot.py:5859`` ``DEFAULT 1``, echoed by the settings
    loader at ``bot.py:7798`` / ``bot.py:7827``). Reading a never-configured
    group as disabled would revoke a paid feature by omission.

    A row that exists but holds SQL ``NULL`` in this column reads as
    enabled too: legacy's startup ``ALTER TABLE`` backfilled old rows
    with the DEFAULT, but a row inserted by the new pipeline's
    ``_set_rp18_flags`` upsert names only the 18+ columns, and SQLite
    fills the rest of an INSERT from the column default — so NULL here
    means "never written", which is the same thing as "never configured".
    """
    row = (
        await session.execute(
            select(GroupSettings.rp_vip_outside_enabled).where(GroupSettings.group_id == chat_id)
        )
    ).first()
    if row is None or row[0] is None:
        return True
    return bool(row[0])


async def _has_active_vip(registry: EngineRegistry, user_id: int, chat_id: int) -> bool:
    """Is ``user_id`` an active VIP, for RP purposes (#270)?

    Opens its own short-lived read-only ``economy.db`` session. The RP
    router mounts only ``SessionMiddleware`` (users.db) and this is the
    single branch in the whole handler that needs the economy side, so
    paying for an economy session on every ``.обнять`` in every group
    would be the wrong trade — the repo has plenty of precedent for a
    handler opening a second-DB session on demand (handlers/rating.py,
    handlers/referrals.py, handlers/chatstats.py). A separate session
    against a *different* database cannot deadlock the users session the
    middleware is holding.

    Deliberate divergence from legacy, and the reason this branch is not
    dead code. Legacy asked only for the GROUP-scoped grant
    (``bot.py:21849`` passes ``group_id=chat_id``, and
    ``get_vip_profile`` with a ``group_id`` reads ``user_group_vip``
    alone, never falling back). But the port grants VIP only globally —
    ``InventoryUseService`` calls :meth:`VipRepo.grant_global` and no
    code path in this package creates a ``user_group_vip`` row (the
    group-migration fold rewrites the ``group_id`` on rows that already
    exist, which is not a grant) — and a read-only count on
    prod's economy.db returns **0** group-scoped rows against a live
    global grant. Porting legacy's read verbatim would therefore have
    shipped a branch that can never fire for anybody who bought VIP
    through this pipeline: the ticket's complaint would survive the fix.
    So the global grant is checked first (it is the one the port issues)
    and the group-scoped grant second, which keeps any legacy-era
    ``user_group_vip`` row working exactly as it did.
    """
    now = datetime.now(UTC)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        repo = VipRepo(session)
        if await repo.get_active_profile(user_id, now=now) is not None:
            return True
        return await repo.get_active_profile(user_id, now=now, group_id=chat_id) is not None


def _rp18_enable_markup(lang: str) -> InlineKeyboardMarkup:
    """The one-time "enable 18+ RP" inline keyboard (single button).

    Reuses the orphaned ``rp18_enable_btn`` i18n key. The button carries no
    payload (:data:`_RP18_ENABLE_CB`) — the target group is read from the
    callback's own message chat, so a forwarded/stale button can never flip
    a different group's flag.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("rp18_enable_btn", lang),
                    callback_data=_RP18_ENABLE_CB,
                )
            ]
        ]
    )


async def _refuse_rp18(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    prompt_sent: bool,
    lang: str,
) -> None:
    """Refuse a 18+ action in a non-enabled group (legacy bot.py:21768-21783).

    If the caller is a confirmed group admin AND the one-time prompt has
    not yet been shown for this group, render ``rp18_prompt_text`` with the
    inline enable button and mark ``rp_18_prompt_sent`` so it shows at most
    once. Everyone else (non-admins, or admins after the prompt was already
    shown) gets the plain ``rp18_disabled`` refusal.

    The admin check is :func:`moderation._is_user_admin` (TG admin/creator,
    matching legacy ``has_group_admin_rights``); its ``None`` (Telegram-API
    error) return is treated as "not an admin" here — fail-closed to the
    plain refusal, never fail-open to showing the enable affordance.
    """
    is_admin = await _is_user_admin(bot, chat_id, user_id) is True
    if is_admin and not prompt_sent:
        # Mark the prompt sent BEFORE replying so a racing second 18+ hit
        # in the same window doesn't double-prompt (the write is on the
        # shared per-update session and commits with the handler).
        await _set_rp18_flags(session, chat_id, prompt_sent=True)
        text = legacy_md_to_html(t("rp18_prompt_text", lang))
        await message.reply(text, reply_markup=_rp18_enable_markup(lang))
        log.bind(chat_id=chat_id, user_id=user_id).info("rp18 enable prompt shown")
        return
    await message.reply(t("rp18_disabled", lang))


async def handle_rp18_enable(
    callback: CallbackQuery,
    bot: Bot,
    users_repo: UsersRepo,
    lang: str,
) -> None:
    """Admin-gated callback: flip ``rp_18_enabled`` ON for the group.

    Legacy ``cb_rp18_once_yes`` (bot.py:23561-23574): only a group admin
    may enable, and the toggle is per-group. We re-check adminship at
    click-time (the button is public — anyone in the group can see it) via
    :func:`moderation._is_user_admin`; a non-admin (or a Telegram-API error,
    fail-closed) gets a ``no_access`` toast and no write. On success we
    answer with the ``rp18_enabled`` toast and edit the prompt away.
    """
    assert callback.from_user is not None
    if callback.message is None or callback.message.chat.id >= 0:
        # Private chats have positive ids — the 18+ gate is group-only, so a
        # callback outside a group is malformed. Ack silently.
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    if await _is_user_admin(bot, chat_id, user_id) is not True:
        await callback.answer(t("no_access", lang)[:200], show_alert=True)
        return

    await _set_rp18_flags(
        users_repo._session,  # noqa: SLF001 — shared per-update users session
        chat_id,
        enabled=True,
    )
    await callback.answer(t("rp18_enabled", lang)[:200])
    try:
        await bot.edit_message_text(
            f"🔞 <b>{t('rp18_enabled', lang)}</b>",
            chat_id=chat_id,
            message_id=callback.message.message_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort edit; the flag is already set
        log.bind(chat_id=chat_id, err=str(exc)).debug("rp18 prompt edit failed")
    log.bind(chat_id=chat_id, user_id=user_id).info("rp18 enabled by admin")


#: #271: statuses that mean "not in this chat right now". Same set as
#: ``group_events._OUT_STATUSES`` / ``groupadmin``; kept local rather
#: than imported so no handler module depends on another.
_OUT_STATUSES: Final = frozenset({ChatMemberStatus.LEFT, ChatMemberStatus.KICKED})


async def _member_of_chat(bot: Bot, chat_id: int, user_id: int) -> bool:
    """True only when Telegram confirms the user is in this chat.

    #271: fails CLOSED. An API error, an unknown user, or a ``left`` /
    ``kicked`` status all return ``False`` — the caller then treats the
    ``@username`` as unresolved and replies ``rel_rp_reply``.

    Deliberate divergence from legacy (bot.py:21800-21805): legacy
    accepted whatever ``get_chat_member`` returned as long as it
    carried a ``user``, so a ``left`` member still qualified. That is
    exactly the case this check exists to stop — a public 18+ line
    naming someone who is not in the room — so the port narrows it.
    The fail-closed-on-exception half IS legacy parity (its bare
    ``except Exception: pass`` left ``target`` at ``None``).
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as exc:
        log.bind(chat_id=chat_id, target=user_id, exc=str(exc)).info(
            "rp: get_chat_member failed — @username target rejected"
        )
        return False
    return member.status not in _OUT_STATUSES


def _first_username_mention(message: Message) -> str | None:
    """Return the first ``@username`` token in the message, or ``None``.

    Reads the ``mention`` entities Telegram attaches (type ``"mention"``
    is the plain ``@handle`` form; ``"text_mention"`` carries an inline
    user object and is handled separately by the reply path). Used only
    by the sex-verb branch so those verbs can target a user who isn't the
    replied-to author.
    """
    text = message.text or ""
    for entity in message.entities or []:
        if entity.type == "mention":
            handle = text[entity.offset : entity.offset + entity.length]
            cleaned = handle.lstrip("@").strip()
            if cleaned:
                return cleaned
    return None


# ---------------------------------------------------------------------------
# Filter — only a valid RP action in a group reaches the handler
# ---------------------------------------------------------------------------


def _is_rp_action(message: Message) -> bool:
    """True only for a group message whose text parses as an RP action.

    This is the gate that keeps the handler from stealing ordinary
    chatter: a non-prefixed message (and any prefixed non-verb) parses to
    ``None`` and the filter rejects it, so the update flows through to
    every later router untouched.
    """
    if message.chat.type not in GROUP_TYPES:
        return False
    if not message.text:
        return False
    return parse_rp_trigger(message.text) is not None


# ---------------------------------------------------------------------------
# RP action handler
# ---------------------------------------------------------------------------


async def handle_rp_action(
    message: Message,
    bot: Bot,
    bonds_write_repo: BondsWriteRepo,
    users_repo: UsersRepo,
    lang: str,
    registry: EngineRegistry,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Resolve a replied-to RP action: grant pair XP or render a general/error."""
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    parsed = parse_rp_trigger(message.text)
    if parsed is None:  # defensive — the filter should have excluded this
        return
    _, min_level, xp, name = parsed
    is_sex = name == "sex"

    # 18+ gate (AUD-4) — actions at RP_18_MIN_LEVEL or above require the
    # per-group ``rp_18_enabled`` flag (default OFF). The first such hit by
    # a group admin in a not-yet-enabled, not-yet-prompted group shows a
    # one-time inline enable prompt; otherwise the action is refused with
    # ``rp18_disabled``. Mirrors legacy bot.py:21768-21783 — including its
    # POSITION: legacy gated here, ahead of both the rate limiter
    # (bot.py:21786) and target resolution (bot.py:21790-21805), so the
    # one-time admin prompt is never swallowed by a rate-limit refusal.
    if min_level >= RP_18_MIN_LEVEL:
        session = users_repo._session  # noqa: SLF001 — shared per-update users session
        enabled, prompt_sent = await _read_rp18_flags(session, chat_id)
        if not enabled:
            await _refuse_rp18(message, bot, session, chat_id, user_id, prompt_sent, lang)
            return

    # Rate limit — legacy bot.py:21786-21788, and deliberately BEFORE target
    # resolution: resolving a bare ``@username`` spends an outbound
    # ``get_chat_member`` (:func:`_member_of_chat`). A limiter placed after
    # that lets one user drive the call at Telegram's own inbound message
    # rate — the reply is refused every time, but the API call has already
    # left — and a global 429 there degrades the whole bot, not one chat.
    # Spending a slot on a malformed reply is legacy's own trade and by far
    # the cheaper of the two.
    if not _limiter.allow(chat_id, user_id):
        await message.reply(t("rel_rp_rate_limit", lang))
        return

    # Resolve the TARGET. The replied-to user is the canonical source; the
    # sex verbs additionally accept a bare ``@username`` so they can fire
    # without a reply, resolved through ``UsersRepo``.
    #
    # #271: ``UsersRepo.get_by_username`` is a flat lookup across the
    # WHOLE users.db — it carries no ``chat_id``, so on its own it will
    # happily resolve a handle belonging to someone who has never been in
    # this group. Publishing ``rel_rp_done_sex_general`` about them is the
    # bug. Every ``@username`` hit is therefore confirmed against a live
    # ``get_chat_member`` (:func:`_member_of_chat`, fails closed), which
    # is what legacy did at bot.py:21800-21805 and the port had dropped.
    #
    # DIVERGENCE (deliberate): legacy gated the ``@username`` path on the
    # two bare RUSSIAN verbs only (bot.py:21794), so its English twin
    # ``sex`` still needed a reply. The port keys on the resolved action
    # NAME, and :data:`core.rp_actions.RP_ACTION_SPEC` maps all three
    # triggers to ``"sex"`` — so the EN trigger reaches the same path. An
    # EN user must not get a narrower command surface than a RU one. What
    # this narrows is consent-by-reply for that one verb; group membership
    # (:func:`_member_of_chat`) and the per-group 18+ flag both still gate
    # it, and the 18+ gate above runs first.
    target: _Target | None = None
    if message.reply_to_message and message.reply_to_message.from_user:
        candidate = message.reply_to_message.from_user
        target = _Target(
            id=candidate.id,
            first_name=candidate.first_name,
            is_bot=candidate.is_bot,
        )
    if target is None and is_sex:
        handle = _first_username_mention(message)
        if handle is not None:
            user = await users_repo.get_by_username(handle)
            if user is not None and await _member_of_chat(bot, chat_id, user.user_id):
                target = _Target(
                    id=user.user_id,
                    first_name=user.first_name,
                    is_bot=False,
                )
    if target is None:
        await message.reply(t("rel_rp_reply", lang))
        return
    if target.id == user_id:
        await message.reply(t("rel_rp_self", lang))
        return
    if target.is_bot:
        # Legacy bot.py:21815-21816 returns False here and sends NOTHING.
        # A group that answers the bot's own posts with RP verbs would
        # otherwise collect one refusal per post. The check sits AFTER the
        # self check, exactly where legacy had it, so a reply to a bot is
        # never re-interpreted through the ``@username`` branch above.
        return

    target_id = target.id

    from_mention = mention(user_id, tg_user.first_name, lang)
    to_mention = mention(target_id, target.first_name, lang)

    # Marriage first: if the replied-to user is the caller's spouse, grant
    # marriage XP.
    marriage = await bonds_write_repo.get_marriage(chat_id, user_id)
    if marriage is not None and target_id in (marriage.user1_id, marriage.user2_id):
        # Bound and tested. The row was read one statement ago, but the
        # UPDATE re-checks ``status`` in SQL, so a /divorce that landed
        # in between makes this ``None``. Filing a joint-activity row
        # then would put an action into the history of a bond that no
        # longer exists, and the reply would announce XP nobody got.
        # On ``None`` nothing is written and control falls through to
        # the no-pair tail — exactly where this request would have gone
        # had the divorce landed one statement earlier.
        new_exp = await bonds_write_repo.add_marriage_xp(chat_id, user_id, xp)
        if new_exp is not None:
            # #231: legacy filed every RP action in the pair's joint-activity
            # log next to the paid activities (bot.py:21825 → :21656). The
            # port granted the XP but wrote no row, so a couple's history
            # listed only the six purchasable activities — while the 26 RP
            # verbs, by far the commoner half, left no trace. ``xp`` may be
            # 0 (the sex verbs); legacy logged those too, and the 0-XP row
            # is the whole record of the action having happened.
            await bonds_write_repo.log_marriage_activity(
                chat_id,
                marriage.user1_id,
                marriage.user2_id,
                rp_activity_key(name),
                xp,
                user_id,
            )
            # #1874: the XP grant and the activity row are both guarded
            # writes, so the bonds DB is locked from ``add_marriage_xp``
            # onwards. RP verbs are the commonest write in a busy group
            # — holding the writer slot across the congratulation would
            # queue every other couple behind one FloodWait. Both writes
            # have landed and must stand whether or not the reply does.
            if checkpoint is not None:
                await checkpoint()
            await message.reply(
                t(
                    f"rel_rp_done_{name}",
                    lang,
                    from_mention=from_mention,
                    to_mention=to_mention,
                    xp=xp,
                )
            )
            log.bind(chat_id=chat_id, user_id=user_id, action=name, xp=xp).info(
                "rp action (marriage)"
            )
            return
        log.bind(chat_id=chat_id, user_id=user_id, action=name).warning(
            "rp marriage vanished between read and update"
        )
        # #1874: the guarded UPDATE took the lock in order to report
        # that it matched nothing. Control now falls through to the
        # relationship read and, failing that, to a reply — let go here
        # rather than carry a lock over a divorce race that wrote
        # nothing. A later branch that writes will take it again.
        if checkpoint is not None:
            await checkpoint()

    # Relationship: if the pair has an active relationship, level-gate then
    # grant relationship XP.
    relationship = await bonds_write_repo.get_relationship(chat_id, user_id, target_id)
    if relationship is not None:
        rel_level = bonds_write_repo._rel_xp_to_level(relationship.experience or 0)
        # Legacy special-case (bot.py:21832-21836): a brand-new level-0
        # relationship (0 XP) may still use the most basic level-1 RP verbs
        # — ``level >= min_level OR (level == 0 and min_level == 1)``.
        if rel_level < min_level and not (rel_level == 0 and min_level == 1):
            await message.reply(
                t(
                    "rel_rp_level_required",
                    lang,
                    level=min_level,
                    level_name=_rel_level_name(min_level, lang),
                )
            )
            return
        # Same read-then-update race as the marriage branch above, and
        # the same answer: no XP means no log row and no congratulation.
        new_exp = await bonds_write_repo.add_relationship_xp(chat_id, user_id, target_id, xp)
        if new_exp is not None:
            # #231, relationship half — legacy bot.py:21837 → :22393. The
            # repo normalises the pair, so passing (caller, target) is the
            # same row legacy built from rel["user1_id"]/rel["user2_id"].
            await bonds_write_repo.log_relationship_activity(
                chat_id, user_id, target_id, rp_activity_key(name), xp, user_id
            )
            # #1874: same as the marriage branch — XP and the activity
            # row are written, so commit before the congratulation.
            if checkpoint is not None:
                await checkpoint()
            await message.reply(
                t(
                    f"rel_rp_done_{name}",
                    lang,
                    from_mention=from_mention,
                    to_mention=to_mention,
                    xp=xp,
                )
            )
            log.bind(chat_id=chat_id, user_id=user_id, action=name, xp=xp).info(
                "rp action (relationship)"
            )
            return
        log.bind(chat_id=chat_id, user_id=user_id, action=name).warning(
            "rp relationship vanished between read and update"
        )
        return

    # No pair. Universal actions render the 0-XP general variant; the rest
    # are relationship-only.
    if name in RP_UNIVERSAL:
        await message.reply(
            t(
                f"rel_rp_done_{name}_general",
                lang,
                from_mention=from_mention,
                to_mention=to_mention,
                xp=0,
            )
        )
        log.bind(chat_id=chat_id, user_id=user_id, action=name).info("rp action (general, no pair)")
        return

    # #270: VIP outside a relationship. A relationship-only action, no
    # pair — but an active VIP in a group that has not switched the perk
    # off performs it anyway, rendered with the same 0-XP ``_general``
    # variant the universal verbs use. Legacy ``bot.py:21847-21853``,
    # in exactly this position: after the universal branch, before the
    # no-pair refusal, so it only ever catches what would otherwise have
    # been refused.
    #
    # No XP is granted and none is rendered — legacy passes no ``xp`` to
    # the key, and all eight relationship-only ``_general`` strings take
    # only ``{from_mention}`` / ``{to_mention}`` in both catalogues. The
    # perk buys the *action*, not pair progress the buyer has no pair to
    # bank: XP here would be XP into nothing.
    #
    # This is what ``/rp_commands`` has been advertising all along via
    # ``rp_vip_outside`` (rendered at the bottom of the list, below) with
    # nothing behind it — the port carried the promise across and left
    # the feature. The group-admin toggle UI for the flag
    # (legacy ``bot.py:27414``, ``bot.py:27544-27546``, inside
    # ``/groupadmin``) is NOT ported here; the flag defaults ON, so the
    # unported half can only fail in the direction of honouring the
    # advertisement.
    # Both ``and``s short-circuit, in cost order: the level test is free,
    # the flag read rides the users session the middleware already holds,
    # and only then does the economy.db lookup happen.
    perk_on = min_level >= RP_VIP_OUTSIDE_MIN_LEVEL and await _rp_vip_outside_enabled(
        users_repo._session,  # noqa: SLF001 — shared per-update users session
        chat_id,
    )
    if perk_on and await _has_active_vip(registry, user_id, chat_id):
        await message.reply(
            t(
                f"rel_rp_done_{name}_general",
                lang,
                from_mention=from_mention,
                to_mention=to_mention,
            )
        )
        log.bind(chat_id=chat_id, user_id=user_id, action=name).info(
            "rp action (vip outside relationship)"
        )
        return

    await message.reply(t("rel_rp_no_pair", lang))


# ---------------------------------------------------------------------------
# /rp_commands — action discovery list (private OR group)
# ---------------------------------------------------------------------------


#: Trigger words listed per script in the universal block before the
#: overflow marker kicks in, and actions listed per level block.
#: Both bounds are legacy's (bot.py:23525/23551). Neither fires at the
#: current spec size — they are a message-length backstop for a future
#: spec that grows past what one 4096-char Telegram message can hold.
_UNIVERSAL_TRIGGER_CAP: Final[int] = 25
_LEVEL_ACTION_CAP: Final[int] = 15

_OVERFLOW_MARKER: Final[str] = "…"

_CYRILLIC_RANGE: Final[tuple[str, str]] = ("Ѐ", "ӿ")


def _is_cyrillic(text: str) -> bool:
    """True if ``text`` contains at least one Cyrillic letter.

    The script split (not the UI language) decides which label a trigger
    goes under, exactly as legacy did: BOTH lists are shown to BOTH
    locales, because both spellings genuinely work and the point of the
    section is discovery.
    """
    low, high = _CYRILLIC_RANGE
    return any(low <= char <= high for char in text)


def _triggers_for(name: str) -> tuple[str, ...]:
    """Trigger words for ``name``, falling back to the name itself."""
    return TRIGGERS_BY_NAME.get(name, (name,))


def _join_capped(items: list[str], cap: int) -> str:
    """``", "``-join at most ``cap`` items, marking the truncation."""
    joined = ", ".join(items[:cap])
    return joined + _OVERFLOW_MARKER if len(items) > cap else joined


def _open_action_names() -> list[str]:
    """Action names shown as "usable on anyone, no relationship".

    Lists ONLY the genuinely no-relationship-required actions: those that
    are both in :data:`RP_UNIVERSAL` (the handler renders a 0-XP general
    variant when there is no pair) AND require no relationship level
    (``min_level == 1``). Higher-level actions that happen to be in
    ``RP_UNIVERSAL`` (e.g. hug Lvl 2, kiss/lick/gift Lvl 5, sex/compliment
    Lvl 6) are NOT listed here — they appear under their level block. This
    resolves the contradiction where an action was shown both as
    "universal" and as level-locked, and stops intimate verbs from being
    advertised as usable "on anyone". The handler's gate is untouched.
    """
    return [n for n in RP_UNIVERSAL if _ACTIONS_BY_NAME.get(n, (1, 0))[0] <= 1]


def _build_universal_block(lang: str) -> list[str]:
    """The "usable on anyone" section, split into RU and EN trigger words.

    RR-5 #54: the port printed one merged list of DISPLAY LABELS in the
    caller's language, so half the vocabulary was invisible per locale
    and none of it was guaranteed typeable. Legacy printed the trigger
    words themselves under a ``Русские:`` / ``English:`` label pair
    (bot.py:23523-23528) — restored here.
    """
    triggers = sorted(
        {trigger for name in _open_action_names() for trigger in _triggers_for(name)},
        key=str.lower,
    )
    lines = [t("h_rp_actions_open_header", lang)]
    for label_key, group in (
        ("rp_universal_ru_label", [x for x in triggers if _is_cyrillic(x)]),
        ("rp_universal_en_label", [x for x in triggers if not _is_cyrillic(x)]),
    ):
        if group:
            lines.append(f"{t(label_key, lang)} {_join_capped(group, _UNIVERSAL_TRIGGER_CAP)}")
    return lines


def _build_level_blocks(lang: str) -> list[str]:
    """Per-level action lines: the typeable synonyms, not just a label.

    RR-5 #58: each row now reads ``обнять · hug — +10 XP`` instead of a
    bare localised label, so the list doubles as the command reference
    it is meant to be. Actions already listed in the "usable on anyone"
    block are skipped (legacy skipped every universal; we skip only the
    ones actually shown above, since the higher-level universals are
    level-locked here and belong under their level).

    Rows keep :data:`RP_ACTION_SPEC` order rather than being sorted —
    the spec is a curated progression from a handshake to a wedding, and
    alphabetising it destroys that reading order.
    """
    shown_above = set(_open_action_names())
    by_level: dict[int, list[str]] = defaultdict(list)
    for name in dict.fromkeys(spec[2] for spec in RP_ACTION_SPEC.values()):
        if name in shown_above:
            continue
        min_level, xp = _ACTIONS_BY_NAME[name]
        synonyms = " · ".join(_triggers_for(name))
        by_level[min_level].append(f"{synonyms} — +{xp} XP")
    lines: list[str] = []
    for level in sorted(by_level):
        lines.append(
            t(
                "rp_level_line",
                lang,
                lev=level,
                name=_rel_level_name(level, lang),
                xp_req=format_number(_rel_xp_threshold(level)),
            )
        )
        rows = by_level[level]
        lines.extend(f"• {row}" for row in rows[:_LEVEL_ACTION_CAP])
        if len(rows) > _LEVEL_ACTION_CAP:
            lines.append(_OVERFLOW_MARKER)
    return lines


def _rel_xp_threshold(level: int) -> int:
    from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo

    xps = BondsWriteRepo.RELATIONSHIP_LEVEL_XP
    return xps[level] if level < len(xps) else xps[-1]


async def handle_rp_commands(
    message: Message,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
) -> None:
    """List RP actions by level. Works in private AND group chats."""
    tg_user = require_from_user(message)

    lines: list[str] = []
    in_group = message.chat.type in GROUP_TYPES

    if in_group:
        # Show the caller's max relationship level in this chat.
        rels = await bonds_write_repo.list_relationships_for(message.chat.id, tg_user.id)
        max_level = 0
        for rel in rels:
            lvl = bonds_write_repo._rel_xp_to_level(rel.experience or 0)
            max_level = max(max_level, lvl)
        if max_level > 0:
            lines.append(
                legacy_md_to_html(
                    t(
                        "rel_rp_commands_your_level",
                        lang,
                        level=max_level,
                        level_name=_rel_level_name(max_level, lang),
                    )
                )
            )
        else:
            lines.append(legacy_md_to_html(t("rel_rp_commands_no_rel", lang)))
    else:
        lines.append(legacy_md_to_html(t("rel_rp_commands_private", lang)))

    lines.append("")
    lines.extend(_build_universal_block(lang))
    lines.append("")
    lines.extend(_build_level_blocks(lang))

    # Restored trailing blocks (RR-5 #55-57): the rich i18n strings exist
    # but were no longer rendered after the split — VIP-outside note, the
    # marriage-unlock callout, and the closing how-to-use hint.
    lines.append("")
    lines.append(legacy_md_to_html(t("rp_vip_outside", lang)))
    marry_level = bonds_write_repo.MARRIAGE_MIN_REL_LEVEL
    lines.append(
        t(
            "rel_marry_at_level6",
            lang,
            level=marry_level,
            level_name=_rel_level_name(marry_level, lang),
            xp_req=_rel_xp_threshold(marry_level),
        )
    )
    lines.append("")
    lines.append(t("rel_rp_commands_hint", lang))
    await message.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry) -> Router:
    """Build the RP-action router.

    Mounts its own ``SessionMiddleware`` (users.db) so the bond repo
    shares the per-update transaction with the marriage/relations
    handlers. The action handler is registered behind the
    :func:`_is_rp_action` filter — only a valid group RP message reaches
    it, so non-action chatter falls through to every later router.
    ``/rp_commands`` is registered separately (any chat type). The 18+
    enable callback (AUD-4) gets the same ``SessionMiddleware`` on the
    callback_query side so it can write ``rp_18_enabled`` on the shared
    users session.

    The action handler is wrapped in a closure that hands it ``registry``
    (#270): the VIP-outside branch reads economy.db, and a closure keeps
    that cost inside the one branch that pays it.
    """
    router = Router(name="rp")
    router.message.middleware(SessionMiddleware(registry))
    router.callback_query.middleware(SessionMiddleware(registry))

    # /rp_commands (+ ru alias) — works in private AND group.
    router.message.register(
        handle_rp_commands,
        Command("rp_commands", "rp", "рп_команды", ignore_case=True),
        F.from_user,
    )

    # RP action — group-only, gated by the parse filter so ordinary
    # chatter is never intercepted. The registry rides in through a
    # closure rather than a middleware: only the #270 VIP branch needs
    # it, and mounting ``EconomyMiddleware`` here would open an
    # economy.db session for every RP action in every group to serve the
    # rarest one.
    async def _rp_action_entry(
        message: Message,
        bot: Bot,
        bonds_write_repo: BondsWriteRepo,
        users_repo: UsersRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_rp_action(
            message, bot, bonds_write_repo, users_repo, lang, registry, checkpoint
        )

    router.message.register(_rp_action_entry, _is_rp_action, F.from_user)
    # AUD-4: one-time 18+ enable button → admin-gated flag flip.
    router.callback_query.register(handle_rp18_enable, F.data == _RP18_ENABLE_CB)

    return router
