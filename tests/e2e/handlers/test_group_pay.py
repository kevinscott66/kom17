"""``/group_pay`` payout: the confirmation must survive a lost card.

The handler owns its own economy session and commits the payout there,
so nothing the middleware does afterwards can take the coins back. That
makes the confirmation load-bearing: an owner who is shown an error
after a successful payout runs the command again, and the treasury pays
twice. These tests pin the payout numbers against both delivery
outcomes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select, text

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, GroupDonationsAggregate
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.group_pay import handle_group_pay

CHAT_ID = -1001
OWNER_ID = 42

# The handler reads exactly one thing off ``settings``: the developer
# bypass in ``_caller_is_owner``. Granting it keeps the Telegram
# membership probe out of a test that is about the payout, not the gate.
_SETTINGS: Any = SimpleNamespace(bot=SimpleNamespace(is_developer=lambda _uid: True))


class _Message:
    """Minimal Message stand-in whose reply target is already gone."""

    def __init__(self, *, chat_gone: bool = False) -> None:
        self.from_user = SimpleNamespace(id=OWNER_ID, first_name="Owner")
        self.chat = SimpleNamespace(id=CHAT_ID, type="supergroup")
        self.text = "/group_pay 1000"
        self.reply_to_message = None
        self.entities: list[Any] = []
        self.sends: list[str] = []
        self._chat_gone = chat_gone

    async def reply(self, text: str, **_kwargs: Any) -> None:
        raise TelegramBadRequest(
            method=SimpleNamespace(),  # type: ignore[arg-type]
            message="Bad Request: message to be replied not found",
        )

    async def answer(self, text: str, **_kwargs: Any) -> None:
        if self._chat_gone:
            raise TelegramForbiddenError(
                method=SimpleNamespace(),  # type: ignore[arg-type]
                message="Forbidden: bot was blocked by the user",
            )
        self.sends.append(text)


class _Bot:
    def __init__(self) -> None:
        self.dms: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> None:
        self.dms.append((chat_id, text))


# ``rating_history`` ships in migration 0009 as a raw-SQL table (no ORM
# model), so ``create_all(EconomyBase)`` doesn't produce it — same
# fixture shape as tests/e2e/handlers/test_shop.py. The payout writes a
# snapshot row into it, so the table has to exist for the happy path.
_RATING_HISTORY_DDL = (
    "CREATE TABLE rating_history ("
    "  group_id INTEGER NOT NULL,"
    "  date TEXT NOT NULL,"
    "  total_donations INTEGER NOT NULL,"
    "  position INTEGER,"
    "  PRIMARY KEY (group_id, date)"
    ")"
)


async def _seed(registry: Any) -> None:
    async with registry.session(DBName.ECONOMY)() as session:
        await session.execute(text(_RATING_HISTORY_DDL))
        session.add(GroupDonationsAggregate(group_id=CHAT_ID, total_donations=5000))
        session.add(EconomyUser(user_id=OWNER_ID, balance=0))
        await session.commit()


async def _balances(registry: Any) -> tuple[int, int]:
    async with registry.session(DBName.ECONOMY)() as session:
        treasury = (
            await session.execute(
                select(GroupDonationsAggregate.total_donations).where(
                    GroupDonationsAggregate.group_id == CHAT_ID
                )
            )
        ).scalar_one()
        wallet = (
            await session.execute(
                select(EconomyUser.balance).where(EconomyUser.user_id == OWNER_ID)
            )
        ).scalar_one()
    return treasury, wallet


@pytest.mark.asyncio
async def test_group_pay_confirms_when_the_command_message_is_gone(
    make_wired: Any,
) -> None:
    bot, _dp, registry = await make_wired(schemas=[EconomyBase])
    await _seed(registry)
    message = _Message()

    await handle_group_pay(
        message,  # type: ignore[arg-type]
        _Bot(),  # type: ignore[arg-type]
        "ru",
        registry=registry,
        settings=_SETTINGS,
        min_withdrawal=1000,
    )

    assert len(message.sends) == 1, "the payout confirmation never reached the chat"
    assert "1000" in message.sends[0]
    assert await _balances(registry) == (4000, 1000)

    await bot.session.close()
    await registry.dispose()


@pytest.mark.asyncio
async def test_group_pay_keeps_the_payout_when_the_chat_is_unreachable(
    make_wired: Any,
) -> None:
    """The companion case: nobody sees the confirmation, and that is fine.

    The coins are in the owner's wallet and the treasury is short by
    the same amount — a raise here would only mislabel a completed
    payout as a failure, since the commit already happened.
    """
    bot, _dp, registry = await make_wired(schemas=[EconomyBase])
    await _seed(registry)
    message = _Message(chat_gone=True)

    await handle_group_pay(
        message,  # type: ignore[arg-type]
        _Bot(),  # type: ignore[arg-type]
        "ru",
        registry=registry,
        settings=_SETTINGS,
        min_withdrawal=1000,
    )

    assert message.sends == []
    assert await _balances(registry) == (4000, 1000)

    await bot.session.close()
    await registry.dispose()
