"""``/accept`` + ``/decline`` standalone game-challenge commands — L-02.

Legacy had ``/accept`` / ``/decline`` slash commands so a player could
answer a pending rock-paper-scissors challenge without tapping the
inline button (``command_aliases.py:125-135`` → ``cmd_accept`` /
``cmd_decline``, implemented at ``rock_paper_scissors.py:397`` over the
in-memory ``CPCManager``). The new pipeline made both the /cpc and the
/duel challenge flows callback-only; this module restores the slash
surface for BOTH games on top of the FSM the callbacks already use.

Resolution algorithm
--------------------
1. The caller types ``/accept`` (or ``/принять``) in a GROUP chat.
2. Scan the FSM storage for sessions whose :class:`StorageKey` lives in
   THIS chat (``key.chat_id == message.chat.id`` — both the group-form
   /cpc and /duel key their Variant A session on the chat the
   challenger typed the command in), whose state is one of the two
   ``awaiting_acceptance`` states, and whose ``opponent_id`` is the
   caller. Those are the caller's pending INCOMING challenges here.
3. Nothing found → localized "nothing to accept/decline" reply.
4. More than one found (e.g. one rps + one duel) → prefer the MOST
   RECENT by the ``state_entered_at`` stamp every set_state in the
   rps/duel handlers writes (sweeper deadline field). Rationale: the
   newest card is the one on the caller's screen — answering the
   freshest challenge matches what a button tap would most likely hit.
   Note the shared accept cores carry the M-G-2 opponent-busy guard,
   which counts the OTHER pending challenge as "busy" — so with two
   pending, /accept is rejected with the localized busy toast exactly
   like the button would be; /decline (no busy guard) clears the
   freshest and unblocks accepting the older one next.
5. Dispatch into the SAME shared core the inline buttons use
   (:func:`accept_rps_challenge` / :func:`decline_rps_challenge` /
   :func:`accept_duel_challenge` / :func:`decline_duel_challenge`) —
   the command path only swaps the UI closures (group reply instead of
   callback toast; edit-by-stored-message-id instead of
   edit-the-clicked-card).

Why no M-G-1 bet guard here: the tamper guard compares a wire-carried
payload bet against the FSM bet; a slash command carries no payload, so
the cores are called with ``payload_bet=None`` and the FSM bet is the
single source of truth.

Middleware: ``EconomyMiddleware`` on the message observer. The FSM
work itself (the busy-guard scan, the state flip) still touches no DB,
but #1664 made ACCEPTING a challenge spend a slot of the shared
per-user game budget, and that budget lives in ``economy.game_plays``.
Without the middleware here, ``/accept`` would be a one-word bypass of
the very gate the inline button enforces. ``/decline`` costs nothing
and is threaded only because it shares :func:`_dispatch`.
``fsm_storage`` is injected by aiogram's dispatcher data.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPE_NAMES
from telegram_invite_bot.fsm.duel import DuelStates
from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.handlers.duel import (
    accept_duel_challenge,
    decline_duel_challenge,
)
from telegram_invite_bot.handlers.group_only import handle_group_only
from telegram_invite_bot.handlers.rps import (
    accept_rps_challenge,
    decline_rps_challenge,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.scheduler.fsm_busy import _iter_keys
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD
from telegram_invite_bot.utils.aiogram import require_from_user

log = logger.bind(component="handlers.challenge_commands")

if TYPE_CHECKING:
    from aiogram.fsm.storage.base import BaseStorage, StorageKey
    from aiogram.types import InlineKeyboardMarkup, Message

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.services.game_limit_service import GameLimitService

GameKind = Literal["rps", "duel"]

# awaiting_acceptance state name → which game's core to dispatch into.
# Only the PRE-accept stage qualifies — a match already in
# awaiting_moves / awaiting_rolls cannot be accepted or declined via
# slash command (same as the buttons: the accept/decline keyboard is
# gone by then).
_RPS_ACCEPT_STATE = RpsStates.awaiting_acceptance.state
_DUEL_ACCEPT_STATE = DuelStates.awaiting_acceptance.state
# aiogram types ``State.state`` as ``str | None`` (None only for the
# special ``State(state="*")`` wildcard); both concrete states always
# carry a name — the assert narrows for mypy and would trip loudly at
# import time if aiogram's contract ever changed.
assert _RPS_ACCEPT_STATE is not None and _DUEL_ACCEPT_STATE is not None
_ACCEPTANCE_STATES: dict[str, GameKind] = {
    _RPS_ACCEPT_STATE: "rps",
    _DUEL_ACCEPT_STATE: "duel",
}

# ``state_entered_at`` stamps are tz-aware UTC ISO strings
# (``fsm_sweeper.utc_now_iso``). Sessions missing/garbling the stamp
# sort as oldest — a fresh, well-formed challenge always wins the
# "most recent" pick.
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _PendingChallenge:
    """One incoming ``awaiting_acceptance`` match found by the scan."""

    kind: GameKind
    challenger_id: int
    data: dict[str, object]
    entered_at: datetime


def _parse_entered_at(raw: object) -> datetime:
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                return parsed
    return _EPOCH


async def _prefetch_acceptance(
    storage: BaseStorage,
) -> dict[StorageKey, tuple[str, dict[str, Any]]] | None:
    """Every ``awaiting_acceptance`` record in ONE round trip, or ``None``.

    #1684. The states this command cares about are the two keys of
    :data:`_ACCEPTANCE_STATES`: fixed at import, never user input. A
    backend exposing ``iter_records(states)`` filters on them in SQL
    and hands back ``(key, state, data)`` triples, collapsing the
    ``1 + N + M`` reads :func:`_find_pending_challenges` used to make
    into one statement. Under ``FSM_BACKEND=sqlite`` those reads all
    queued behind the single connection
    :class:`fsm.sqlite_storage.SQLiteStorage` holds for the life of
    the process — the same connection every routed update's own
    ``get_state`` goes through — and their count tracked the size of
    the whole store rather than the number of live challenges.

    ``None`` means the backend has no such method and the caller keeps
    the key-by-key walk. Returning ``None`` rather than ``[]`` matters
    for the same reason it does in
    :func:`scheduler.fsm_busy._prefetch_busy`: an empty mapping is a
    legitimate answer meaning "nothing pending", so the two cases must
    stay distinguishable.

    Duck-typed on the method, like :func:`_iter_keys` itself, so a
    future Redis or Postgres backend that adopts the convention gets
    the fast path here, in the sweeper and in the busy guard at once.
    """
    iter_records = getattr(storage, "iter_records", None)
    if iter_records is None:
        return None
    records = await iter_records(tuple(_ACCEPTANCE_STATES))
    return {key: (state, data) for key, state, data in records}


async def _find_pending_challenges(
    storage: BaseStorage, *, bot_id: int, chat_id: int, user_id: int
) -> list[_PendingChallenge]:
    """Scan FSM storage for the caller's pending incoming challenges.

    Same duck-typed iteration as the sweeper / user-busy guard
    (``iter_keys()`` if the backend exposes it, ``MemoryStorage``
    internals otherwise; unknown backends yield nothing — the command
    then degrades to "nothing to accept", mirroring how the busy guard
    degrades to a no-op).

    #1684. A backend that also offers ``iter_records(states)`` answers
    the state half of the question in ONE statement
    (:func:`_prefetch_acceptance`), which is the same treatment #1451
    gave the sweeper and #1491 gave the busy guard — the three scanners
    were literally the same loop, and leaving one of them on the old
    shape would have made the next reader believe the walk was
    deliberate here. Only the STATE filter moves into SQL: an FSM key
    is stored as one ``|``-joined TEXT column, so ``bot_id`` /
    ``chat_id`` and the ``opponent_id`` payload check stay in Python
    on both paths.

    Portable path unchanged and still O(N) over the keys the backend
    reports, which is what :class:`MemoryStorage` gets — there every
    read is a dict lookup and the round-trip count is not a cost.
    """
    prefetched = await _prefetch_acceptance(storage)
    keys = list(prefetched) if prefetched is not None else await _iter_keys(storage)
    found: list[_PendingChallenge] = []
    for key in keys:
        if key.bot_id != bot_id or key.chat_id != chat_id:
            continue
        if prefetched is None:
            state_name = await storage.get_state(key)
            if state_name is None:
                continue
            kind = _ACCEPTANCE_STATES.get(state_name)
            if kind is None:
                continue
            data: dict[str, Any] = await storage.get_data(key)
        else:
            state_name, data = prefetched[key]
            kind = _ACCEPTANCE_STATES[state_name]
        if data.get("opponent_id") != user_id:
            continue
        found.append(
            _PendingChallenge(
                kind=kind,
                challenger_id=key.user_id,
                data=dict(data),
                entered_at=_parse_entered_at(data.get(STATE_ENTERED_AT_FIELD)),
            )
        )
    return found


def _most_recent(pending: list[_PendingChallenge]) -> _PendingChallenge:
    """Pick the freshest challenge (see module docstring step 4)."""
    return max(pending, key=lambda p: p.entered_at)


async def _edit_rps_card(
    *,
    bot: Bot,
    acceptor_id: int,
    card_message_id: object,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> int | None:
    """Command-path EditCardFn for RPS — the challenge card lives in
    the acceptor's PRIVATE chat (the /cpc flow DMs the opponent), so
    edit it there by the ``opponent_accept_message_id`` the /cpc
    handler stashed; fall back to a fresh PM when the edit fails or
    the id was never recorded. ``None`` only when even the fresh send
    is forbidden (acceptor blocked the bot mid-flow) — same posture as
    the callback path's unreachable-message branch.
    """
    if isinstance(card_message_id, int):
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=acceptor_id,
                message_id=card_message_id,
                reply_markup=keyboard,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            log.bind(acceptor_id=acceptor_id, message_id=card_message_id).debug(
                "rps card edit failed; falling back to fresh send"
            )
        else:
            return card_message_id
    try:
        fresh = await bot.send_message(acceptor_id, text, reply_markup=keyboard)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # #306: both shapes, for the same reason as #305. ``/accept`` can
        # be typed in a group, so reaching this fallback does not prove
        # the acceptor ever opened a PM with the bot — and if they never
        # did, Telegram answers ``chat not found``, not ``Forbidden``.
        log.bind(acceptor_id=acceptor_id, error=str(exc)).warning(
            "/accept: rps card undeliverable; match continues without card"
        )
        return None
    return fresh.message_id


async def _edit_duel_card(
    *,
    message: Message,
    card_message_id: object,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> int | None:
    """Command-path EditCardFn for duel — the challenge card lives in
    THIS group chat (``challenge_message_id``); edit it in place, or
    post a fresh card to the group when the edit fails so the roll
    keyboard still surfaces.
    """
    bot = message.bot
    if bot is not None and isinstance(card_message_id, int):
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=card_message_id,
                reply_markup=keyboard,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            log.bind(chat_id=message.chat.id, message_id=card_message_id).debug(
                "duel card edit failed; falling back to fresh send"
            )
        else:
            return card_message_id
    fresh = await message.answer(text, reply_markup=keyboard)
    return fresh.message_id


async def _dispatch(
    *,
    message: Message,
    bot: Bot,
    fsm_storage: BaseStorage,
    game_limit_service: GameLimitService,
    lang: str,
    action: Literal["accept", "decline"],
    checkpoint: Checkpoint | None = None,
) -> None:
    """Shared body of /accept and /decline (they differ only in which
    core gets invoked and which i18n keys render)."""
    tg_user = require_from_user(message)
    pending = await _find_pending_challenges(
        fsm_storage,
        bot_id=bot.id,
        chat_id=message.chat.id,
        user_id=tg_user.id,
    )
    if not pending:
        key = (
            "h_challenge_nothing_to_accept"
            if action == "accept"
            else "h_challenge_nothing_to_decline"
        )
        await message.answer(t(key, lang))
        return

    chosen = _most_recent(pending)
    if len(pending) > 1:
        log.bind(
            uid=tg_user.id,
            chat_id=message.chat.id,
            candidates=[(p.kind, p.challenger_id) for p in pending],
            chosen=(chosen.kind, chosen.challenger_id),
        ).info("/{action}: multiple pending challenges; picked most recent", action=action)

    async def _reject(text: str) -> None:
        await message.answer(text)

    async def _ack() -> None:
        key = "h_challenge_accept_ok" if action == "accept" else "h_challenge_decline_ok"
        await message.answer(t(key, lang))

    if chosen.kind == "rps":
        rps_card_id = chosen.data.get("opponent_accept_message_id")

        async def _edit_rps(text: str, keyboard: InlineKeyboardMarkup | None) -> int | None:
            return await _edit_rps_card(
                bot=bot,
                acceptor_id=tg_user.id,
                card_message_id=rps_card_id,
                text=text,
                keyboard=keyboard,
            )

        if action == "accept":
            await accept_rps_challenge(
                bot=bot,
                fsm_storage=fsm_storage,
                lang=lang,
                chat_id=message.chat.id,
                challenger_id=chosen.challenger_id,
                acceptor_id=tg_user.id,
                payload_bet=None,
                game_limit_service=game_limit_service,
                reject=_reject,
                ack=_ack,
                edit_opponent_card=_edit_rps,
                checkpoint=checkpoint,
            )
        else:
            await decline_rps_challenge(
                bot=bot,
                fsm_storage=fsm_storage,
                lang=lang,
                chat_id=message.chat.id,
                challenger_id=chosen.challenger_id,
                decliner_id=tg_user.id,
                reject=_reject,
                ack=_ack,
                edit_opponent_card=_edit_rps,
            )
        return

    duel_card_id = chosen.data.get("challenge_message_id")

    async def _edit_duel(text: str, keyboard: InlineKeyboardMarkup | None) -> int | None:
        return await _edit_duel_card(
            message=message,
            card_message_id=duel_card_id,
            text=text,
            keyboard=keyboard,
        )

    if action == "accept":
        await accept_duel_challenge(
            bot=bot,
            fsm_storage=fsm_storage,
            lang=lang,
            chat_id=message.chat.id,
            challenger_id=chosen.challenger_id,
            acceptor_id=tg_user.id,
            payload_bet=None,
            game_limit_service=game_limit_service,
            reject=_reject,
            ack=_ack,
            edit_card=_edit_duel,
            checkpoint=checkpoint,
        )
    else:
        await decline_duel_challenge(
            bot=bot,
            fsm_storage=fsm_storage,
            lang=lang,
            chat_id=message.chat.id,
            challenger_id=chosen.challenger_id,
            decliner_id=tg_user.id,
            reject=_reject,
            ack=_ack,
            edit_card=_edit_duel,
        )


async def handle_accept(
    message: Message,
    bot: Bot,
    fsm_storage: BaseStorage,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/accept`` — accept the caller's pending incoming challenge."""
    await _dispatch(
        message=message,
        bot=bot,
        fsm_storage=fsm_storage,
        game_limit_service=game_limit_service,
        lang=lang,
        action="accept",
        checkpoint=checkpoint,
    )


