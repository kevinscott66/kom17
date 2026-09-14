"""``/pvp_coin`` + ``/pvp_dice`` — PvP escrow stake games (AUD-2).

Ported from legacy ``cmd_pvp_coin`` / ``cmd_pvp_dice``
(bot.py:20867-20948). The legacy DM-create → group-picker indirection is
collapsed to the in-group challenge-card pattern already used by
``/duel``: the creator runs the command in a group, their stake is
escrowed immediately, and an Accept card is published to that group. An
opponent taps Accept → the game resolves atomically (the pot is 2×bet,
of which the winner collects 1.9×bet and the rest is burned as the house
cut — T-020/R8; a dice tie refunds both). The creator can Cancel a still-pending
offer to reclaim the hold; the economy-cleanup sweep expires stale
offers and refunds them.

All money composition (escrow, payout, refund, ledger, atomicity) lives
in :class:`PvpService`; this module is the Telegram surface only.
"""

from __future__ import annotations

import contextlib
import html
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from telegram_invite_bot.core.callback_fields import DbInt
from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.games.limits import MAX_BET, MIN_BET, PLAY_LOCKS
from telegram_invite_bot.games.pot import split_pot
from telegram_invite_bot.games.pvp import CREATOR, CoinSide, normalize_side
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.game_cards import render_abuse_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.pvp_service import (
    PvpAcceptOutcome,
    PvpCreateOutcome,
)
from telegram_invite_bot.utils.aiogram import (
    BENIGN_EDIT_REJECTS,
    command_body,
    edit_card,
    mention_html,
    require_from_user,
)
from telegram_invite_bot.utils.html import legacy_md_to_html
from telegram_invite_bot.utils.numbers import format_number, parse_int_token
from telegram_invite_bot.utils.time import db_now

if TYPE_CHECKING:
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.services.game_limit_service import GameLimitService
    from telegram_invite_bot.services.pvp_service import ExpiredOffer, PvpService

log = logger.bind(component="handlers.pvp_stake")


class PvpAccept(CallbackData, prefix="pvp_acc"):
    """Accept the challenge with this id.

    Carries the offer id and NOTHING else on purpose. The chat is
    not on the wire: the handler reads it from
    ``callback.message.chat.id``, which Telegram sets, and the
    service checks it against the chat the offer was published in
    (#1604). Putting the chat in the payload would let the tapper
    choose it, and would invalidate every card already sitting in a
    chat. Same posture as ``GroupAdminRefresh`` and the
    ``voice_settings`` builders.

    The tapper is likewise not on the wire — this offer is open to
    anyone in the chat, and the only person who may NOT take it (the
    creator) is rejected by the service against
    ``callback.from_user.id``.
    """

    offer_id: DbInt


class PvpCancel(CallbackData, prefix="pvp_cxl"):
    """Withdraw the challenge with this id.

    Also id-only. Authorization is a server-side gate, not a wire
    field: ``PvpService.cancel`` takes ``creator_id`` from
    ``callback.from_user.id`` and the UPDATE matches on it, so a
    stranger tapping Cancel on a forwarded card changes nothing and
    is told so. The chat is not checked here because cancelling
    only ever refunds the creator their own stake.
    """

    offer_id: DbInt


def _name(user: object) -> str:
    """HTML-escaped display name for a Telegram user-ish object."""
    first = getattr(user, "first_name", None) or "?"
    return html.escape(str(first))


def _offer_keyboard(offer_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_pvp_btn_accept", lang),
                    callback_data=PvpAccept(offer_id=offer_id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_pvp_btn_cancel", lang),
                    callback_data=PvpCancel(offer_id=offer_id).pack(),
                ),
            ]
        ]
    )


def _parse_bet(token: str | None) -> int | None:
    """The raw bet, sign included — the service is what refuses ``<= 0``.

    A negative bet is parsed rather than rejected here on purpose: it
    reaches :class:`PvpService` and comes back as ``INVALID_BET``, which
    is the same answer with one place deciding it.
    """
    if token is None:
        return None
    return parse_int_token(token, signed=True)


