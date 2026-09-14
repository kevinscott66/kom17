"""End-to-end ``/give`` — the developer gate on the coin-minting command.

``/give`` is the only handler that creates coins out of nothing
(``EconomyService.credit`` with ``type="admin_give"``, no counterparty
debit), so its ``settings.bot.is_developer`` check at the top of
``handlers.admin.give.handle_give`` is load-bearing in a way no other
gate in the tree is: everything downstream of it — ``/withdraw``, P2P,
the whole payout ratio — assumes coins only enter the economy through a
paid ``purchase_*`` ledger row or this command.

Until #333 that gate was pinned by nothing. ``test_give_parse.py``
exercises the argument parser and says so; the regression scanners
missed the module entirely (``tests/regression/test_from_user_filter.py``
walked ``handlers/*.py`` non-recursively — #332 — and
``test_authorization_gates.py`` only proves the gate is *mentioned*, not
that it returns — #336). Turning ``return`` into ``pass`` on
``give.py:136`` left the whole ``tests/regression`` tree green while any
user could mint themselves coins.

Pins:

* Non-developer → ``h_give_dev_only``, and ``EconomyService.credit`` is
  never entered. The refusal text alone is not enough: a handler that
  replies *and then* credits would satisfy it.
* Same in a group — ``build_router`` deliberately sets no chat-type
  filter (``give.py:260-262``), so the gate is the only thing standing
  there too.
* Developer → the credit really does land. Without this the two
  refusal tests would still pass on a ``/give`` that had been broken
  into a no-op, and would be proving nothing.
* The courtesy DM is rendered in the RECIPIENT's language, not the
  operator's (#1506). Both halves are pinned in one case: a "fix" that
  simply switched *both* messages to the recipient's language is the
  mirror-image bug, and a one-sided assertion would bless it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from pydantic import SecretStr
from sqlalchemy import select

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.user_settings import UserSetting
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

_CALLER = 555
_TARGET = 999


async def _seed_wallet(registry: Any, user_id: int, *, balance: int = 0) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _seed_language(registry: Any, user_id: int, language: str) -> None:
    """Record an explicit ``/lang`` choice for ``user_id``.

    ``user_settings.user_id`` is a real FK onto ``users.user_id`` and
    the registry runs with ``PRAGMA foreign_keys=ON``, so the parent row
    has to be committed before the child one.
    """
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(User(user_id=user_id, first_name="T"))
        await session.commit()
        session.add(UserSetting(user_id=user_id, language=language))
        await session.commit()


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(EconomyUser, user_id)
        return None if row is None else row.balance


async def _ledger(registry: Any) -> list[Transaction]:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = await session.execute(select(Transaction).order_by(Transaction.id))
        return list(rows.scalars().all())


def _spy_credit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record every ``EconomyService.credit`` entry, then run the real one.

    Patched on the class, so it catches the call however the handler got
    hold of its service instance — the middleware builds a fresh one per
    update and the handler never names the class.
    """
    calls: list[tuple[int, int]] = []
    original = EconomyService.credit

    async def spy(self: EconomyService, user_id: int, amount: int, **kwargs: Any) -> Any:
        calls.append((user_id, amount))
        return await original(self, user_id, amount, **kwargs)

    monkeypatch.setattr(EconomyService, "credit", spy)
    return calls


@pytest.mark.parametrize("chat_type", ["private", "supergroup"])
async def test_non_developer_is_refused_and_no_coins_are_minted(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    chat_type: str,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=1),
    )
    await _seed_wallet(registry, _TARGET, balance=0)
    sent = capture_outgoing(bot)
    calls = _spy_credit(monkeypatch)

    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/give {_TARGET} 100",
            user_id=_CALLER,
            chat_type=chat_type,
            language_code="ru",
        ),
    )

    assert result is not UNHANDLED  # the handler ran; the gate refused
    assert [m["text"] for m in sent] == [t("h_give_dev_only", "ru")]
    assert calls == []
    assert await _balance(registry, _TARGET) == 0
    assert await _ledger(registry) == []


async def test_developer_mints_the_coins(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=_CALLER),
    )
    await _seed_wallet(registry, _TARGET, balance=0)
    sent = capture_outgoing(bot)
    calls = _spy_credit(monkeypatch)

    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/give {_TARGET} 100",
            user_id=_CALLER,
            chat_type="private",
            language_code="ru",
        ),
    )

    assert result is not UNHANDLED
    assert calls == [(_TARGET, 100)]
    assert await _balance(registry, _TARGET) == 100

    ledger = await _ledger(registry)
    # ``from_id`` is NULL, not the operator: this row is a MINT, and the
    # column means "the wallet that paid". #1971 — see
    # ``test_the_gift_is_not_charged_to_the_operator_who_granted_it``.
    assert [(row.type, row.amount, row.from_id, row.to_id) for row in ledger] == [
        ("admin_give", 100, None, _TARGET)
    ]

    # Receipt to the admin, then the courtesy DM to the recipient.
    assert [m["chat_id"] for m in sent] == [_CALLER, _TARGET]