async def handle_decline(
    message: Message,
    bot: Bot,
    fsm_storage: BaseStorage,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/decline`` — decline the caller's pending incoming challenge.

    Declining spends nothing; the two limiter parameters are here only
    because :func:`_dispatch` is shared with the accept half.
    """
    await _dispatch(
        message=message,
        bot=bot,
        fsm_storage=fsm_storage,
        game_limit_service=game_limit_service,
        lang=lang,
        action="decline",
        checkpoint=checkpoint,
    )


def build_router(registry: EngineRegistry) -> Router:
    """Factory — fresh router per call, parity with sibling factories.

    ``EconomyMiddleware`` on the message observer, so the accept half
    sees ``game_limit_service`` and ``checkpoint``: #1664 charges the
    acceptor a slot of the shared per-user game budget, and a router
    with no economy session would have made ``/accept`` a free
    alternative to the button (see module docstring). The private-chat
    twins below take no injected kwargs, so the middleware costs them
    a session and nothing else.

    Group-only: the challenges these commands answer are keyed on the
    group chat (group-form /cpc and /duel). Legacy listed /accept as
    private+group, but the new pipeline's private RPS flow keys its
    FSM on the chat the CHALLENGER typed /cpc in — a private /accept
    by the opponent could never find it, so the private surface stays
    on the inline buttons.

    Which is a rule the opponent has no way of knowing, so each pair
    below gets a private-chat twin that says it (#122). Before that,
    an ``/accept`` in a DM matched no handler and answered with
    nothing at all — indistinguishable from a broken bot.
    """
    router = Router(name="challenge_commands")
    router.message.middleware(EconomyMiddleware(registry))
    router.message.register(
        handle_accept,
        Command("accept", "принять", ignore_case=True),
        F.from_user,
        F.chat.type.in_(GROUP_TYPE_NAMES),
    )
    router.message.register(
        handle_group_only,
        Command("accept", "принять", ignore_case=True),
        F.from_user,
        F.chat.type == ChatType.PRIVATE,
    )
    router.message.register(
        handle_decline,
        Command("decline", "отклонить", "reject", ignore_case=True),
        F.from_user,
        F.chat.type.in_(GROUP_TYPE_NAMES),
    )
    router.message.register(
        handle_group_only,
        Command("decline", "отклонить", "reject", ignore_case=True),
        F.from_user,
        F.chat.type == ChatType.PRIVATE,
    )
    return router
