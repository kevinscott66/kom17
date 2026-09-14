"""#1967: four money refusals held ``economy.db`` across the reply.

The pattern and its cure are already named in this repo four times —
``/daily`` RACE_LOST (#1861), ``couple_activities`` (#1869), ``/fine``
(#1879) and ``withdraw_service`` (#776). What follows is the same
mechanism at the four sites the earlier passes did not reach.

``db/engines.py`` promotes the connection to ``BEGIN IMMEDIATE`` on the
first write-headed statement, and ``savepoint`` is deliberately absent
from ``_NON_WRITE_HEADS`` so a ``begin_nested()`` counts too. A GUARDED
update that matches ZERO rows is still write-headed: the refusal costs
nothing in the wallet and everything in lock time. Left alone the
transaction stays open until the middleware commits after the handler
returns — so ``economy.db``, the busiest file in the bot, is held for a
Telegram round trip while SQLite's ``busy_timeout`` is five seconds.

Every case here is the LOST RACE, not the cheap pre-check: the wallet
the handler read said the bet was affordable and the SQL guard was the
one that said no. That is reproduced the way it actually happens — the
row on disk is poorer than the wallet this command read — by inflating
what ``EconomyRepo.get`` reports while leaving the row alone. The cheap
pre-check reaches the same reply without writing anything, and there
the added checkpoint is a no-op, exactly as ``/daily``'s is for a plain
COOLDOWN.

Durability cannot show any of this — there is nothing to be durable
about — so the probe is the lock itself: a second connection must be
able to take ``BEGIN IMMEDIATE`` while the handler is mid-send.

#1968 adds the shop's two buy surfaces to the same file, because it is
the same defect with a different disguise. ``PurchaseService.purchase``
has no affordability read at all — it goes straight to the guarded
debit — so ``INSUFFICIENT_FUNDS`` is ALWAYS the zero-row write, never a
cheap pre-check. Its sibling refusals are already clean and stay in the
tests below as the control: ``ITEM_NOT_FOUND`` and the pre-check
``OUT_OF_STOCK`` never write, and the stock-race ``OUT_OF_STOCK``
rolls the session back itself (``purchase_service.py:176``).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import update

from telegram_invite_bot.core.entities.wallet import Wallet
from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, ShopItem
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.keyboards.builders import ShopBuyConfirm
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot
    from aiogram.types import Update

    from tests.e2e.handlers.conftest import WiredFactory

GROUP_CHAT_ID = -1009
USER_ID = 909
RECIPIENT_ID = 910
SHOP_USER_ID = 911

# Poor on disk, rich to whoever asks: the gap is what turns each
# command's guarded UPDATE into a zero-row write.
_REAL_BALANCE = 10
_BET = 100


def _update(text: str, *, chat_type: str, chat_id: int) -> Update:
    return make_message_update(
        text,
        user_id=USER_ID,
        chat_id=chat_id,
        chat_type=chat_type,
        first_name="Лок",
        language_code="ru",
    )


# ``(id, command, chat_type, chat_id)`` — the four handlers that reply
# to a lost money race. ``/pvp_dice`` and ``/roll`` are the twins of
# ``/pvp_coin`` and ``/flip``; one of each pair is enough to pin the
# shape, and both twins go through the same refusal branch, so the same
# edit covers them. ``/flip`` is the one spelt out here because ``/roll``
# spins a real Telegram dice mid-handler (``answer_dice``) and that call
# is not part of this file's outbound surface.
_CASES = [
    ("roulette", f"/roulette {_BET}", "supergroup", GROUP_CHAT_ID),
    ("pvp", f"/pvp_coin {_BET} орёл", "supergroup", GROUP_CHAT_ID),
    ("flip", f"/flip {_BET} орёл", "supergroup", GROUP_CHAT_ID),
    ("send", f"/send {RECIPIENT_ID} {_BET}", "private", USER_ID),
]


async def _seed(registry: Any) -> None:  # noqa: ANN401 — EngineRegistry, TYPE_CHECKING only
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=USER_ID, balance=_REAL_BALANCE, language="ru"))
        session.add(EconomyUser(user_id=RECIPIENT_ID, balance=0, language="ru"))
        await session.commit()


@pytest.mark.parametrize(
    ("case", "command", "chat_type", "chat_id"), _CASES, ids=[c[0] for c in _CASES]
)
async def test_a_lost_money_race_releases_the_write_lock_before_replying(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    command: str,
    chat_type: str,
    chat_id: int,
) -> None:
    """The refusal reply must not be sent under ``BEGIN IMMEDIATE``."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase, ModerationBase])
    await _seed(registry)
    sessionmaker = registry.session(DBName.ECONOMY)

    original_get = EconomyRepo.get

    async def rich_get(self: EconomyRepo, user_id: int) -> Wallet | None:
        """What the losing command saw: a balance the row no longer has."""
        wallet = await original_get(self, user_id)
        if wallet is None or user_id != USER_ID:
            return wallet
        return replace(wallet, balance=wallet.balance + _BET * 100)

    monkeypatch.setattr(EconomyRepo, "get", rich_get)

    probe: list[str] = []
    sent = capture_outgoing(bot)
    original_request = bot.session.make_request

    async def probing(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ANN401, ASYNC109
        if not isinstance(method, SendMessage):
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

    await dispatcher.feed_update(bot, _update(command, chat_type=chat_type, chat_id=chat_id))

    assert sent, f"{case}: no reply was sent — the refusal branch was not reached"
    assert probe == ["free"], f"{case}: economy.db was still locked during the send: {probe}"

    # And the guard did its job: the losing command moved no money.
    async with sessionmaker() as session:
        row = await session.get(EconomyUser, USER_ID)
    assert row is not None
    assert row.balance == _REAL_BALANCE


# --- #1968: the shop's two buy surfaces ------------------------------

_ITEM_ID = 1
_PRICE = 100


async def _seed_shop(registry: Any, *, balance: int, stock: int = 3) -> None:  # noqa: ANN401
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=SHOP_USER_ID, balance=balance, language="ru"))
        session.add(ShopItem(id=_ITEM_ID, name="Дорого", price=_PRICE, type="unwarn", stock=stock))
        await session.commit()