async def _handle_create(
    message: Message,
    *,
    game: str,
    side: CoinSide | None,
    bet: int | None,
    pvp_service: PvpService,
    game_limit_service: GameLimitService,
    lang: str,
    usage_key: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)
    if message.chat.type not in GROUP_TYPES:
        await message.answer(t("h_pvp_group_only", lang))
        return
    if bet is None:
        await message.answer(legacy_md_to_html(t(usage_key, lang)))
        return

    # #1559 item 5: publishing an offer is a wallet write, and it was
    # the only one on this surface with no per-user cap at all —
    # /roll, /flip and /roulette every one of them gate on
    # GameLimitService, so a create->cancel loop here was bounded by
    # nothing but the global 2-req/sec bucket.
    #
    # The whole check -> create -> record sequence runs under the
    # shared per-user play lock, and the stamp is committed INSIDE it:
    # ``record`` is a bare ``session.add``, so without the checkpoint
    # the next update from the same player would read ``game_plays``
    # without the row and be waved through (games/limits.py:69-77
    # spells the contract out; #222-B is the window it closes).
    #
    # The stamp is taken on a SUCCESSFUL create only, so a bad bet or
    # an empty wallet never burns a cooldown slot — /roll's
    # no-cooldown-on-typo posture. A cancelled offer keeps its slot,
    # which is precisely the property the cap exists for.
    now = datetime.now()  # noqa: DTZ005 — naive local, matches game_plays
    async with PLAY_LOCKS.acquire(tg_user.id):
        abuse = await game_limit_service.check(tg_user.id, now=now)
        if not abuse.allowed:
            await message.answer(render_abuse_refusal(lang, abuse))
            log.bind(uid=tg_user.id, reason=abuse.reason).info("/pvp create blocked")
            return
        result = await pvp_service.create_offer(
            creator_id=tg_user.id,
            game=game,
            bet=bet,
            side=side,
            chat_id=message.chat.id,
            now=db_now(),
        )
        if result.outcome is PvpCreateOutcome.OK:
            await game_limit_service.record(tg_user.id, game=f"pvp_{game}", now=now)
        # #1967: the checkpoint now covers the refusals too, not just OK.
        # ``INSUFFICIENT_FUNDS`` also comes back from a ``hold`` whose
        # guarded UPDATE matched zero rows — a lost race — and a guarded
        # UPDATE that changes nothing still promotes the transaction to
        # ``BEGIN IMMEDIATE`` (``db/engines.py``). Left alone, the
        # refusal below is sent with ``economy.db`` locked over a write
        # that never happened. On ``INVALID_BET``/``NO_WALLET``, and on
        # the pre-check flavour of ``INSUFFICIENT_FUNDS``, nothing was
        # written and the call is a no-op — the same shape ``/daily``
        # uses for RACE_LOST beside plain COOLDOWN (#1861).
        if checkpoint is not None:
            await checkpoint()
    if result.outcome is PvpCreateOutcome.INVALID_BET:
        # T-020/R9: quote the live bounds instead of the two literals
        # the string used to bake in. The ceiling is now one shared
        # constant, and a refusal that names a stale number is worse
        # than no number at all.
        await message.answer(
            t(
                "h_pvp_invalid_bet",
                lang,
                min_bet=format_number(MIN_BET),
                max_bet=format_number(MAX_BET),
            )
        )
        return
    if result.outcome in (
        PvpCreateOutcome.NO_WALLET,
        PvpCreateOutcome.INSUFFICIENT_FUNDS,
    ):
        await message.answer(t("h_pvp_insufficient", lang, bet=bet))
        return

    assert result.offer_id is not None
    # RR-3 #32: the offer card states the rules an accepter is agreeing to
    # — the prize they play for, the side left to them on coin, and the
    # higher-roll-wins / tie-refunds contract on dice (legacy
    # bot.py:20982-20984 carried the same rule lines).
    #
    # T-020/R8: this quotes what the winner actually COLLECTS, not the
    # 2×bet pot. The two stopped being the same number once the house
    # took its cut, and the card an opponent agrees to has to be the
    # honest one — the result card names the fee explicitly.
    prize = format_number(split_pot(bet)[0])
    if game == "coin":
        other_side: CoinSide = "tails" if side == "heads" else "heads"
        title = t(
            "h_pvp_offer_coin",
            lang,
            name=_name(tg_user),
            bet=format_number(bet),
            side=t(f"h_pvp_side_{side}", lang),
            other_side=t(f"h_pvp_side_{other_side}", lang),
            prize=prize,
        )
    else:
        title = t(
            "h_pvp_offer_dice",
            lang,
            name=_name(tg_user),
            bet=format_number(bet),
            prize=prize,
        )
    card = await message.answer(title, reply_markup=_offer_keyboard(result.offer_id, lang))
    await pvp_service.set_offer_message(result.offer_id, card.chat.id, card.message_id)


