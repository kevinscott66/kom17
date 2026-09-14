"""#1665: a settled duel survives a failure while announcing it.

``handle_duel_roll`` settles the match and then edits the shared card.
``BaseSessionMiddleware`` commits on handler return and rolls back on
raise, and ``utils.aiogram.edit_card`` re-raises everything outside
``BENIGN_EDIT_REJECTS`` — it does not catch ``TelegramForbiddenError``,
``TelegramRetryAfter`` or a socket error at all. So without a
``checkpoint()`` between the two, a bot kicked from the group (or a 429,
or a dropped connection) in that window unwound the entire economy
session: both holds, the winner's credit, both ``bump_totals``, both
``record_game`` rows and every ledger row.

The coin position stays consistent either way — both stakes come back —
so this is not lost or duplicated money. What it is, is a match that was
played and decided and then silently un-happened, with the FSM (which is
not transactional) already cleared, so it cannot be replayed either.
The sibling handlers already take this posture and say so in their own
comments (``pvp_stake._accept``, ``games.handle_roll_bet``); these two
were the last money handlers without it.

The failure is injected only *after* the settlement, keyed off a spy on
:meth:`DuelService.play` rather than a call count, so the test does not
quietly change meaning when the number of card edits in the flow does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.handlers import duel as duel_handler
from telegram_invite_bot.keyboards.builders import DuelAccept, DuelRoll
from telegram_invite_bot.services.duel_service import DuelService
from telegram_invite_bot.utils.aiogram import edit_card
from tests.e2e.handlers.test_duel import (
    CHALLENGER_ID,
    GROUP_CHAT_ID,
    OPPONENT_ID,
    _balance,
    _duel_callback,
    _duel_message,
    _FakeRng,
    _get_state_name,
    _seed_wallet,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


async def test_a_settled_duel_is_not_unwound_by_a_failed_result_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)
    monkeypatch.setattr(duel_handler, "_rng", _FakeRng([6, 1]))

    settled = False
    real_play = DuelService.play

    async def spy_play(self: DuelService, **kwargs: Any) -> Any:
        nonlocal settled
        result = await real_play(self, **kwargs)
        settled = True
        return result

    # Taken from its defining module rather than off the handler:
    # the handler re-exports it, and a re-export is not an
    # explicit export under strict typing.
    real_edit = edit_card

    async def failing_edit(*args: Any, **kwargs: Any) -> Any:
        # A bare exception rather than a Telegram one on purpose: the
        # audit's own worst case is the network, and ``edit_card`` does
        # not filter by type outside its benign list.
        if settled:
            raise RuntimeError("the connection went away mid-announcement")
        return await real_edit(*args, **kwargs)

    monkeypatch.setattr(DuelService, "play", spy_play)
    monkeypatch.setattr(duel_handler, "edit_card", failing_edit)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=CHALLENGER_ID),
    )
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=OPPONENT_ID),
    )

    assert settled, "the match never reached the settlement this test is about"
    # Same numbers the happy-path test pins: 500 - 100 stake + 190
    # payout, the house keeping 10 of the 200 pot.
    assert await _balance(registry, CHALLENGER_ID) == 590
    assert await _balance(registry, OPPONENT_ID) == 400
    # The FSM was cleared before the failure and is not transactional,
    # which is precisely why the money must not roll back under it.
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )
