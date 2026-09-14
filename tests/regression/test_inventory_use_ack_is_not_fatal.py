"""#1955: an ack that raises must not swallow a side effect already durable.

``await callback.answer()`` has exactly one failure mode in practice —
:class:`TelegramBadRequest` on a query id Telegram has already retired
(the ack window is ~15 minutes, and a user who clicks an old card, or
a delivery the bot retried, lands past it). Everywhere that ack sits at
the END of a handler the raise is harmless: ``middlewares.base`` rolls
the update back and nothing durable is lost.

The two branches pinned here are not that shape. Both have committed
something BEFORE the ack:

* NEEDS_TITLE_INPUT parks the user in ``awaiting_title`` through
  :class:`SQLiteStorage`, which commits on its OWN connection — the
  parking survives the update's rollback. A raising ack there left the
  user in the state without ever having been asked for a title, so the
  next thing they typed, whatever it was, became their group title and
  burned the item.
* NEEDS_MODERATION reaches the ack one line after ``checkpoint()``, the
  #1277 commit that makes the unwarn + consume durable. A raising ack
  there costs the user the only card that would have told them their
  warning is gone.

So both are wrapped in ``contextlib.suppress(TelegramBadRequest)`` —
the same idiom the buy-confirm path two screens down already uses. The
suppress is deliberately narrow: anything else out of ``answer`` is our
bug and still propagates, and the last test pins that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery

from telegram_invite_bot.fsm.custom_title import CustomTitleStates
from telegram_invite_bot.handlers import shop
from telegram_invite_bot.handlers.custom_title import PENDING_ENTRY_FIELD
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.shop import InventoryUse
from telegram_invite_bot.services.inventory_use_service import UseOutcome, UseResult

if TYPE_CHECKING:
    from telegram_invite_bot.services.inventory_use_service import InventoryUseService

_USER = 42
_ENTRY = 7
_LANG = "ru"


def _expired_ack() -> TelegramBadRequest:
    """The real thing: Telegram's wording for a retired query id."""
    return TelegramBadRequest(
        method=AnswerCallbackQuery(callback_query_id="stale"),
        message="Bad Request: query is too old and response timeout expired",
    )


@dataclass
class _FakeCallback:
    """Only the surface ``handle_inventory_use`` touches."""

    answer_error: BaseException | None = None
    from_user: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(id=_USER))
    message: object | None = None
    acks: list[None] = field(default_factory=list)

    async def answer(self, *_args: object, **_kwargs: object) -> None:
        if self.answer_error is not None:
            raise self.answer_error
        self.acks.append(None)


@pytest.fixture
def edits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record ``_safe_edit`` bodies.

    Patched rather than driven for real because ``_safe_edit``
    isinstance-guards on the aiogram :class:`Message` and returns
    silently for anything else — a fake callback would make every
    assertion below vacuously pass.
    """
    seen: list[str] = []

    async def _record(_callback: object, text: str, **_kwargs: object) -> None:
        seen.append(text)

    monkeypatch.setattr(shop, "_safe_edit", _record)
    return seen


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=_USER, user_id=_USER)
    )


def _service(outcome: UseOutcome) -> InventoryUseService:
    service = SimpleNamespace(use=AsyncMock(return_value=UseResult(outcome=outcome)))
    return cast("InventoryUseService", service)


async def _run(
    callback: _FakeCallback,
    outcome: UseOutcome,
    state: FSMContext,
    *,
    checkpoint: Any = None,  # noqa: ANN401 — the handler's own param type
) -> None:
    await shop.handle_inventory_use(
        cast("Any", callback),
        InventoryUse(entry_id=_ENTRY),
        _service(outcome),
        cast("Any", object()),
        state,
        cast("Any", object()),
        cast("Any", SimpleNamespace(bot=SimpleNamespace(main_chat_id=-1001))),
        _LANG,
        checkpoint,
    )


async def test_an_expired_ack_still_asks_for_the_title(edits: list[str]) -> None:
    """The hole itself: parked in the FSM, never prompted."""
    callback = _FakeCallback(answer_error=_expired_ack())
    state = _state()

    await _run(callback, UseOutcome.NEEDS_TITLE_INPUT, state)

    assert edits == [t("h_item_custom_title_prompt", _LANG)]
    assert await state.get_state() == CustomTitleStates.awaiting_title.state
    assert (await state.get_data())[PENDING_ENTRY_FIELD] == _ENTRY


async def test_the_ack_is_still_sent_when_it_can_be(edits: list[str]) -> None:
    """Suppressing the failure must not mean dropping the call."""
    callback = _FakeCallback()

    await _run(callback, UseOutcome.NEEDS_TITLE_INPUT, _state())

    assert callback.acks == [None]
    assert edits == [t("h_item_custom_title_prompt", _LANG)]


async def test_an_expired_ack_still_shows_the_unwarn_card(
    edits: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The twin, one commit later: the checkpoint has already landed."""
    monkeypatch.setattr(shop, "_apply_unwarn", AsyncMock(return_value=True))
    checkpoint = AsyncMock()
    callback = _FakeCallback(answer_error=_expired_ack())

    await _run(callback, UseOutcome.NEEDS_MODERATION, _state(), checkpoint=checkpoint)

    checkpoint.assert_awaited_once()
    assert edits == [t("h_item_unwarn_success", _LANG)]


async def test_anything_other_than_a_bad_request_still_propagates(
    edits: list[str],
) -> None:
    """The suppress is narrow on purpose.

    ``TelegramForbiddenError``, ``TelegramRetryAfter`` and a plain bug
    in our own code are not "the query id expired", and hiding them
    here would turn a real failure into a silent one.
    """
    callback = _FakeCallback(answer_error=RuntimeError("not a bad request"))

    with pytest.raises(RuntimeError, match="not a bad request"):
        await _run(callback, UseOutcome.NEEDS_TITLE_INPUT, _state())

    assert edits == []