def _probe_on(bot: Any, sessionmaker: Any, kinds: tuple[type, ...]) -> list[str]:  # noqa: ANN401
    """Wrap the (already stubbed) session so the listed outbound methods
    each try to take ``BEGIN IMMEDIATE`` from a second connection first.

    Wrapping rather than replacing keeps whichever capture fixture the
    test used in charge of what a call returns.
    """
    probe: list[str] = []
    original_request = bot.session.make_request

    async def probing(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ANN401, ASYNC109
        if not isinstance(method, kinds):
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

    bot.session.make_request = probing
    return probe


async def test_buy_refusal_releases_the_write_lock(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/buy`` on an unaffordable item must not reply under the lock."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_shop(registry, balance=_PRICE // 2)
    sessionmaker = registry.session(DBName.ECONOMY)

    sent = capture_outgoing(bot)
    probe = _probe_on(bot, sessionmaker, (SendMessage,))

    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/buy {_ITEM_ID}",
            user_id=SHOP_USER_ID,
            chat_id=SHOP_USER_ID,
            chat_type="private",
            first_name="Лок",
            language_code="ru",
        ),
    )

    assert sent, "/buy sent no refusal"
    assert probe == ["free"], f"/buy replied with economy.db locked: {probe}"


async def test_shop_confirm_refusal_releases_the_write_lock(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Same for the inline card, which edits instead of sending.

    The success branch of this handler deliberately keeps the
    transaction open so an undeliverable receipt can be rolled back
    (``shop.py``). That argument is about a purchase that HAPPENED; on
    this branch the debit matched zero rows, so there is nothing to
    refund and nothing to protect — only lock time.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_shop(registry, balance=_PRICE // 2)
    sessionmaker = registry.session(DBName.ECONOMY)

    sent = capture_callback_outgoing(bot)
    probe = _probe_on(bot, sessionmaker, (EditMessageText,))

    await dispatcher.feed_update(
        bot, make_callback_update(ShopBuyConfirm(item_id=_ITEM_ID).pack(), user_id=SHOP_USER_ID)
    )

    assert [m for m in sent if m["kind"] == "edit"], "no card was rendered"
    assert probe == ["free"], f"the buy card was edited with economy.db locked: {probe}"


async def test_the_clean_shop_refusals_stay_clean(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The control: a missing item and a sold-out one never write, so
    they were never locked and must not start being.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_shop(registry, balance=_PRICE * 10, stock=0)
    sessionmaker = registry.session(DBName.ECONOMY)

    capture_outgoing(bot)
    probe = _probe_on(bot, sessionmaker, (SendMessage,))

    for command in (f"/buy {_ITEM_ID}", "/buy 4242"):
        await dispatcher.feed_update(
            bot,
            make_message_update(
                command,
                user_id=SHOP_USER_ID,
                chat_id=SHOP_USER_ID,
                chat_type="private",
                first_name="Лок",
                language_code="ru",
            ),
        )

    assert probe == ["free", "free"], f"a read-only refusal held the lock: {probe}"
