"""``/transfer_rights`` — transfer bot-side group ownership (L-49).

Legacy ``cmd_transfer_rights`` plus its confirm callback
(bot.py:41603-41759 — 41766 is ``get_developer_testing_mode``, an
unrelated function; the command alone ends at 41687) semantics:

* **DM-only** — a group invocation got the "только в личных сообщениях"
  one-liner (bot.py:41615-41617).
* **Caller gate** — ``is_owner(from_user.id)`` (bot.py:41618). #699:
  that predicate is *membership in* ``DEVELOPER_IDS``
  (bot.py:41771-41775, with a "testing as a group admin" carve-out),
  **not** an equality test against the ``settings.json``
  ``owner_user_id``. The owner id is merely appended to the developer
  list at import (bot.py:3074-3078), so every developer could call
  ``/transfer_rights``. Tightening this port's gate down to a single
  configured owner would therefore be a behaviour change, not a
  hardening — the port's own gate is correct as it stands.
* **Target resolution** (bot.py:41629-41658) — reply-to / ``@username``
  (via the users-DB lookup ``_lookup_user_id_by_username``) / numeric
  id; the target had to exist in the users DB ("должен хотя бы раз
  написать боту"), could not be the caller, could not be a bot.
* **Membership gate** — ``is_user_in_chat(target_id)``, applied
  **twice**: once at target resolution (bot.py:41659-41661) and again
  at the confirm callback (bot.py:41722-41724), because the target can
  leave inside the 5-minute confirmation window. Legacy fails
  **closed**: ``get_chat_member`` raising means "no" (bot.py:44731-44736).
* **Confirmation step** (bot.py:41663-41687) — a 5-minute pending token
  with ✅ Подтвердить / ❌ Отмена inline buttons; the callback
  re-checked ``is_owner`` and the token's ``from_id``.
* **Rate limit** (bot.py:41622-41627) — a 30s per-caller cooldown via
  ``cache_set(ttl=30)``.
* **Effect** (bot.py:41726-41730) — rewrote ``settings.json``'s global
  ``owner_user_id`` + ``ADMIN_CHAT_ID``, then best-effort DM'd the new
  owner (bot.py:41745-41753).

The new pipeline is multi-group: "ownership" is the per-group
attribution ``bot_groups.added_by_user_id`` (users.db) that ``/mygroups``
and the group-admin gate key off. So the port re-targets the same
confirmed, rate-limited, DM-only flow at one group:

1. ``/transfer_rights`` (DM) → picker over the caller's OWN groups
   (same list/scoping as ``/mygroups``).
2. Pick → FSM asks for the target (``@username`` or numeric id, both
   resolved against ``users.db`` — the "must have talked to the bot"
   rule carries over; reply-to has no DM equivalent worth keeping).
3. Confirm/cancel inline card (owner-pinned payload).
4. Confirm → atomic guarded UPDATE of ``added_by_user_id`` via
   :class:`BotGroupsRepo`, then best-effort DM to the new owner.

#700: the membership gate carries over, re-aimed the same way the rest
of the flow is. Legacy had one "main group" to be a member of; here the
group is the one being transferred, so the target must be in **that**
chat — a stronger test than legacy's, and the only one that means
anything once ownership is per-group. It runs at both of legacy's
points and fails closed at both (:func:`_is_member`). Without it,
ownership of a group could be handed to somebody who had already left
it, and the recipient — who is the only person who can transfer it back
— would have to rejoin to do so.

Rate limit: **1 transfer per group per 24h** (in-process TTL map, the
``rank_self.py`` ``_SYNC_CACHE`` pattern) — stricter than legacy's 30s
anti-double-click because the multi-group effect is per-group and
reversible only by the recipient. Process restart clears it (accepted:
same trade every in-process limiter here makes).

Authorisation note: every callback re-derives ownership from the DB
(``BotGroupsRepo.get_owned`` / the UPDATE's WHERE guard) — the FSM and
payloads are never trusted to assert "still the owner".
"""

from __future__ import annotations

import contextlib
import html
import time
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message as MessageType
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND, register_text_expected
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.transfer_rights import (
    PAGE_SIZE,
    TransferDecision,
    TransferPage,
    TransferPick,
    build_confirm_markup,
    build_pick_markup,
    total_pages,
)
from telegram_invite_bot.repositories.bot_groups_repo import BotGroupsRepo
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD, utc_now_iso
from telegram_invite_bot.utils.aiogram import edit_card
from telegram_invite_bot.utils.numbers import parse_int_token

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.fsm.context import FSMContext
    from aiogram.types import (
        CallbackQuery,
        InlineKeyboardMarkup,
        Message,
        ResultChatMemberUnion,
    )

    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="handlers.transfer_rights")


