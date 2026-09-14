"""#2011: a funded check whose code the creator never sees.

``handlers/checks.py::handle_check_create_confirm`` debits the creator
and commits (#694 put the commit *before* the render so a funded check
does not hold ``economy.db``'s writer slot across Telegram I/O). The
code that check is worth is then delivered exactly once, by editing the
card the ✅ was tapped on — and that edit is best-effort by design:
``edit_card`` swallows "message to edit not found" and "message can't
be edited", and ``_edit_create_card`` returns early when the callback
carries an :class:`InaccessibleMessage` rather than a ``Message``.

Both are ordinary outcomes for a card the user left open, and neither
is recoverable. There is no "my checks" surface: ``ChecksRepo`` has no
query by ``creator_id``, and ``get_active_by_code`` needs the code the
user is trying to find. The coins are debited, the check is active, and
nobody alive knows how to claim it.

The sibling command path already knows this. ``/create_check`` ends in
``reply_or_send`` with #694's comment attached — "the check is
committed, so a lost reply target must not raise". The FSM path is the
same money with a weaker delivery, which is the whole finding: the two
create surfaces were kept identical in what they *render*
(``_create_receipt`` is shared) and drifted in whether it arrives.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, InaccessibleMessage, Message
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import Check
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


@pytest.fixture
async def economy(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield session


@pytest.fixture
async def bot() -> AsyncIterator[Bot]:
    b = Bot(token="42:TEST-token")
    try:
        yield b
    finally:
        await b.session.close()


def _install_dead_card(bot: Bot) -> list[str]:
    """Refuse every edit the way Telegram refuses a stale card.

    "message to edit not found" is one of :data:`BENIGN_EDIT_REJECTS`,
    so ``edit_card`` swallows it and answers ``False`` — the shape this
    file is about. Sends are recorded and succeed, because the question
    is never whether the chat is reachable: it is whether anything
    tries it.
    """
    delivered: list[str] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        name = type(method).__name__
        if name == "GetMe":
            return TelegramUser(id=42, is_bot=True, first_name="bot", username="kom17bot")
        if name == "EditMessageText":
            raise TelegramBadRequest(
                method=method, message="Bad Request: message to edit not found"
            )
        delivered.append(getattr(method, "text", ""))
        return Message(
            message_id=2,
            date=_NOW,
            chat=Chat(id=_USER, type="private"),
            from_user=TelegramUser(id=42, is_bot=True, first_name="bot"),
            text=getattr(method, "text", ""),
        )

    bot.session.make_request = fake_make_request  # type: ignore[assignment]
    return delivered


class _Callback:
    """Enough of ``CallbackQuery`` for the confirm handler."""

    def __init__(self, message: Any) -> None:
        self.from_user = SimpleNamespace(id=_USER)
        self.message = message
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **_: object) -> None:
        self.answers.append(text)


def _card(bot: Bot) -> Message:
    return Message(
        message_id=1,
        date=_NOW,
        chat=Chat(id=_USER, type="private"),
        from_user=TelegramUser(id=_USER, is_bot=False, first_name="creator"),
        text="summary",
    ).as_(bot)


def _inaccessible() -> InaccessibleMessage:
    """The card as aiogram models it once it is out of the bot's reach.

    Telegram sends ``date: 0`` for a message the bot may no longer act
    on. It still carries a chat, which is why it passes the router's
    ``F.message.chat.type == PRIVATE`` filter and reaches the handler —
    and why "no card" cannot be treated as "no recipient".
    """
    return InaccessibleMessage(message_id=1, date=0, chat=Chat(id=_USER, type="private"))


async def _confirm(bot: Bot, economy: AsyncSession, message: Any) -> tuple[_Callback, str]:
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
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=_USER, user_id=_USER),
    )
    await state.set_state(CheckCreateStates.awaiting_confirm)
    await state.set_data({"lang": "ru", "ctype": "fixed", "fixed_amount": 100, "max_claims": 2})

    callback = _Callback(message)
    await handle_check_create_confirm(
        callback,  # type: ignore[arg-type]
        CheckCreateConfirm(owner_id=_USER),
        bot,
        service,
        state,
    )
    check = (await economy.execute(select(Check))).scalars().one()
    return callback, check.code


async def test_a_refused_card_edit_still_delivers_the_code(economy: AsyncSession, bot: Bot) -> None:
    """The card is gone; the 200 coins are not.

    Unfixed, ``edit_card`` swallowed the refusal, the handler answered
    the toast and returned, and the only place the code existed was a
    database row nothing can look up.
    """
    delivered = _install_dead_card(bot)
    _, code = await _confirm(bot, economy, _card(bot))

    assert any(code in text for text in delivered), (
        f"the check was funded and its code {code!r} never reached the creator — "
        "the card edit was refused and nothing else tried"
    )


async def test_an_inaccessible_card_still_delivers_the_code(
    economy: AsyncSession, bot: Bot
) -> None:
    """The same money, the other way the card dies.

    ``InaccessibleMessage`` is not a ``Message``, so the render is
    skipped before Telegram is even asked — a silent ``return`` on the
    exact path that just spent the user's balance. It still names the
    chat, so there was always somewhere to send the receipt.
    """
    delivered = _install_dead_card(bot)
    _, code = await _confirm(bot, economy, _inaccessible())

    assert any(code in text for text in delivered), (
        f"the check was funded and its code {code!r} never reached the creator — "
        "the callback carried an InaccessibleMessage and the render returned early"
    )