def _result_text(res: object, lang: str, names: dict[int, str | None]) -> str:
    """Render the resolved-game card body from a PvpAcceptResult.

    ``names`` maps player id → display name so the winner reads as a
    proper mention rather than a bare numeric id.

    T-020/R8: both decided cards quote ``res.payout`` — what the service
    actually credited — and name the burned remainder, rather than the
    ``2 × bet`` pot the winner no longer collects in full. Reading the
    numbers off the result instead of re-deriving them from ``bet``
    means the card and the wallet cannot disagree.
    """
    from telegram_invite_bot.services.pvp_service import PvpAcceptResult

    assert isinstance(res, PvpAcceptResult)
    if res.coin is not None:
        flip = t(f"h_pvp_side_{res.coin.flip}", lang)
        winner_seat = res.coin.winner
        winner_id = res.creator_id if winner_seat == CREATOR else res.opponent_id
        return t(
            "h_pvp_result_coin",
            lang,
            flip=flip,
            winner=mention_html(winner_id, names.get(winner_id)),
            payout=format_number(res.payout),
            rake=format_number(res.rake),
        )
    assert res.dice is not None
    if res.winner_id is None:
        return t(
            "h_pvp_result_dice_tie",
            lang,
            c=res.dice.creator_roll,
            o=res.dice.opponent_roll,
        )
    return t(
        "h_pvp_result_dice",
        lang,
        c=res.dice.creator_roll,
        o=res.dice.opponent_roll,
        winner=mention_html(res.winner_id, names.get(res.winner_id)),
        payout=format_number(res.payout),
        rake=format_number(res.rake),
    )