async def on_expire_transfer(bot: Bot, key: object, data: dict[str, object]) -> None:
    """FSM-sweeper timeout for the ``/transfer_rights`` flow.

    Nothing irreversible happens before the final confirm. Without this
    rule the abandoned flow's busy-gate (``handle_transfer_start`` rejects
    re-entry with ``h_trights_busy`` while state is set) never clears,
    locking the owner out of ``/transfer_rights`` until ``/cancel``. The
    sweeper clears the state after this returns; we DM the user as a nudge.
    """
    user_id = getattr(key, "user_id", None)
    if not isinstance(user_id, int):
        return
    lang_raw = data.get(_FD_LANG)
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(user_id, t("h_trights_timeout", lang))
    log.bind(uid=user_id).info("/transfer_rights flow expired by sweeper")


# One transfer per group per 24h. Legacy's 30s cooldown was a per-caller
# anti-double-click; the per-group day window is the L-49 spec for the
# multi-group effect.
TRANSFER_COOLDOWN_SECONDS = 24 * 60 * 60

# The sweep in :class:`_CooldownTable` fires once the table passes this
# many entries; below it the table is small enough that reclaiming is
# not worth a scan.
_SWEEP_WATERMARK_MIN = 256


#: A member who left or was banned is not "in the chat". Legacy spelled
#: the same verdict as an allow-list of the four present statuses
#: (bot.py:44727) — identical in effect, and stated as a deny-list here
#: so a status Telegram adds later reads as "present" rather than
#: silently blocking every transfer. Mirrors ``handlers/report.py:136``.
_ABSENT_STATUSES = frozenset({ChatMemberStatus.LEFT, ChatMemberStatus.KICKED})


async def _member_of(bot: Bot, chat_id: int, user_id: int) -> ResultChatMemberUnion | None:
    """``getChatMember`` for the transfer gates. Fails **closed** (#700).

    ``None`` means "the API did not answer", and every caller reads that
    as a refusal. An API error leaves us unable to establish that the
    recipient is in the group we are about to hand them, and the
    transfer is only reversible *by them*. Refusing is the cheap
    failure: the owner retries, or the target joins. Legacy took the
    same direction (bot.py:44731-44736), and so does the ``/report`` DM
    path (``handlers/report.py:215-230``), which this mirrors.

    Not cached, unlike legacy's ``is_user_in_chat``: the whole point of
    checking twice is to catch a target who left inside the
    confirmation window, and a cache older than that window would hand
    back the pre-departure answer at exactly the moment it matters.

    Returns the member rather than a verdict because the target step
    needs the ``is_bot`` flag off the same answer, and one round-trip is
    the whole budget here.
    """
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — see the fail-closed note above
        log.bind(chat_id=chat_id, uid=user_id).warning(
            "transfer_rights: get_chat_member failed, refusing: {}", exc
        )
        return None


