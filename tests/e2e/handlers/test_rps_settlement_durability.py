"""#1665: a settled ``/cpc`` match survives a failure while announcing it.

The twin of ``test_duel_settlement_durability`` on the other of the two
money handlers that were missing a ``checkpoint()``. ``handle_rps_move``
settles the round and then sends a result card to each seat;
``BaseSessionMiddleware`` commits on handler return and rolls back on
raise, so without a checkpoint between the two, anything escaping the
announcement unwound the whole economy session — both holds, the
winner's credit, both ``bump_totals``, both ``record_game`` rows and
every ledger row — while the FSM, which is not transactional, had
already been cleared.

``_render_result_for_seat`` swallows ``TelegramBadRequest`` and
``TelegramForbiddenError`` (#305), so the realistic escape here is the
class it does not filter: a dropped connection, a 429, a bug. That is
what is injected, keyed off a spy on :meth:`RpsService.play` rather
than a call count, so the test keeps its meaning if the number of sends
in the flow changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.handlers import rps as rps_handler
from telegram_invite_bot.keyboards.builders import RpsAccept, RpsMoveCallback
from telegram_invite_bot.services.rps_service import RpsService
from tests.e2e.handlers.conftest import make_callback_update
from tests.e2e.handlers.test_rps import _balance, _cpc_message, _get_state_name, _seed_wallet

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


async def test_a_settled_cpc_round_is_not_unwound_by_a_failed_result_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    settled = False
    real_play = RpsService.play

    async def spy_play(self: RpsService, **kwargs: Any) -> Any:
        nonlocal settled
        result = await real_play(self, **kwargs)
        settled = True
        return result

    async def failing_render(**kwargs: Any) -> None:
        raise RuntimeError("the connection went away mid-announcement")

    monkeypatch.setattr(RpsService, "play", spy_play)
    monkeypatch.setattr(rps_handler, "_render_result_for_seat", failing_render)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="rock", chat_id=100).pack(),
            user_id=100,
            language_code="ru",
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="scissors", chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )

    assert settled, "the round never reached the settlement this test is about"
    # The happy path's own numbers: 500 - 100 stake + 190 payout, the
    # house keeping 10 of the 200 pot.
    assert await _balance(registry, 100) == 590
    assert await _balance(registry, 200) == 400
    # Cleared before the failure, and not transactional — which is
    # exactly why the money underneath it must not roll back.
    assert await _get_state_name(bot, dispatcher, 100) is None
