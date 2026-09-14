"""End-to-end ``/pvp_coin`` + ``/pvp_dice`` offer cards (RR-3 #32).

The PvP stake surface had no handler-level coverage at all; these tests
pin the *challenge card* — the message an opponent reads before tapping
Accept — plus the escrow side-effect that publishing it implies.

RR-3 #32 is specifically about that card being self-explanatory:

* coin — the creator's side AND the side the accepter inherits, so the
  opponent knows what they are betting on without asking;
* dice — the rule legacy carried on its card (bot.py:20982, "кто
  выбросит больше — тот победил") plus the tie-refund clause;
* both — the prize the winner collects. T-020/R8: that is 1.9×bet,
  NOT the 2×bet pot, and the card must quote the honest number.

The router is mounted STANDALONE (like ``test_stake_games.py``): it owns
its ``EconomyMiddleware``, so only ``LanguageMiddleware`` is added for
the ``lang`` kwarg.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import select, update

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.game_limits import GamePlay
from telegram_invite_bot.db.models.pvp import PvpOffer
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.games.limits import MAX_BET, MIN_BET
from telegram_invite_bot.handlers import pvp_stake as pvp_handler
from telegram_invite_bot.handlers.pvp_stake import PvpAccept, PvpCancel
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.language import LanguageMiddleware
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.services.roulette_service import COOLDOWN_SEC, MAX_PER_DAY
from telegram_invite_bot.utils.numbers import format_number
from telegram_invite_bot.utils.time import db_now
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

GROUP_CHAT_ID = -1005
USER_ID = 500


async def _wire(make_wired: WiredFactory) -> tuple[Bot, Dispatcher, Any]:
    bot, _, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.outer_middleware(LanguageMiddleware(registry))
    dispatcher.callback_query.outer_middleware(LanguageMiddleware(registry))
    dispatcher.include_router(pvp_handler.build_router(registry))
    return bot, dispatcher, registry


async def _seed_wallet(registry: Any, user_id: int, *, balance: int = 1_000) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    return wallet.balance if wallet is not None else None


async def _offers(registry: Any) -> list[PvpOffer]:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (await session.execute(select(PvpOffer))).scalars().all()
    return list(rows)


def _update(
    text: str,
    *,
    user_id: int = USER_ID,
    chat_type: str = "supergroup",
    chat_id: int = GROUP_CHAT_ID,
) -> Update:
    return make_message_update(
        text,
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        first_name="Кир",
        language_code="ru",
    )


OPPONENT_ID = 501


def _accept_update(
    offer_id: int, *, user_id: int = OPPONENT_ID, chat_id: int = GROUP_CHAT_ID
) -> Update:
    return make_callback_update(
        PvpAccept(offer_id=offer_id).pack(),
        user_id=user_id,
        first_name="Оппонент",
        language_code="ru",
        chat_id=chat_id,
        chat_type="supergroup",
    )


def _cancel_update(offer_id: int, *, user_id: int = USER_ID) -> Update:
    return make_callback_update(
        PvpCancel(offer_id=offer_id).pack(),
        user_id=user_id,
        first_name="Кир",
        language_code="ru",
        chat_id=GROUP_CHAT_ID,
        chat_type="supergroup",
    )


def _capture_callback(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[str],
    *,
    fail_on: str | None = None,
) -> None:
    """Record the wire for a callback flow, optionally crashing on one call.

    Wider than :func:`_capture`, which asserts on anything but
    ``SendMessage`` — a resolved game answers the callback and edits the
    card. ``fail_on`` raises a NON-Telegram error from the named method,
    which is the failure ``_accept`` does not catch.
    """

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == fail_on:
            raise RuntimeError(f"synthetic failure in {name}")
        if name == "GetChat":
            # Best-effort name lookup the handler suppresses. Refusing it
            # exercises the numeric-mention fallback and spares this
            # fixture a hand-built ChatFullInfo.
            raise TelegramBadRequest(method=method, message="chat not found")
        if name == "AnswerCallbackQuery":
            sink.append("callback_answer")
            return True
        if name == "EditMessageText":
            sink.append(method.text)
            return True
        if name != "SendMessage":
            raise AssertionError(f"unexpected Telegram call: {name}")
        sink.append(method.text)
        return Message(
            message_id=7,
            date=datetime(2024, 1, 1),  # noqa: DTZ001 — synthetic fixture
            chat=Chat(id=method.chat_id, type="supergroup"),
            from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
            text=method.text,
        )

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


def _capture(bot: Bot, monkeypatch: pytest.MonkeyPatch, sink: list[str]) -> None:
    """Record every outgoing SendMessage body."""

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name != "SendMessage":
            raise AssertionError(f"unexpected Telegram call: {name}")
        sink.append(method.text)
        return Message(
            message_id=7,
            date=datetime(2024, 1, 1),  # noqa: DTZ001 — synthetic fixture
            chat=Chat(id=method.chat_id, type="supergroup"),
            from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
            text=method.text,
        )

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


# ── Coin offer card ──────────────────────────────────────────────────


async def test_coin_offer_card_states_both_sides_and_prize(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepter learns which side is left to them and what they win."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[str] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _update("/pvp_coin 100 орёл"))

    assert result is not UNHANDLED
    [card] = sent
    assert "<b>орёл</b>" in card  # creator's call
    assert "<b>решка</b>" in card  # the side the opponent inherits
    # T-020/R8: the WINNER'S TAKE, not the 200-coin pot. Quoting the pot
    # here would promise an accepter 10 coins the payout never pays.
    assert "<b>190</b>" in card
    assert "<b>200</b>" not in card
    # The stake is escrowed the moment the card goes up.
    assert await _balance(registry, USER_ID) == 900
    [offer] = await _offers(registry)
    assert (offer.game, offer.bet) == ("coin", 100)
    assert json.loads(offer.params_json)["side"] == "heads"


async def test_coin_offer_card_mirrors_sides_for_tails(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inherited side is derived, not hard-coded — tails flips it."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[str] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/pvp_coin 250 решка"))

    [card] = sent
    creator_at = card.index("<b>решка</b>")
    opponent_at = card.index("<b>орёл</b>")
    # Creator's side is announced first, the inherited one after it.
    assert creator_at < opponent_at
    assert "<b>475</b>" in card  # winner's take on a 250 stake


# ── Dice offer card ──────────────────────────────────────────────────


async def test_dice_offer_card_carries_the_rule_and_tie_clause(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy's card said who wins; the tie-refund is ours to state too."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[str] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _update("/pvp_dice 300"))

    assert result is not UNHANDLED
    [card] = sent
    assert "кто выбросит больше" in card
    assert "Ничья" in card
    assert "<b>570</b>" in card  # winner's take on a 300 stake
    assert await _balance(registry, USER_ID) == 700
    [offer] = await _offers(registry)
    assert (offer.game, offer.bet) == ("dice", 300)


# ── Refusals publish no card ─────────────────────────────────────────


async def test_insufficient_funds_publishes_no_offer_card(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=50)
    sent: list[str] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/pvp_dice 300"))

    [refusal] = sent
    assert "кто выбросит больше" not in refusal
    assert await _balance(registry, USER_ID) == 50
    assert await _offers(registry) == []


async def test_bet_above_the_ceiling_is_refused_with_the_live_bounds(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-020/R9. The refusal used to bake "от 10 до 10000" into the
    string; it now interpolates the shared constants, and an unpassed
    placeholder renders LITERALLY rather than raising — so assert the
    numbers are really there and no brace survived.

    The wallet is funded well above the attempted stake so the only
    thing that can reject this is the ceiling.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=MAX_BET * 10)
    sent: list[str] = []
    _capture(bot, monkeypatch, sent)

    # ``/pvp_dice`` rather than ``/pvp_coin``: the coin command also
    # wants a side, and a missing one short-circuits to the usage text
    # before the bet is ever validated.
    await dispatcher.feed_update(bot, _update(f"/pvp_dice {MAX_BET + 1}"))

    [refusal] = sent
    assert format_number(MAX_BET) in refusal
    assert format_number(MIN_BET) in refusal
    assert "{" not in refusal and "}" not in refusal
    assert await _balance(registry, USER_ID) == MAX_BET * 10
    assert await _offers(registry) == []


async def test_private_chat_is_refused_without_touching_the_wallet(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stakes are group-only — a DM gets the refusal, not a game (#123).

    The router's group filter still drops the DM before any staking
    code runs; the balance assertion is the half that matters and is
    unchanged. What used to be silence is now the group-only twin.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[str] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(
        bot, _update("/pvp_dice 300", chat_type="private", chat_id=USER_ID)
    )

    assert result is not UNHANDLED
    assert sent == [t("h_group_only_command", "ru", command="pvp_dice")]
    assert await _balance(registry, USER_ID) == 1_000


# -- Accept: the settled game (#1282, #1289) --------------------------


async def _published_coin_offer(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[Bot, Dispatcher, Any, int]:
    """Run a real ``/pvp_coin 100 орёл`` and hand back its offer id.

    Going through the command rather than inserting the row keeps the
    escrow side of the fixture honest: the creator is 100 coins down
    because the handler debited them, not because the test said so.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    await _seed_wallet(registry, OPPONENT_ID, balance=1_000)
    _capture_callback(bot, monkeypatch, [])

    await dispatcher.feed_update(bot, _update("/pvp_coin 100 орёл"))

    [offer] = await _offers(registry)
    assert await _balance(registry, USER_ID) == 900
    return bot, dispatcher, registry, offer.id