async def test_recipient_dm_uses_the_recipients_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1506: the DM is addressed to the recipient, so it speaks theirs.

    ``lang`` is the ``LanguageMiddleware`` stamp for whoever typed the
    command — right for the receipt, wrong for the notice. Note the
    recipient's *wallet* row is seeded ``language="ru"`` on purpose:
    ``economy.users.language`` is stamped once by
    ``EconomyRepo.get_or_create`` under ``ON CONFLICT DO NOTHING`` and
    never refreshed by ``/lang``, so a resolver that trusted it would
    still send Russian here and this test would catch it.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=_CALLER),
    )
    await _seed_wallet(registry, _TARGET, balance=0)
    await _seed_language(registry, _TARGET, "en")
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/give {_TARGET} 100",
            user_id=_CALLER,
            chat_type="private",
            language_code="ru",
        ),
    )

    assert result is not UNHANDLED
    receipt, notice = sent
    assert receipt["chat_id"] == _CALLER
    assert receipt["text"] == t(
        "h_give_success", "ru", target=str(_TARGET), amount="100", balance="100"
    )
    assert notice["chat_id"] == _TARGET
    assert notice["text"] == t("h_give_notify", "en", amount="100")


async def test_the_gift_survives_a_failing_receipt(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1865: minted coins and their ledger row outlive a dead reply.

    ``EconomyService.credit`` writes the wallet row and its
    ``admin_give`` ledger row, and ``BaseSessionMiddleware`` commits
    only after the handler returns and rolls back on any raise
    (``middlewares/base.py:131-132``). The receipt below the credit is
    ``message.reply`` — unwrapped, and the last thing that can throw:
    a deleted command message, a bot blocked by the developer, a 429.
    ``handlers/errors.py`` classes those as benign and says nothing to
    anyone, so the operator saw neither confirmation nor error and the
    natural next move is to re-run ``/give`` — against a wallet that
    silently kept none of the first one.

    The recipient DM was already best-effort, which is why the receipt
    was the only unguarded step and why this test kills exactly it.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=_CALLER),
    )
    await _seed_wallet(registry, _TARGET, balance=0)
    sent = capture_outgoing(bot)
    calls = _spy_credit(monkeypatch)

    # Wrap the capture rather than replacing it (the sink still has to
    # record the DM), and key the failure off the recipient so the test
    # keeps its meaning if the order of the two messages changes.
    captured = bot.session.make_request

    async def failing_receipt(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "SendMessage" and method.chat_id == _CALLER:
            raise RuntimeError("the receipt never left the building")
        return await captured(_bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", failing_receipt)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/give {_TARGET} 100",
            user_id=_CALLER,
            chat_type="private",
            language_code="ru",
        ),
    )

    assert calls == [(_TARGET, 100)], "the credit this test is about never ran"
    assert await _balance(registry, _TARGET) == 100
    assert [(row.type, row.amount) for row in await _ledger(registry)] == [("admin_give", 100)]
    # Nothing was delivered at all: the receipt is the FIRST of the two
    # sends, so its failure also costs the recipient their DM. That is
    # the shape of the incident — the coins are real and every trace of
    # them the humans could see is gone — and it is why the durability
    # above has to come from the checkpoint rather than from a later
    # message happening to get through.
    assert sent == []


async def test_the_gift_is_not_charged_to_the_operator_who_granted_it(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1971: ``/give`` MINTS, so its ledger row must name no payer.

    This is #1476 one command further on. That fix stopped the referral
    kickback claiming ``from_id=buyer_id`` for coins the buyer never
    paid, and its comment enumerates the shape every other mint writes
    — "promo, roulette, rps, pvp, duel, p2p, treasury, check,
    inventory_use, message_activity". ``admin_give`` is absent from
    that list because the sweep never reached this handler.

    The readers are the same ones #1476 names, and neither filters on
    ``type``: ``window_stats`` sums ``ABS(amount) WHERE from_id ==
    user``, and ``recent`` renders any row whose ``from_id`` is the
    viewer as a minus. So every gift showed up on the operator's own
    finances panel as coins they had spent — out of a wallet that was
    never touched.

    ``give.py``'s docstring justified the attribution as "exactly what
    ``/admin_donations`` and audit queries expect". It is not:
    ``handlers/admin/donations.py`` reads the ``donations`` table and
    never opens the ledger. Traceability is not lost either — the
    granting admin's id is already in ``reason``.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=_CALLER),
    )
    await _seed_wallet(registry, _TARGET, balance=0)
    await _seed_wallet(registry, _CALLER, balance=5_000)
    capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/give {_TARGET} 100",
            user_id=_CALLER,
            chat_type="private",
            language_code="ru",
        ),
    )

    # The coins are real for the recipient and free for everyone else.
    assert await _balance(registry, _TARGET) == 100
    assert await _balance(registry, _CALLER) == 5_000

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        repo = TransactionsRepo(session)
        # Wide enough to hold whatever clock the row was stamped with.
        since = datetime(2000, 1, 1)  # noqa: DTZ001 — the column is naive
        operator = await repo.window_stats(_CALLER, since=since)
        recipient = await repo.window_stats(_TARGET, since=since)
        operator_recent = await repo.recent(_CALLER)
    assert operator.sent == 0, "the mint was charged to the operator's own cashflow"
    assert operator.tx_count == 0, "the operator is not a party to a mint"
    assert operator_recent == [], "the gift rendered as a debit on the operator's panel"
    # And the half that must NOT change: the recipient still sees it.
    assert (recipient.received, recipient.sent) == (100, 0)