def build_router(registry: EngineRegistry) -> Router:
    """Build the /pvp_coin + /pvp_dice router (group-only, EconomyMiddleware)."""
    router = Router(name="pvp_stake")
    router.message.middleware(EconomyMiddleware(registry))
    router.callback_query.middleware(EconomyMiddleware(registry))

    group_filter = F.chat.type.in_(GROUP_TYPES)

    async def _coin(
        message: Message,
        pvp_service: PvpService,
        game_limit_service: GameLimitService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        parts = command_body(message).split()
        bet = _parse_bet(parts[1] if len(parts) > 1 else None)
        side = normalize_side(parts[2]) if len(parts) > 2 else None
        if side is None:
            await message.answer(t("pvp_coin_usage", lang))
            return
        await _handle_create(
            message,
            game="coin",
            side=side,
            bet=bet,
            pvp_service=pvp_service,
            game_limit_service=game_limit_service,
            lang=lang,
            usage_key="pvp_coin_usage",
            checkpoint=checkpoint,
        )

    async def _dice(
        message: Message,
        pvp_service: PvpService,
        game_limit_service: GameLimitService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        parts = command_body(message).split()
        bet = _parse_bet(parts[1] if len(parts) > 1 else None)
        await _handle_create(
            message,
            game="dice",
            side=None,
            bet=bet,
            pvp_service=pvp_service,
            game_limit_service=game_limit_service,
            lang=lang,
            usage_key="pvp_dice_usage",
            checkpoint=checkpoint,
        )

    async def _close_expired_card(bot: Bot, expired: ExpiredOffer | None, lang: str) -> None:
        """Strip the «Accept» keyboard off a card this tap just retired (#2020).

        Set only when the tap's own lazy expiry is what retired the
        offer, which is also the call that refunded the stake — so this
        runs once per offer, exactly like the sweeper's pass, and never
        for a tap that merely found an already-dead card.

        This is the sweeper's job (``_notify_pvp_expired``, #1766) done
        by hand, because the sweeper cannot do it here: it only ever sees
        the offers it retires itself, and ``expire_guard`` retires each
        one exactly once. An offer closed on the accept path is invisible
        to it forever, so without this the card stays tappable for the
        life of the message.

        Two deliberate differences from the sweeper's version:

        * the text is rendered in the TAPPER's language, not the
          creator's. The sweeper resolves the creator's because it has
          nobody else; this router carries no ``users.db`` session, and
          adding one so a group card can pick between two strings is not
          worth the wiring. A bilingual group can therefore see either,
          depending on who closed the card.
        * the card is addressed by the offer's own ``chat_id`` /
          ``message_id`` rather than through ``callback.message``. Those
          are the coordinates ``set_offer_message`` pinned; the tapped
          message is only usually the same one, and a card forwarded
          inside its own chat would sail past the chat gate with a
          different id.

        Nothing is retried, and a benign reject is silence: "message to
        edit not found" is the expected answer for a card somebody
        deleted, and the refund it accompanies is already committed.
        """
        if expired is None or expired.message_id is None:
            return
        try:
            await bot.edit_message_text(
                chat_id=expired.chat_id,
                message_id=expired.message_id,
                text=t("h_pvp_expired_card", lang),
                reply_markup=None,
            )
        except TelegramBadRequest as exc:
            if not any(marker in str(exc) for marker in BENIGN_EDIT_REJECTS):
                raise
        except TelegramAPIError as exc:
            log.bind(offer=expired.offer_id, exc=str(exc)).info(
                "pvp expired card edit failed on the accept path"
            )

    async def _accept(
        callback: CallbackQuery,
        callback_data: PvpAccept,
        pvp_service: PvpService,
        game_limit_service: GameLimitService,
        lang: str,
        bot: Bot,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        if callback.from_user is None:
            return
        msg = callback.message
        if msg is None:
            # #1604: the offer is scoped to the chat it was posted
            # in, and without the card there is no way to tell which
            # chat the tap came from. Telegram only omits ``message``
            # for a callback older than its own retention window, so
            # the card is dead in every sense anyway.
            await callback.answer(t("h_pvp_not_found", lang), show_alert=True)
            return
        # #1751: tapping ✅ is a whole settled play — a ``hold`` on both
        # wallets, an RNG roll, a credit to the winner and a rake — and it
        # was the one seat on this surface that counted for nothing.
        # ``_handle_create`` caps the publisher, and every sibling accept
        # caps its acceptor (``duel.accept_duel_challenge``,
        # ``rps.accept_rps_challenge``, ``challenge_commands._dispatch``).
        # A player who had spent the day's slots on /roulette only needed
        # a partner willing to post the offer, and played on unbounded.
        #
        # ``include_cooldown=False`` for the reason duel's accept passes
        # it: the 180 s clock spaces out plays a user serves themselves,
        # and an accept lands on somebody else's schedule.
        #
        # PLAY_LOCKS guards the COUNTER, not the money — ``claim_for_accept``
        # and ``hold`` are both atomic guarded UPDATEs, so a double tap was
        # already refused at the SQL layer. The stamp is committed INSIDE
        # the lock because ``record`` is a bare ``session.add`` (#222-B):
        # until something commits the row is invisible to every other
        # connection, and this player's next update walks past the cap.
        acceptor_id = callback.from_user.id
        now = datetime.now()  # noqa: DTZ005 — naive local, matches game_plays
        async with PLAY_LOCKS.acquire(acceptor_id):
            abuse = await game_limit_service.check(acceptor_id, now=now, include_cooldown=False)
            if not abuse.allowed:
                await callback.answer(render_abuse_refusal(lang, abuse), show_alert=True)
                log.bind(
                    uid=acceptor_id,
                    offer=callback_data.offer_id,
                    reason=abuse.reason,
                ).info("pvp accept blocked")
                return
            res = await pvp_service.accept_and_resolve(
                offer_id=callback_data.offer_id,
                opponent_id=acceptor_id,
                chat_id=msg.chat.id,
                now=db_now(),
            )
            if res.outcome is not PvpAcceptOutcome.SUCCESS and checkpoint is not None:
                # #1970: every refusal below answers the tap with the
                # update's transaction still open, and two of them have
                # already written by then — ``claim_for_accept`` is a
                # guarded UPDATE, and a lost hold race adds
                # ``revert_accept`` on top. A guarded UPDATE that matches
                # zero rows is still write-headed, so even the loser of a
                # double tap holds economy.db's single writer slot for the
                # whole alert round trip. The refusals that never wrote
                # (self-tap, a stale card that was not expired) reach a
                # no-op checkpoint, exactly as a plain ``/daily`` COOLDOWN
                # does.
                #
                # Scoped to the refusals ON PURPOSE: hoisting this above
                # the chain unconditionally would commit a SETTLED game
                # before ``game_limit_service.record`` stamps the slot,
                # and #222-B needs those two in one transaction.
                await checkpoint()
            if res.outcome is PvpAcceptOutcome.CREATOR_CANNOT_ACCEPT:
                await callback.answer(t("h_pvp_self", lang), show_alert=True)
                return
            if res.outcome is PvpAcceptOutcome.ALREADY_TAKEN:
                await callback.answer(t("h_pvp_already_taken", lang), show_alert=True)
                return
            if res.outcome is PvpAcceptOutcome.NOT_FOUND:
                await callback.answer(t("h_pvp_not_found", lang), show_alert=True)
                await _close_expired_card(bot, res.expired, lang)
                return
            if res.outcome in (
                PvpAcceptOutcome.NO_WALLET,
                PvpAcceptOutcome.INSUFFICIENT_FUNDS,
            ):
                await callback.answer(t("h_pvp_opp_insufficient", lang), show_alert=True)
                return
            # Stamped on a SETTLED accept only, so a stale card, a
            # self-tap or an empty wallet never burns a slot — the same
            # no-cooldown-on-a-typo posture ``_handle_create`` takes.
            await game_limit_service.record(acceptor_id, game=f"pvp_{res.game}", now=now)
            # The play is over: the RNG rolled, both stakes moved and the
            # offer is closed. Everything below is Telegram, and until this
            # commit lands the update's transaction holds economy.db's single
            # writer slot across up to three round-trips — on a small host
            # that is every other writer queued behind Telegram's latency.
            # Committing here also removes the worse half: a failure while
            # announcing the result no longer unwinds a settled game, which
            # would refund a loss, claw back a win and reopen an offer that
            # two people already watched resolve. Mirrors roulette.py.
            if checkpoint is not None:
                await checkpoint()
        # Card I/O is deliberately outside the play lock, exactly as in
        # ``duel.accept_duel_challenge``: nothing below touches the cap.
        await callback.answer()
        # Opponent name is free (the clicker); the creator's name needs a
        # best-effort fetch — fall back to the numeric mention on any error.
        names: dict[int, str | None] = {callback.from_user.id: callback.from_user.first_name}
        with contextlib.suppress(TelegramAPIError):
            creator_chat = await bot.get_chat(res.creator_id)
            names[res.creator_id] = getattr(creator_chat, "first_name", None) or getattr(
                creator_chat, "title", None
            )
        body = _result_text(res, lang, names)
        if isinstance(msg, Message):
            try:
                await msg.edit_text(body)
            except TelegramAPIError as exc:
                # The pot is already split AND committed, so the result
                # has to land somewhere: fall back to a fresh message. A
                # non-API failure is a bug in our own body and still
                # propagates, but it can no longer take the payout with
                # it — that is what the checkpoint above bought.
                log.debug("pvp card edit failed: {exc!r}", exc=exc)
                await bot.send_message(msg.chat.id, body)

    async def _cancel(
        callback: CallbackQuery,
        callback_data: PvpCancel,
        pvp_service: PvpService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        if callback.from_user is None:
            return
        ok = await pvp_service.cancel(
            offer_id=callback_data.offer_id, creator_id=callback.from_user.id
        )
        if not ok:
            await callback.answer(t("h_pvp_cancel_denied", lang), show_alert=True)
            return
        # #1562: same posture as ``_accept`` above and as
        # ``handlers/games.py``. The offer is cancelled and the stake
        # released, but nothing is committed until this handler
        # returns. Without the checkpoint the user is told "cancelled"
        # and then a broken card body — or the process dying — rolls
        # the whole thing back: the offer is pending again, the stake
        # is still held, and it stays open for any group member to
        # accept until the TTL sweeper reaches it. Coins are never
        # lost (the unwind is consistent), but what the user was told
        # and what the database holds diverge.
        if checkpoint is not None:
            await checkpoint()
        await callback.answer(t("h_pvp_cancelled", lang))
        msg = callback.message
        if isinstance(msg, Message):
            # ``edit_card`` rather than a bare ``suppress(Exception)``:
            # the offer card going stale is expected, a broken card body
            # is our bug and used to vanish here without a trace.
            await edit_card(msg, t("h_pvp_cancelled_card", lang))

    router.message.register(
        _coin,
        Command("pvp_coin", "пвп_монета", ignore_case=True),
        F.from_user,
        group_filter,
    )
    router.message.register(
        _dice,
        Command("pvp_dice", "пвп_кости", ignore_case=True),
        F.from_user,
        group_filter,
    )
    # #1563: ``with_chat_type_refusal`` below walks ``router.message``
    # only (handlers/chat_scope.py) and hangs its refusals on the
    # refusal router's message observer, so it never reaches a
    # callback. Without this the two buttons were the one part of a
    # module whose docstring says "group-only" that accepted a
    # private-chat update. Not exploitable as shipped — the buttons
    # only exist on a card sent to a group, cancel is owner-checked
    # service-side and self-accept is refused — but the surface was
    # wider than advertised and any callback added to this router
    # would have inherited it. Observer-level, matching the six other
    # routers that scope their callbacks this way (e.g.
    # handlers/checks.py); note ``group_filter`` above is a MESSAGE
    # filter — ``F.chat`` on a ``CallbackQuery`` is ``None`` and would
    # silently match nothing.
    router.callback_query.filter(F.message.chat.type.in_(GROUP_TYPES))
    router.callback_query.register(_accept, PvpAccept.filter())
    router.callback_query.register(_cancel, PvpCancel.filter())
    return with_chat_type_refusal(router, scope="group")
