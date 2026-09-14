"""#1762 — the ``/check_create`` confirm card must fund exactly one check.

``handlers/checks.py::handle_check_create_confirm`` reads its FSM bag,
validates it, and *then* clears it. The read and the clear are two
separate awaits, so two taps on the same ✅ both read a populated bag
before either clears — and both fund a check. This is the identical
defect #777 fixed in ``handlers/withdraw.py``, which is why the fix is
the same ``KeyedLocks`` critical section rather than anything new.

The sibling half of this pair lives in
``tests/e2e/handlers/test_shop.py`` (#1761), where the fix is
deliberately *not* a lock: ``ShopBuyConfirm`` carries no FSM state, so
serialising two taps would only make them buy twice in sequence.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import func, select

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import Check
from telegram_invite_bot.fsm.sqlite_storage import SQLiteStorage
from telegram_invite_bot.handlers.checks import (
    CheckCreateStates,
    handle_check_create_confirm,
)
from telegram_invite_bot.keyboards.builders.checks import CheckCreateConfirm
from telegram_invite_bot.repositories.checks_repo import ChecksRepo
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.check_service import CheckService
from tests.integration.repositories._session import build_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 6, 5, 12, 0, 0)
_USER = 555
_BOT_ID = 1

#: How long the losing tap is given to prove it is *not* blocked. On
#: unfixed code it runs to completion in milliseconds and the wait
#: returns early; on fixed code it is the price of proving the tap is
#: parked on the lock, which cannot be observed any other way.
_LOSER_GRACE_SECONDS = 1.0


class _ParkingStorage(SQLiteStorage):
    """Production FSM storage that parks its first ``get_data`` caller.

    The park happens **after** ``super().get_data`` has returned, not
    before: parking first would hand the caller the bag as it looks at
    *release* time — already cleared — and the race would silently fail
    to reproduce. What has to be frozen is a coroutine holding a *stale*
    read, which is exactly the state after the real read.

    ``MemoryStorage`` cannot stand in here: its coroutines return
    without ever suspending, so two handler calls under
    ``asyncio.gather`` never interleave inside the read→clear window and
    the race is unreproducible. This is the real storage, with real
    ``aiosqlite`` I/O.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.armed = True
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        data = await super().get_data(key)
        if self.armed:
            self.armed = False
            self.parked.set()
            # Bounded so a regression in the handler can never hang the
            # suite; the test always sets ``release`` well before this.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.release.wait(), timeout=30.0)
        return data


class _FakeMe:
    username = "kom17bot"


class _FakeBot:
    """Enough of ``Bot`` for ``_bot_username``, the lock key, and the DM.

    ``send_message`` is here because of #2011: the receipt for a funded
    check falls back to a direct message when the card is unusable, and
    the card here is deliberately absent (see :class:`_FakeCallback`).
    Recording the sends also keeps this test honest about the thing it
    is named for — one funded check must produce one receipt, not two.
    """

    id = _BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def me(self) -> _FakeMe:
        return _FakeMe()

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        self.sent.append((chat_id, text))


class _FakeCallback:
    """Enough of ``CallbackQuery`` for the confirm handler.

    ``message`` is ``None`` on purpose: nothing here has to fake a card
    render, so the assertions stay on the money. Since #2011 that is no
    longer the same as *no delivery* — a receipt with no card to live on
    goes out as a DM through :class:`_FakeBot`, which is exactly the
    path a double tap must not walk twice.
    """

    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.message = None
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **_: object) -> None:
        self.answers.append((text, show_alert))


@pytest.fixture
async def economy(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield session


@pytest.fixture
async def parking_storage(tmp_path: Path) -> AsyncIterator[_ParkingStorage]:
    storage = _ParkingStorage(tmp_path / "fsm.db")
    try:
        yield storage
    finally:
        await storage.close()


async def test_double_tap_on_the_check_confirm_card_funds_one_check(
    economy: AsyncSession,
    parking_storage: _ParkingStorage,
) -> None:
    """#1762: two ✅ taps on one summary card must fund ONE check.

    Timing is driven, not raced: the first tap is frozen holding a stale
    read of the FSM bag, the second is then let run as far as it can,
    and only afterwards is the first released. Unfixed, the second tap
    creates and clears while the first sits on its stale copy, and the
    release produces a second funded check — a 200-coin card that cost
    400. Fixed, the second tap cannot enter the critical section while
    the first holds it, and on entry re-reads the consumed bag.
    """
    repo = EconomyRepo(economy)
    await repo.get_or_create(_USER, now=_NOW)
    await repo.set_balance(_USER, 1000)
    await economy.commit()

    service = CheckService(
        ChecksRepo(economy),
        EconomyRepo(economy),
        TransactionsRepo(economy),
        economy,
    )
    key = StorageKey(bot_id=_BOT_ID, chat_id=_USER, user_id=_USER)
    state = FSMContext(storage=parking_storage, key=key)
    await state.set_state(CheckCreateStates.awaiting_confirm)
    await state.set_data({"lang": "ru", "ctype": "fixed", "fixed_amount": 100, "max_claims": 2})

    bot: Any = _FakeBot()
    first, second = _FakeCallback(_USER), _FakeCallback(_USER)
    payload = CheckCreateConfirm(owner_id=_USER)

    async def tap(callback: _FakeCallback) -> None:
        await handle_check_create_confirm(callback, payload, bot, service, state)  # type: ignore[arg-type]

    winner = asyncio.create_task(tap(first))
    await parking_storage.parked.wait()
    loser = asyncio.create_task(tap(second))
    # Unfixed, the loser runs to completion inside this window. Fixed,
    # it is still parked on the lock the winner holds.
    await asyncio.wait({loser}, timeout=_LOSER_GRACE_SECONDS)
    parking_storage.release.set()
    await asyncio.gather(winner, loser)

    created = await economy.scalar(select(func.count()).select_from(Check))
    assert created == 1, "the confirm card funded a second check on the re-tap"
    wallet = await EconomyRepo(economy).get(_USER)
    assert wallet is not None
    assert wallet.balance == 800, "the loser tap debited a second 200-coin check"
    assert len(bot.sent) == 1, (
        "one funded check owes the creator exactly one receipt; the loser"
        f" tap sent a second code the user cannot have been charged for: {bot.sent}"
    )