async def _backdate(registry: Any, offer_id: int, *, minutes: int) -> None:
    """Push an offer's ``created_at`` back past the TTL.

    The alternative — publishing with a doctored clock — is not
    available here: the card goes out through the real command, which
    stamps ``created_at`` from its own ``db_now()``.
    """
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        await session.execute(
            update(PvpOffer)
            .where(PvpOffer.id == offer_id)
            .values(created_at=db_now() - timedelta(minutes=minutes))
        )
        await session.commit()


async def test_accepting_settles_the_pot_and_closes_the_offer(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1289: nothing anywhere constructed a ``PvpAccept``.

    Six tests covered publishing the card and none covered the tap that
    spends the money, which is why #1282 could sit in ``_accept``
    unnoticed. The pot total is asserted rather than either wallet
    because the coin flip is real: whoever wins, the two balances sum to
    the 2 000 they started with minus the 10-coin house cut on a 100
    stake (T-020/R8 — 1.9x to the winner).
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    sent: list[str] = []
    _capture_callback(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _accept_update(offer_id))

    assert result is not UNHANDLED
    creator = await _balance(registry, USER_ID)
    opponent = await _balance(registry, OPPONENT_ID)
    assert creator is not None and opponent is not None
    assert creator + opponent == 1_990
    assert {creator, opponent} == {900, 1_090}
    [settled] = await _offers(registry)
    assert settled.status == "finished"
    assert "callback_answer" in sent


async def test_a_tap_that_expires_the_offer_closes_the_card_it_tapped(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2020: the lazy expiry retired the offer and left the card live.

    ``PvpService.accept_and_resolve`` runs legacy's on-access expiry
    before it will match anyone (#267), and that expiry is a real one:
    the row flips to ``expired`` and the creator's stake comes back. The
    handler then answered ``h_pvp_not_found`` and stopped, so the
    challenge card kept its «Accept» keyboard — and because
    ``expire_guard`` retires an offer exactly once, the sweeper that
    exists to close those cards (``_notify_pvp_expired``, #1766) never
    saw this one and never will. The card is permanently live: every
    later tap re-answers ``h_pvp_not_found``, and none of them can ever
    reach the branch that would close it.

    That is strictly worse than the backlog #1766 was written for. Those
    thirty cards were an oversight the sweeper now clears; this one is a
    card the sweeper is structurally unable to reach.
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    await _backdate(registry, offer_id, minutes=11)
    sent: list[str] = []
    _capture_callback(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _accept_update(offer_id))

    assert result is not UNHANDLED
    [expired] = await _offers(registry)
    assert expired.status == "expired"
    assert await _balance(registry, USER_ID) == 1_000, "the stake did not come back"
    assert await _balance(registry, OPPONENT_ID) == 1_000, "the tapper was charged"
    assert t("h_pvp_expired_card", "ru") in sent, (
        "the offer is retired and unreachable by the sweeper, but its card"
        f" still carries a live «Accept» button: {sent}"
    )


async def test_a_tap_on_a_merely_stale_card_does_not_rewrite_it_twice(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the tap that DID the expiring owns the edit.

    ``expire`` returns the retired offer iff that call is the one that
    retired it, which is the same guarantee the refund rides on. A
    second tap on the same dead card finds an offer that is already
    ``expired``, moves no coins, and must not spend a Telegram edit
    saying so again — the card it would rewrite already says it.
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    await _backdate(registry, offer_id, minutes=11)
    _capture_callback(bot, monkeypatch, [])
    await dispatcher.feed_update(bot, _accept_update(offer_id))

    second: list[str] = []
    _capture_callback(bot, monkeypatch, second)
    await dispatcher.feed_update(bot, _accept_update(offer_id))

    assert second == ["callback_answer"], f"the second tap rewrote the card again: {second}"


async def test_a_forwarded_card_tapped_in_another_chat_settles_nothing(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1604: the handler must hand the service the TAP's chat.

    The service-level pin lives in
    ``tests/integration/services/test_pvp_service.py`` and would
    still pass if ``_accept`` read the chat off the wrong object —
    the offer row itself, say, which would make the gate compare a
    value with itself and never refuse anything. Telegram keeps an
    inline keyboard live across a forward, so the same ``PvpAccept``
    payload is fed here from a SECOND supergroup.

    Both halves are asserted: nothing moves in the foreign chat, and
    the offer is still takeable at home afterwards. A gate that
    consumed the offer would have turned a scope fix into a way for
    any passer-by to kill any challenge.
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    _capture_callback(bot, monkeypatch, [])

    foreign = await dispatcher.feed_update(bot, _accept_update(offer_id, chat_id=GROUP_CHAT_ID - 1))

    # The router still owns the callback — it is the SERVICE that
    # refuses, so ``UNHANDLED`` would mean the wrong gate fired.
    assert foreign is not UNHANDLED
    assert await _balance(registry, USER_ID) == 900  # stake still escrowed
    assert await _balance(registry, OPPONENT_ID) == 1_000  # never debited
    [untouched] = await _offers(registry)
    assert untouched.status == "pending"

    assert await dispatcher.feed_update(bot, _accept_update(offer_id)) is not UNHANDLED
    [settled] = await _offers(registry)
    assert settled.status == "finished"


async def test_a_lost_hold_race_on_accept_releases_the_write_lock(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1970: the accept refusals answered the tap under ``BEGIN IMMEDIATE``.

    Same defect class as #1967/#1968, one surface further in. The
    checkpoint on this handler sits BELOW the four refusal branches, so
    only a SETTLED accept ever released ``economy.db``. A refusal that
    had already written — ``claim_for_accept`` is a guarded UPDATE, and
    a lost hold race adds ``revert_accept`` on top — kept the single
    writer slot for the whole ``answer(..., show_alert=True)`` round
    trip while SQLite's ``busy_timeout`` is five seconds.

    The race is reproduced the way it actually happens: the row on disk
    is drained after the card went up, while ``EconomyRepo.get`` still
    reports what the tapper had a moment ago, so the ``balance < bet``
    pre-check waves the accept through and the guarded ``hold`` is the
    one that says no — after the claim has already been written.

    The fix must stay SCOPED to the refusals: hoisting the checkpoint
    above the outcome chain unconditionally would commit the settlement
    before ``game_limit_service.record``, and #222-B needs those two in
    one transaction.
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    sessionmaker = registry.session(DBName.ECONOMY)

    # Drained between the pre-check and the hold — the only way to reach
    # INSUFFICIENT_FUNDS with the claim already on disk.
    async with sessionmaker() as session:
        await session.execute(
            update(EconomyUser).where(EconomyUser.user_id == OPPONENT_ID).values(balance=0)
        )
        await session.commit()

    original_get = EconomyRepo.get

    async def rich_get(self: EconomyRepo, user_id: int) -> Any:
        """What the tapper saw: a balance the row no longer has."""
        wallet = await original_get(self, user_id)
        if wallet is None or user_id != OPPONENT_ID:
            return wallet
        return replace(wallet, balance=wallet.balance + 100_000)

    monkeypatch.setattr(EconomyRepo, "get", rich_get)

    sent: list[str] = []
    _capture_callback(bot, monkeypatch, sent)
    probe: list[str] = []
    original_request = bot.session.make_request

    async def probing(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        # The refusal is an ALERT, not a message — this is the only
        # outbound call the losing tap makes.
        if type(method).__name__ != "AnswerCallbackQuery":
            return await original_request(_bot, method, timeout=timeout)
        try:
            async with sessionmaker() as other:
                await other.execute(
                    update(EconomyUser).where(EconomyUser.user_id == -1).values(balance=0)
                )
                await other.commit()
        except Exception as exc:  # noqa: BLE001 — the verdict IS the exception
            probe.append(repr(exc))
        else:
            probe.append("free")
        return await original_request(_bot, method, timeout=timeout)

    monkeypatch.setattr(bot.session, "make_request", probing)

    assert await dispatcher.feed_update(bot, _accept_update(offer_id)) is not UNHANDLED

    assert sent == ["callback_answer"], f"the refusal alert never fired: {sent}"
    assert probe == ["free"], f"economy.db was still locked during the alert: {probe}"
    # And the refusal really refused: no money moved, and the revert put
    # the offer back up for grabs. Read the ROW, not the repo — the repo
    # is the thing lying about the balance in this test.
    async with sessionmaker() as session:
        drained = await session.get(EconomyUser, OPPONENT_ID)
        creator = await session.get(EconomyUser, USER_ID)
    assert drained is not None and drained.balance == 0
    assert creator is not None and creator.balance == 900  # still escrowed
    [offer] = await _offers(registry)
    assert offer.status == "pending"


async def test_a_crash_while_announcing_no_longer_unwinds_a_settled_game(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1282: ``_accept`` never committed before it talked to Telegram.

    ``accept_and_resolve`` moves both stakes and closes the offer, then
    the handler makes up to three more network calls with the update's
    transaction still open. The handler only catches ``TelegramAPIError``
    around the edit, so anything else propagates and the session
    middleware rolls the whole settled game back — after the players may
    already have been told who won.

    A ``RuntimeError`` out of ``EditMessageText`` is that shape exactly.
    With the checkpoint in place the payout survives the crash; without
    it both wallets snap back to their pre-tap values (1 900 in total
    rather than 1 990) and the offer reopens.
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    _capture_callback(bot, monkeypatch, [], fail_on="EditMessageText")

    with pytest.raises(RuntimeError, match="synthetic"):
        await dispatcher.feed_update(bot, _accept_update(offer_id))

    creator = await _balance(registry, USER_ID)
    opponent = await _balance(registry, OPPONENT_ID)
    assert creator is not None and opponent is not None
    assert creator + opponent == 1_990
    [settled] = await _offers(registry)
    assert settled.status == "finished"


# -- Cancel: the creator reclaims the hold (#1289) ---------------------


async def test_a_stranger_cannot_cancel_and_the_creator_can(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PvpCancel`` was constructed by no test either.

    The card carries a Cancel button and the callback data is a plain
    offer id, so the ownership check is the only thing between a
    passer-by and somebody else's escrow. Both halves are asserted here
    because a fix that refused everyone would also pass the first one.
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    _capture_callback(bot, monkeypatch, [])

    await dispatcher.feed_update(bot, _cancel_update(offer_id, user_id=OPPONENT_ID))

    assert await _balance(registry, USER_ID) == 900
    [still_open] = await _offers(registry)
    assert still_open.status == "pending"

    await dispatcher.feed_update(bot, _cancel_update(offer_id))

    assert await _balance(registry, USER_ID) == 1_000
    [cancelled] = await _offers(registry)
    assert cancelled.status == "cancelled"


# -- #1559 item 5: publishing an offer is capped like every other play --


async def _plays(registry: Any) -> list[GamePlay]:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (await session.execute(select(GamePlay))).scalars().all()
    return list(rows)


async def test_publishing_a_coin_offer_stamps_a_play(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create path writes a ``game_plays`` row, and commits it.

    ``GameLimitService.record`` is a bare ``session.add``; the stamp is
    read back through a FRESH session here, so a row that never reached
    the database would come back empty and fail this test.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    _capture(bot, monkeypatch, [])

    await dispatcher.feed_update(bot, _update("/pvp_coin 100 орёл"))

    [play] = await _plays(registry)
    assert play.user_id == USER_ID
    assert play.game == "pvp_coin"


async def test_publishing_a_dice_offer_stamps_its_own_label(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two commands share a cap but not a label.

    The caps are per-user, not per-game, so the label is diagnostics
    only — but a single ``"pvp"`` for both would make the admin view
    unable to tell the two surfaces apart.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    _capture(bot, monkeypatch, [])

    await dispatcher.feed_update(bot, _update("/pvp_dice 100"))

    [play] = await _plays(registry)
    assert play.game == "pvp_dice"


async def test_the_cooldown_refuses_a_second_offer_and_spares_the_wallet(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1559 item 5: the create->cancel loop is what this closes.

    Before the gate, publishing an offer was the ONLY wallet write on
    this surface with no per-user cap at all, so a creator could open
    and cancel offers as fast as the global rate bucket allowed. The
    second create here lands well inside ``COOLDOWN_SEC`` and must be
    refused BEFORE the escrow debit — hence the balance assertion.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[str] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/pvp_coin 100 орёл"))
    await dispatcher.feed_update(bot, _update("/pvp_coin 100 орёл"))

    assert len(sent) == 2
    assert "Слишком часто" in sent[1]
    assert str(COOLDOWN_SEC) in sent[1] or "сек" in sent[1]
    # One escrow hold, not two: the refusal precedes ``create_offer``.
    assert await _balance(registry, USER_ID) == 900
    assert len(await _offers(registry)) == 1
    assert len(await _plays(registry)) == 1


async def test_a_refused_bet_does_not_burn_a_cooldown_slot(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not cost the player three minutes.

    This mirrors ``/roll``'s posture: the stamp is taken on a
    SUCCESSFUL create only, so an out-of-bounds bet is refused by
    ``PvpService`` and the very next well-formed command still goes
    through.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[str] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update(f"/pvp_coin {MAX_BET + 1} орёл"))
    assert not await _plays(registry)

    await dispatcher.feed_update(bot, _update("/pvp_coin 100 орёл"))

    assert "<b>190</b>" in sent[1]
    assert len(await _offers(registry)) == 1
    assert len(await _plays(registry)) == 1


# -- #1562 / #1563: the cancel button's commit point and chat scope ----


async def test_a_crash_while_editing_the_card_no_longer_unwinds_a_cancel(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1562: ``_cancel`` was the one money path here that talked first.

    ``pvp_service.cancel`` closes the offer and releases the hold, but
    nothing commits until the handler returns. The old code answered
    the callback with "cancelled" and then edited the card with the
    transaction still open, so a non-Telegram failure — a broken body,
    the process dying — rolled the cancel back: the offer went back to
    pending with the stake still held, open to any group member, while
    the user had already been told it was gone. The checkpoint makes
    the cancel survive the crash, exactly as ``_accept`` does (#1282).
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    _capture_callback(bot, monkeypatch, [], fail_on="EditMessageText")

    with pytest.raises(RuntimeError, match="synthetic"):
        await dispatcher.feed_update(bot, _cancel_update(offer_id))

    assert await _balance(registry, USER_ID) == 1_000
    [cancelled] = await _offers(registry)
    assert cancelled.status == "cancelled"


async def test_pvp_callbacks_are_ignored_outside_a_group(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1563: the two buttons were the module's only unscoped surface.

    ``with_chat_type_refusal`` walks the message observer only, so the
    router's "group-only" promise never reached its callbacks. Both
    halves are asserted: a private tap does nothing, and the same tap
    in the group still works — a filter that refused everyone would
    also satisfy the first half.
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)
    _capture_callback(bot, monkeypatch, [])

    private_cancel = make_callback_update(
        PvpCancel(offer_id=offer_id).pack(),
        user_id=USER_ID,
        first_name="Кир",
        language_code="ru",
    )
    private_accept = make_callback_update(
        PvpAccept(offer_id=offer_id).pack(),
        user_id=OPPONENT_ID,
        first_name="Оппонент",
        language_code="ru",
    )

    assert await dispatcher.feed_update(bot, private_cancel) is UNHANDLED
    assert await dispatcher.feed_update(bot, private_accept) is UNHANDLED
    assert await _balance(registry, USER_ID) == 900
    [still_open] = await _offers(registry)
    assert still_open.status == "pending"

    assert await dispatcher.feed_update(bot, _cancel_update(offer_id)) is not UNHANDLED
    assert await _balance(registry, USER_ID) == 1_000


# -- #1751: accepting is a settled play and must be capped like one ------


async def _spend_the_day(registry: Any, user_id: int) -> None:
    """Fill the 24 h window without tripping the cooldown or the hour cap.

    Two hours back is outside both the 180 s cooldown and the one-hour
    window, and comfortably inside the 24 h one — so the only cap left
    to refuse the caller is the daily one. Lifted from
    ``test_game_limit_challenges``, which does the same for ``/duel``.
    """
    stale = datetime.now() - timedelta(hours=2)  # noqa: DTZ005 — game_plays is naive local
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for _ in range(MAX_PER_DAY):
            session.add(GamePlay(user_id=user_id, game="roulette", played_at=stale))
        await session.commit()


async def test_accepting_an_offer_stamps_a_play_for_the_acceptor(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accept seat is a whole settled game, and it counted for nothing.

    ``_handle_create`` stamps the publisher, and every sibling accept
    seat on this surface stamps its acceptor — ``duel.accept_duel_challenge``,
    ``rps.accept_rps_challenge`` and ``challenge_commands._dispatch`` all
    do, and ``test_game_limit_challenges`` asserts it for each of the
    three. PvP was the one that did not: tapping ✅ ran ``hold`` on both
    seats, rolled, credited the winner, took the rake and closed the
    offer while writing no ``game_plays`` row at all.
    """
    bot, dispatcher, registry, offer_id = await _published_coin_offer(make_wired, monkeypatch)

    await dispatcher.feed_update(bot, _accept_update(offer_id))

    stamped = sorted((play.user_id, play.game) for play in await _plays(registry))
    assert stamped == [(USER_ID, "pvp_coin"), (OPPONENT_ID, "pvp_coin")]


async def test_an_accept_is_refused_once_the_acceptors_day_cap_is_spent(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exploit the missing stamp opened, stated as money.

    The caps are per-user and shared across every game
    (``GameLimitsRepo.count_since`` filters on ``user_id`` alone), so a
    player who has spent the day's 25 slots on ``/roulette`` is meant to
    be done playing. Without a check on this seat they were not: a
    willing partner posts an offer, the capped player taps ✅, and the
    match settles in full — no cooldown, no hourly cap, no daily cap,
    for as long as somebody keeps posting.

    Asserted as state rather than as refusal copy: the offer must still
    be open and BOTH wallets untouched. A refusal that still moved coins
    would pass a text assertion and fail this one.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    await _seed_wallet(registry, OPPONENT_ID, balance=1_000)
    _capture_callback(bot, monkeypatch, [])
    await dispatcher.feed_update(bot, _update("/pvp_coin 100 орёл"))
    [offer] = await _offers(registry)
    await _spend_the_day(registry, OPPONENT_ID)

    await dispatcher.feed_update(bot, _accept_update(offer.id))

    [still_open] = await _offers(registry)
    assert still_open.status == "pending"
    assert still_open.opponent_id is None
    # The creator is 100 down from publishing; the acceptor never paid.
    assert await _balance(registry, USER_ID) == 900
    assert await _balance(registry, OPPONENT_ID) == 1_000
    # And the refusal burns no slot of its own — the 25 stale roulette
    # rows are still the only thing standing against this player.
    assert [p.game for p in await _plays(registry) if p.user_id == OPPONENT_ID] == (
        ["roulette"] * MAX_PER_DAY
    )