async def _is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Is ``user_id`` currently in ``chat_id``? Fails closed via ``_member_of``.

    The confirm step wants only the verdict; the target step reads the
    member object itself, so the shared fetch lives one level down.
    """
    member = await _member_of(bot, chat_id, user_id)
    return member is not None and member.status not in _ABSENT_STATUSES


class _CooldownTable:
    """``group_id -> monotonic deadline``, self-bounding.

    #73-fp growth class. Dropping the expired entry on read only
    reclaims the groups somebody asks about again — a group transferred
    once and never touched afterwards kept its day-stale float forever,
    so the table grew with every distinct group ever transferred. A full
    sweep on insert fixes that, but running it on every insert is O(n)
    per call while nothing has expired, so it fires only once the table
    passes a watermark, and the watermark is then re-set to twice the
    surviving size: amortised O(1) per insert, and the table can never
    hold more than roughly twice the groups transferred inside one
    cooldown window.

    Deliberately NOT a
    :class:`~telegram_invite_bot.utils.ttl_lru_cache.TTLLRUCache` like
    the other bounded tables. This one is a rate limit, and LRU eviction
    under pressure would hand back exactly the early transfer it exists
    to refuse — a bypass anybody able to make the bot join enough groups
    could drive. Bounding it by *time* keeps the limit intact; bounding
    it by *count* would not.

    In-process, like ``rank_self._SYNC_CACHE``: a restart forgets every
    running window, which lets one extra transfer through per group and
    is the same trade-off the rest of the module already takes.
    """

    def __init__(self, ttl: float, *, sweep_at_min: int = _SWEEP_WATERMARK_MIN) -> None:
        self._ttl = ttl
        self._sweep_at_min = sweep_at_min
        self._sweep_at = sweep_at_min
        self._deadlines: dict[int, float] = {}

    def __len__(self) -> int:
        return len(self._deadlines)

    @property
    def sweep_at(self) -> int:
        """Size at which the next insert sweeps (pinned by tests)."""
        return self._sweep_at

    def blocked(self, group_id: int, now: float) -> bool:
        """True iff ``group_id``'s window is still running.

        The queried entry is dropped when it has elapsed; everything
        else is reclaimed by :meth:`mark`'s sweep.
        """
        deadline = self._deadlines.get(group_id)
        if deadline is None:
            return False
        if deadline <= now:
            del self._deadlines[group_id]
            return False
        return True

    def mark(self, group_id: int, now: float) -> None:
        """Start ``group_id``'s window, sweeping first if it's time."""
        if len(self._deadlines) >= self._sweep_at:
            for gid in [gid for gid, dl in self._deadlines.items() if dl <= now]:
                del self._deadlines[gid]
            self._sweep_at = max(self._sweep_at_min, 2 * len(self._deadlines))
        self._deadlines[group_id] = now + self._ttl

    def clear(self) -> None:
        self._deadlines.clear()
        self._sweep_at = self._sweep_at_min


_COOLDOWNS = _CooldownTable(TRANSFER_COOLDOWN_SECONDS)

# Same fetch cap as /mygroups — bounds the picker query, pagination
# keeps pages readable.
_MAX_GROUPS = 200

_NAME_TRUNC = 40


def clear_transfer_cooldowns() -> None:
    """Test-isolation hook (same contract as ``clear_rank_caches``)."""
    _COOLDOWNS.clear()


def _on_cooldown(group_id: int, *, now: float | None = None) -> bool:
    """True iff ``group_id`` was transferred within the last 24h."""
    return _COOLDOWNS.blocked(group_id, time.monotonic() if now is None else now)


def _mark_transferred(group_id: int, *, now: float | None = None) -> None:
    _COOLDOWNS.mark(group_id, time.monotonic() if now is None else now)


class TransferStates(StatesGroup):
    """The two text/confirm steps after the picker callback."""

    awaiting_target = State()
    awaiting_confirm = State()


# FSM data fields. Strings (not an Enum) — aiogram FSM data is a plain
# dict and these never leave this module.
_FD_LANG = "lang"
_FD_GROUP_ID = "group_id"
_FD_TITLE = "title"
_FD_OWNER_ID = "owner_id"
_FD_TARGET_ID = "target_id"
_FD_TARGET_LABEL = "target_label"


def _page_slice(
    rows: list[tuple[int, str | None]], page: int
) -> tuple[list[tuple[int, str | None]], int]:
    """Clamp ``page`` into range and return (visible rows, clamped page)."""
    pages = total_pages(len(rows))
    page = min(max(1, page), pages)
    start = (page - 1) * PAGE_SIZE
    return rows[start : start + PAGE_SIZE], page


def _render_picker(
    lang: str, *, rows: list[tuple[int, str | None]], page: int, owner_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Picker page text + keyboard. ``rows`` is the FULL result set."""
    visible, page = _page_slice(rows, page)
    lines = [t("h_trights_title", lang), ""]
    for chat_id, title in visible:
        safe_title = html.escape((title or "—")[:_NAME_TRUNC])
        lines.append(f"• <code>{chat_id}</code> — {safe_title}")
    lines.extend(["", t("h_trights_pick_hint", lang)])
    markup = build_pick_markup(lang, rows=visible, page=page, total=len(rows), owner_id=owner_id)
    return "\n".join(lines), markup


