"""Regression guard: a double tap on «Пропустить» escrows once (#1502).

The sell interview ends in ``handlers/p2p._create_and_render``, which
reads the FSM bag, clears it, and calls ``create_sell_order``. Those are
three separate awaits, and the gates in front of them
(``handle_sell_skip_limits``'s state check, ``handle_sell_limits_text``'s
``StateFilter``) resolve through an awaited read of their own — so two
taps a few milliseconds apart both pass the gate, both read the same
amount, and both escrow. That is the shape #777 fixed on the withdraw
confirm, in a module that had no lock at all.

Why the storage here suspends
-----------------------------
With :class:`~aiogram.fsm.storage.memory.MemoryStorage` the window does
not exist: its ``get_data`` is a coroutine that never awaits anything,
so on one event loop the read and the clear cannot be interleaved, and
clearing before the service call would be guard enough on its own. That
is NOT what production runs. ``di/providers.py:98-102`` selects
:class:`~telegram_invite_bot.fsm.sqlite_storage.SQLiteStorage` whenever
``FSM_STORAGE_BACKEND=sqlite``, whose ``get_data`` is a real ``SELECT``
over aiosqlite — a genuine suspension point between the two taps' reads.
``_YieldingStorage`` models exactly that one property and nothing else.

The guard is written against ``_create_and_render`` rather than the
dispatcher because the race is a property of that section, not of the
transport: the interleaving has to be forced, and the only place a test
can hold one caller *inside* the critical section is the service call it
makes there.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from telegram_invite_bot.handlers.p2p import _create_and_render
from telegram_invite_bot.i18n import t
from telegram_invite_bot.services.p2p_service import (
    CreateOrderOutcome,
    CreateOrderResult,
    P2pService,
)

_BOT_ID = 4242
_UID = 777001
_LANG = "ru"


class _YieldingStorage(MemoryStorage):
    """``MemoryStorage`` whose ``get_data`` suspends, as SQLite's does.

    The suspension is placed AFTER the read and the value is copied, so
    the snapshot a caller ends up holding can predate another caller's
    ``clear()`` — which is the whole shape of the race. Yielding before
    the read would model nothing: every caller would then see the state
    as of after the winner cleared it.
    """

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        data = dict(await super().get_data(key))
        await asyncio.sleep(0)
        return data


class _GatedService:
    """A ``create_sell_order`` that parks its caller until released.

    Counting calls is the point: without the lock the loser of the race
    reaches the service too, and the seller ends up with two orders for
    coins they parked once.
    """

    def __init__(self, gate: asyncio.Event) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self._gate = gate

    async def create_sell_order(self, **kwargs: object) -> CreateOrderResult:
        self.calls += 1
        self.entered.set()
        await self._gate.wait()
        return CreateOrderResult(
            outcome=CreateOrderOutcome.OK,
            order_id=1,
            price_per_com=1.5,
            total_fiat=750.0,
        )


def _context(storage: MemoryStorage) -> FSMContext:
    """A real ``FSMContext`` — the bag semantics are what is under test."""
    key = StorageKey(bot_id=_BOT_ID, chat_id=_UID, user_id=_UID)
    return FSMContext(storage=storage, key=key)


async def _render(service: _GatedService, state: FSMContext) -> tuple[str, object]:
    return await _create_and_render(
        bot_id=_BOT_ID,
        user_id=_UID,
        p2p_service=cast("P2pService", service),
        state=state,
        lang=_LANG,
        payment_methods=None,
        min_amount=None,
        max_amount=None,
    )


async def test_double_tap_creates_one_order() -> None:
    """Two overlapping taps: one escrow, one «сессия истекла» card."""
    state = _context(_YieldingStorage())
    await state.set_data({"lang": _LANG, "amount": 500, "currency": "RUB"})

    gate = asyncio.Event()
    service = _GatedService(gate)

    # Both taps are in flight before either has read the bag — the only
    # arrangement in which the un-serialised version can double-escrow.
    winner = asyncio.create_task(_render(service, state))
    loser = asyncio.create_task(_render(service, state))
    await service.entered.wait()
    # Give the second tap every chance to slip in behind the first.
    for _ in range(20):
        await asyncio.sleep(0)

    assert service.calls == 1, "the loser must not reach the service"

    gate.set()
    winner_text, winner_markup = await winner
    loser_text, loser_markup = await loser

    assert service.calls == 1
    assert winner_markup is not None
    assert loser_markup is None
    assert loser_text == t("h_p2p_session_expired", _LANG)
    assert loser_text != winner_text
    assert await state.get_data() == {}
