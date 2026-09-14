"""End-to-end ``/balance`` flow: dispatcher → EconomyMiddleware → economy.db.

Validates the second-DB wiring introduced in Stage 7 — the economy
middleware is attached at the router level (not the dispatcher) so
``/start`` / ``/weather`` / ``/profile`` do not open an economy
session per update. The test exercises both that scoping (by also
sending an unrelated update) and the ``/balance`` happy path.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 25.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.utils.numbers import balance_tier_emoji, format_number
from tests.e2e.handlers.conftest import (
    assert_unknown_form_hint,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, user_id: int = 555) -> Update:
    """File-local defaults: private chat, ``Eve`` (ru). Delegates to the
    shared builder.
    """
    return make_message_update(
        text,
        user_id=user_id,
        first_name="Eve",
        language_code="ru",
    )


def _expected_card(
    *,
    balance: int,
    earned: int = 0,
    spent: int = 0,
    streak: int = 0,
    lang: str = "ru",
) -> str:
    """Render the expected /balance card via the same i18n key the
    handler uses (``h_balance_card``). Comparing full strings (not
    substrings) pins both the template wiring AND the formatting
    helpers (thin-space separators, tier emoji) in one assertion."""
    return t(
        "h_balance_card",
        lang,
        tier=balance_tier_emoji(balance),
        balance=format_number(balance),
        earned=format_number(earned),
        spent=format_number(spent),
        streak=streak,
    )


async def test_balance_seeds_wallet_and_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/balance"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    # Fresh wallet: welcome credit 100, zero tracked counters, streak 0.
    assert body.startswith(_expected_card(balance=100))

    # Wallet row was committed by the EconomyMiddleware.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(555)
    assert wallet is not None
    assert wallet.balance == 100


async def test_balance_alias_bal_works(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/bal"))
    assert result is not UNHANDLED
    assert sent[0]["text"].startswith(_expected_card(balance=100))


async def test_balance_with_args_gets_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/balance @other`` — a cross-user lookup is still not ported (#158).

    The handler declines the form exactly as before; what it no longer
    does is leave the user with nothing. "Falls through to legacy" was a
    real contract until the bridge was removed — after that it was just
    a dropped update, and "/balance @someone" looked like a dead bot
    rather than an unsupported form.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/balance @someone"))

    assert result is not UNHANDLED
    assert_unknown_form_hint(sent, command="balance")


async def test_existing_legacy_balance_is_preserved(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A wallet seeded by legacy (balance != 100) must not be clobbered."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])

    # Seed a row as legacy would (balance 4242).
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        from telegram_invite_bot.db.models.economy import EconomyUser

        session.add(EconomyUser(user_id=555, balance=4242, language="ru"))
        await session.commit()

    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/balance"))
    # ``format_number`` renders 4242 with a thin-space thousands
    # separator; full-card equality also proves the welcome credit
    # was NOT applied on top of the legacy-seeded balance.
    assert sent[0]["text"].startswith(_expected_card(balance=4242))


async def test_balance_uses_thousands_separator_and_tier_emoji(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Pin the cosmetics wired in via ``utils.numbers``: a balance large
    enough to cross the 💎 tier (≥10K) must render the diamond header
    AND the value with a thousands separator. If a future refactor of
    ``_format_balance`` drops the helper, this fails."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        from telegram_invite_bot.db.models.economy import EconomyUser

        session.add(EconomyUser(user_id=555, balance=1_234_567, language="ru"))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/balance"))
    # Full-card equality: the 💎 tier emoji and the thin-space
    # thousands separator both come from _expected_card's helpers.
    assert sent[0]["text"].startswith(_expected_card(balance=1_234_567))


# ---------------------------------------------------------------------------
# #1862 — the seeding write must not span the outgoing card
# ---------------------------------------------------------------------------


async def test_balance_seeded_wallet_survives_a_failed_card(
    make_wired: WiredFactory,
) -> None:
    """#1862: the first ``/balance`` INSERTs the wallet, and that write
    held ``economy.db``'s lock across the dashboard reads and the send.

    If the send raises, the middleware rolls the INSERT back — the
    welcome credit the user was just granted is un-granted, silently,
    because the dispatcher's errors router treats a kicked bot as a
    benign reject. The checkpoint commits the seed first.
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])

    original = bot.session.make_request

    async def kicked(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = kicked  # type: ignore[method-assign,assignment]

    await dispatcher.feed_update(bot, _update("/balance"))

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(555)
    assert wallet is not None