async def _edit_card(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Best-effort edit of the flow card (drops its keyboard unless a
    replacement is passed). Swallows the usual edit races — "message is
    not modified" / deleted-card — same as every other card flow here.

    Via the shared :func:`edit_card`, so the swallow stays limited to
    those races: the old ``suppress(TelegramAPIError)`` here also ate
    malformed-HTML rejects, which are bugs in our own copy and used to
    disappear without a log line on a card that carries group names.
    """
    msg = callback.message
    if not isinstance(msg, MessageType):
        return
    await edit_card(msg, text, reply_markup=reply_markup)


def _target_label(user_id: int, username: str | None, first_name: str | None) -> str:
    """Display label for the confirm card / success copy (HTML-safe)."""
    if username:
        return f"@{html.escape(username)} (<code>{user_id}</code>)"
    if first_name:
        return f"{html.escape(first_name)} (<code>{user_id}</code>)"
    return f"<code>{user_id}</code>"


async def handle_transfer_rights(
    message: Message, registry: EngineRegistry, lang: str, state: FSMContext
) -> None:
    """``/transfer_rights`` entry — render the caller's group picker.

    Guards against a leftover flow the same way ``/check_create`` does:
    point at ``/cancel`` instead of stranding a previous card.
    """
    user = message.from_user
    if user is None:  # F.from_user-filtered; kept for direct calls/mypy.
        return
    if await state.get_state() is not None:
        await message.reply(t("h_trights_busy", lang))
        return
    async with session_for(registry, DBName.USERS) as session:
        rows = await BotGroupsRepo(session).list_owned(user.id, limit=_MAX_GROUPS)
    if not rows:
        await message.reply(t("h_trights_empty", lang))
        log.bind(user_id=user.id).info("/transfer_rights rendered (empty)")
        return
    text, markup = _render_picker(lang, rows=rows, page=1, owner_id=user.id)
    await message.reply(text, reply_markup=markup)
    log.bind(user_id=user.id, count=len(rows)).info("/transfer_rights picker rendered")


async def handle_transfer_page(
    callback: CallbackQuery,
    callback_data: TransferPage,
    registry: EngineRegistry,
    lang: str,
) -> None:
    """Picker pagination — re-scoped to the tapping user every time."""
    async with session_for(registry, DBName.USERS) as session:
        rows = await BotGroupsRepo(session).list_owned(callback.from_user.id, limit=_MAX_GROUPS)
    if not rows:
        await callback.answer(t("h_trights_not_found", lang), show_alert=True)
        return
    text, markup = _render_picker(
        lang, rows=rows, page=callback_data.page, owner_id=callback.from_user.id
    )
    await _edit_card(callback, text, reply_markup=markup)
    await callback.answer()


async def handle_transfer_pick(
    callback: CallbackQuery,
    callback_data: TransferPick,
    registry: EngineRegistry,
    lang: str,
    state: FSMContext,
) -> None:
    """Group picked — verify ownership + cooldown, then ask for the target.

    Ownership comes from the DB (not the payload), so a forged
    ``group_id`` answers with the same "not found" alert whether the
    group is unknown or someone else's.
    """
    async with session_for(registry, DBName.USERS) as session:
        owned = await BotGroupsRepo(session).get_owned(
            callback_data.group_id, callback.from_user.id
        )
    if owned is None:
        await callback.answer(t("h_trights_not_found", lang), show_alert=True)
        return
    chat_id, title = owned
    if _on_cooldown(chat_id):
        await callback.answer(t("h_trights_rate_limited", lang), show_alert=True)
        return
    await state.set_state(TransferStates.awaiting_target)
    await state.set_data(
        {
            _FD_LANG: lang,
            _FD_GROUP_ID: chat_id,
            _FD_TITLE: title or "—",
            _FD_OWNER_ID: callback.from_user.id,
            # The sweeper reads the deadline off the FSM data, not the
            # storage record — an unstamped state is skipped forever
            # (fsm_sweeper.sweep_once), so the rule registered in
            # ``_transfer_rights_timeout_rules`` would never fire and the
            # busy-gate below would never clear.
            STATE_ENTERED_AT_FIELD: utc_now_iso(),
        }
    )
    await _edit_card(
        callback,
        t(
            "h_trights_ask_target",
            lang,
            title=html.escape((title or "—")[:_NAME_TRUNC]),
            chat_id=chat_id,
        ),
    )
    await callback.answer()
    log.bind(user_id=callback.from_user.id, group_id=chat_id).info("/transfer_rights group picked")


async def handle_transfer_target(
    message: Message, registry: EngineRegistry, state: FSMContext, bot: Bot
) -> None:
    """Target step — resolve ``@username`` / numeric id against users.db.

    Stays in ``awaiting_target`` on a miss (legacy's "must have written
    to the bot at least once" rule, bot.py:41650-41652). Self-transfer
    rejected (bot.py:41653-41655), bot targets too (bot.py:41656-41658):
    resolving against ``users`` is not proof the target is human — the
    anonymous-admin sender id is a bot id like any other — so the
    ``is_bot`` flag on the membership answer is what settles it.

    #700: last comes the membership gate (bot.py:41659-41661), aimed at
    the group being transferred rather than legacy's single main group.
    It is deliberately the last check — it costs a Telegram round-trip,
    and the two cheap local rejections above it are the common ones.
    The bot check rides along on that same answer, in legacy's order.
    """
    user = message.from_user
    if user is None:
        return
    data = await state.get_data()
    lang = str(data.get(_FD_LANG) or "ru")
    if int(data.get(_FD_OWNER_ID) or 0) != user.id:
        # FSM keys are (chat, user) in DM so this shouldn't trigger;
        # belt-and-braces against storage-key surprises.
        await state.clear()
        await message.reply(t("h_trights_expired", lang))
        return
    token = (message.text or "").split(maxsplit=1)[0].strip() if message.text else ""
    target = None
    if token:
        async with session_for(registry, DBName.USERS) as session:
            repo = UsersRepo(session)
            target_id = parse_int_token(token, signed=True)
            if target_id is not None and target_id > 0:
                target = await repo.get(target_id)
            else:
                target = await repo.get_by_username(token)
    if target is None:
        await message.reply(t("h_trights_bad_target", lang))
        return
    if target.user_id == user.id:
        await message.reply(t("h_trights_self", lang))
        return
    group_id = int(data.get(_FD_GROUP_ID) or 0)
    title = html.escape(str(data.get(_FD_TITLE) or "—")[:_NAME_TRUNC])
    member = await _member_of(bot, group_id, target.user_id)
    # Both rejections stay in ``awaiting_target`` like every other target
    # rejection, so the owner can name somebody else without restarting.
    if member is not None and member.user.is_bot:
        await message.reply(t("h_trights_bot_target", lang))
        return
    if member is None or member.status in _ABSENT_STATUSES:
        await message.reply(t("h_trights_not_in_group", lang, title=title))
        return
    label = _target_label(target.user_id, target.username, target.first_name)
    # Re-stamp on the step change so ``awaiting_confirm`` gets its own
    # full budget rather than inheriting the target step's elapsed time.
    await state.update_data(
        {
            _FD_TARGET_ID: target.user_id,
            _FD_TARGET_LABEL: label,
            STATE_ENTERED_AT_FIELD: utc_now_iso(),
        }
    )
    await state.set_state(TransferStates.awaiting_confirm)
    await message.reply(
        t(
            "h_trights_confirm",
            lang,
            title=title,
            chat_id=group_id,
            target=label,
        ),
        reply_markup=build_confirm_markup(lang, owner_id=user.id),
    )


async def handle_transfer_confirm(
    callback: CallbackQuery,
    callback_data: TransferDecision,
    registry: EngineRegistry,
    bot: Bot,
    lang: str,
    state: FSMContext,
) -> None:
    """✅ Confirm — guarded UPDATE + cooldown mark + best-effort DM.

    The repo's WHERE guard re-derives ownership at write time, so a
    transfer that raced (legacy re-attributed the row, or a second
    confirm card) updates zero rows and surfaces "not found" instead of
    stealing the attribution.
    """
    if callback.from_user.id != callback_data.owner_id:
        await callback.answer(t("h_trights_foreign", lang), show_alert=True)
        return
    data = await state.get_data()
    flow_lang = str(data.get(_FD_LANG) or lang)
    group_id = int(data.get(_FD_GROUP_ID) or 0)
    target_id = int(data.get(_FD_TARGET_ID) or 0)
    if not group_id or not target_id:
        await state.clear()
        await _edit_card(callback, t("h_trights_expired", flow_lang))
        await callback.answer()
        return
    # Single-shot card: clear before the write so a stale re-click can't
    # re-run the flow (the data it needs is consumed here).
    await state.clear()
    if _on_cooldown(group_id):
        await _edit_card(callback, t("h_trights_rate_limited", flow_lang))
        await callback.answer()
        return
    title = html.escape(str(data.get(_FD_TITLE) or "—")[:_NAME_TRUNC])
    # #700: re-checked here and not only at the target step, because the
    # target can leave between the two (legacy re-checked at exactly this
    # point too, bot.py:41722-41724). Terminal like the cooldown refusal
    # above — the state is already cleared, so the owner restarts.
    if not await _is_member(bot, group_id, target_id):
        await _edit_card(callback, t("h_trights_not_in_group", flow_lang, title=title))
        await callback.answer()
        return
    async with session_for(registry, DBName.USERS) as session:
        moved = await BotGroupsRepo(session).transfer_ownership(
            group_id, from_user_id=callback.from_user.id, to_user_id=target_id
        )
    if not moved:
        await _edit_card(callback, t("h_trights_not_found", flow_lang))
        await callback.answer()
        return
    _mark_transferred(group_id)
    label = str(data.get(_FD_TARGET_LABEL) or f"<code>{target_id}</code>")
    await _edit_card(callback, t("h_trights_done", flow_lang, title=title, target=label))
    await callback.answer()
    # Best-effort DM to the new owner (legacy bot.py:41745-41753). The
    # recipient's language is unknown here — use the flow language, the
    # same trade legacy made with its hardcoded RU copy.
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(
            target_id,
            t("h_trights_notify", flow_lang, title=title, chat_id=group_id),
        )
    log.bind(user_id=callback.from_user.id, group_id=group_id, target_id=target_id).info(
        "/transfer_rights ownership transferred"
    )


async def handle_transfer_cancel(
    callback: CallbackQuery,
    callback_data: TransferDecision,
    lang: str,
    state: FSMContext,
) -> None:
    """❌ Cancel — clear the flow at any step; nothing was written."""
    if callback.from_user.id != callback_data.owner_id:
        await callback.answer(t("h_trights_foreign", lang), show_alert=True)
        return
    data = await state.get_data()
    flow_lang = str(data.get(_FD_LANG) or lang)
    await state.clear()
    await _edit_card(callback, t("h_trights_cancelled", flow_lang))
    await callback.answer()
    log.bind(user_id=callback.from_user.id).info("/transfer_rights cancelled")


def build_router(registry: EngineRegistry) -> Router:
    """Private-only on both event types — the command is DM-only in
    legacy (bot.py:41615) and every callback originates from a card this
    router sent in DM. A group ``/transfer_rights`` does **not** fall
    through to UNHANDLED: the ``with_chat_type_refusal`` wrapper below
    answers it with the localized ``h_private_only_command`` card and
    its deep-link button. The refusal fires because ``transfer_rights``
    is not in ``chat_scope._TWO_SIDED_COMMANDS`` and its catalog rank is
    0 (its ``core.ranks.COMMAND_ENTRIES`` row), below
    ``_SILENT_FROM_RANK``.

    The target text-step is registered with ``F.text`` only — ``/cancel``
    still wins because the cancel router is included before this one in
    ``main_router`` (the documented escape-hatch ordering).
    """
    router = Router(name="transfer_rights")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message, lang: str, state: FSMContext) -> None:
        await handle_transfer_rights(message, registry, lang, state)

    async def _page(callback: CallbackQuery, callback_data: TransferPage, lang: str) -> None:
        await handle_transfer_page(callback, callback_data, registry, lang)

    async def _pick(
        callback: CallbackQuery,
        callback_data: TransferPick,
        lang: str,
        state: FSMContext,
    ) -> None:
        await handle_transfer_pick(callback, callback_data, registry, lang, state)

    async def _target(message: Message, state: FSMContext, bot: Bot) -> None:
        await handle_transfer_target(message, registry, state, bot)

    async def _confirm(
        callback: CallbackQuery,
        callback_data: TransferDecision,
        bot: Bot,
        lang: str,
        state: FSMContext,
    ) -> None:
        await handle_transfer_confirm(callback, callback_data, registry, bot, lang, state)

    async def _cancel(
        callback: CallbackQuery,
        callback_data: TransferDecision,
        lang: str,
        state: FSMContext,
    ) -> None:
        await handle_transfer_cancel(callback, callback_data, lang, state)

    router.message.register(
        _entry,
        Command("transfer_rights", "giverights", "передать_права", ignore_case=True),
        F.from_user,
    )
    router.callback_query.register(_page, TransferPage.filter(), F.from_user)
    router.callback_query.register(_pick, TransferPick.filter(), F.from_user)
    router.message.register(
        _target,
        StateFilter(TransferStates.awaiting_target),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    register_text_expected(router, TransferStates.awaiting_target)
    # Confirm only fires from the confirm card's state; cancel works at
    # ANY step (the picker's cancel row predates the FSM being set).
    router.callback_query.register(
        _confirm,
        TransferDecision.filter(F.ok),
        StateFilter(TransferStates.awaiting_confirm),
        F.from_user,
    )
    router.callback_query.register(
        _cancel,
        TransferDecision.filter(~F.ok),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="private")
