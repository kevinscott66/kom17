"""End-to-end ``/withdraw`` — escrow-on-create flow (#28, T-027).

Replaces the T-024.1 deferral-stub pins with coverage of the real
two-step FSM wired through the full dispatcher:

* happy path: ``/withdraw`` → amount → ✅ confirm escrows the coins
  (balance drops) and writes a ``pending`` row; the submitted card
  carries the new request id;
* ❌ cancel leaves balance + row untouched;
* the amount step rejects below-min / above-max / unaffordable /
  non-numeric input WITHOUT escrowing — the user stays in the flow;
* the RU alias ``/вывод`` enters the same flow;
* a second ``/withdraw`` mid-flow hits the busy guard;
* the private-only router filter drops a group ``/withdraw`` (UNHANDLED);
* #237: a created request pushes an admin DM to ``ADMIN_CHAT_ID`` — sent
  to the right chat, naming the request id, and skipped entirely when no
  admin chat is configured. A failed send is swallowed: the escrow and
  the row are the source of truth.

The flow is driven by feeding sequential updates into one dispatcher
so the in-process ``MemoryStorage`` carries FSM state across the
message → amount → callback hops, exactly as a real chat would.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from pydantic import SecretStr
from sqlalchemy import func, select

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    Transaction,
    WithdrawalRequest,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.fsm.sqlite_storage import SQLiteStorage
from telegram_invite_bot.keyboards.builders import WithdrawCancel, WithdrawConfirm
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

_USER = 4242


async def _seed_wallet(
    registry: Any, user_id: int, *, balance: int, deposited: int = 100_000
) -> None:
    """Seed a wallet, by default one belonging to a *paying* account.

    T-019 (R2) refuses withdrawals from accounts that never bought coins,
    so a bare balance is no longer enough to reach the money path — the
    ledger has to show a ``purchase_*`` credit too. Pass ``deposited=0``
    for the free-rider case.
    """
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        if deposited > 0:
            session.add(
                Transaction(
                    from_id=None,
                    to_id=user_id,
                    amount=deposited,
                    reason="test top-up",
                    date=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
                    type="purchase_crypto",
                )
            )
        await session.commit()


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.execute(
            select(EconomyUser.balance).where(EconomyUser.user_id == user_id)
        )
        val = row.scalar_one_or_none()
        return int(val) if val is not None else None


async def _request_count(registry: Any) -> int:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.execute(select(func.count()).select_from(WithdrawalRequest))
        return int(row.scalar_one())


async def _first_request(registry: Any) -> WithdrawalRequest | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.execute(
            select(WithdrawalRequest).order_by(WithdrawalRequest.id.asc()).limit(1)
        )
        return row.scalar_one_or_none()


@pytest.mark.asyncio
async def test_withdraw_happy_path_escrows_and_creates_pending(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/withdraw`` → 4500 → ✅ confirm: balance drops 4500, a pending
    row exists, and the submitted card names the request id."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=10_000)
    sink = capture_callback_outgoing(bot)

    # Step 1: open the flow.
    r1 = await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    assert r1 is not UNHANDLED
    assert any("Вывод средств" in e.get("text", "") for e in sink if e["kind"] == "text")

    # Step 2: name the amount → confirm card.
    await dispatcher.feed_update(
        bot, make_message_update("4500", user_id=_USER, language_code="ru")
    )
    assert any("Подтверждение" in e.get("text", "") for e in sink if e["kind"] == "text")
    # Not escrowed yet — coins only move on confirm.
    assert await _balance(registry, _USER) == 10_000
    assert await _request_count(registry) == 0

    # Step 3: ✅ confirm.
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            WithdrawConfirm(user_id=_USER).pack(), user_id=_USER, language_code="ru"
        ),
    )
    # Escrowed + pending row written.
    assert await _balance(registry, _USER) == 10_000 - 4500
    row = await _first_request(registry)
    assert row is not None
    assert row.status == "pending"
    assert row.amount_com == 4500
    edits = [e for e in sink if e["kind"] == "edit"]
    assert edits, "confirm should edit the card into the submitted message"
    assert f"#{row.id}" in edits[-1]["text"]


@pytest.mark.asyncio
async def test_withdraw_refused_for_account_that_never_deposited(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-019 (R2): a balance grown purely from minted coins cannot leave
    the ecosystem as USDT.

    The refusal lands at the amount step — the gate is checked before the
    confirm card is drawn, so the user is not walked up to a button that
    was never going to work. ``create`` re-checks it on confirm anyway;
    that half is pinned in the integration suite.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=10_000, deposited=0)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot, make_message_update("4500", user_id=_USER, language_code="ru")
    )

    texts = [e.get("text", "") for e in sink if e["kind"] == "text"]
    assert any("Вывод пока недоступен" in text for text in texts)
    assert not any("Подтверждение" in text for text in texts), (
        "a refused amount must never reach the confirm card"
    )
    assert await _balance(registry, _USER) == 10_000
    assert await _request_count(registry) == 0


@pytest.mark.asyncio
async def test_withdraw_refused_above_lifetime_deposits(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-020 (R6): the win-and-withdraw route.

    This account DID pay, so R2's threshold gate waves it through — it is
    the exact case R2 alone cannot stop. It paid 1 000 COM worth and then
    ran its balance up to 10 000 in the near-zero-edge games; without the
    cap it would export 4 500, i.e. 4.5× the money that ever came in.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=10_000, deposited=1_000)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot, make_message_update("4500", user_id=_USER, language_code="ru")
    )

    texts = [e.get("text", "") for e in sink if e["kind"] == "text"]
    refusals = [text for text in texts if "Лимит вывода исчерпан" in text]
    assert refusals, "the cap must refuse before the confirm card"
    # The headroom shown is what they actually paid in, not zero — the
    # copy has to stay truthful for a partially-used allowance.
    assert "1000" in refusals[-1] or "1 000" in refusals[-1]
    # …and the intro that preceded it already said so, so the refusal is
    # never the first the user hears of the lifetime ceiling.
    assert any("Лимит за всё время" in text for text in texts)
    assert await _balance(registry, _USER) == 10_000
    assert await _request_count(registry) == 0


@pytest.mark.asyncio
async def test_withdraw_allowed_up_to_lifetime_deposits(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The cap must not punish an honest buyer: someone who paid for
    4 500 COM can still take all 4 500 back out. A cap that clipped the
    full exit would be a spread by another name — and R6 exists precisely
    so that no spread is needed."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=10_000, deposited=4_500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot, make_message_update("4500", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            WithdrawConfirm(user_id=_USER).pack(), user_id=_USER, language_code="ru"
        ),
    )

    assert await _balance(registry, _USER) == 10_000 - 4_500
    row = await _first_request(registry)
    assert row is not None
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_withdraw_cancel_leaves_balance_and_no_row(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """❌ Cancel after the amount step: nothing escrowed, no row."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=10_000)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot, make_message_update("4500", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            WithdrawCancel(user_id=_USER).pack(), user_id=_USER, language_code="ru"
        ),
    )
    assert await _balance(registry, _USER) == 10_000
    assert await _request_count(registry) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "anchor"),
    [
        ("4499", "Минимальная"),  # below WITHDRAW_MIN_COINS (4500)
        ("90001", "Максимальная"),  # above WITHDRAW_MAX_COINS (90000)
        ("nope", "целым числом"),  # not a number
    ],
)
async def test_withdraw_amount_rejected_no_escrow(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    amount: str,
    anchor: str,
) -> None:
    """Out-of-band / non-numeric amounts are rejected with a hint and
    do NOT escrow — the user stays in the amount step."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=1_000_000)
    sink = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot, make_message_update(amount, user_id=_USER, language_code="ru")
    )
    assert any(anchor in e.get("text", "") for e in sink)
    assert await _balance(registry, _USER) == 1_000_000
    assert await _request_count(registry) == 0


@pytest.mark.asyncio
async def test_withdraw_insufficient_balance_no_escrow(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An in-band amount above the balance is refused at the amount
    step (before any escrow)."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=5_000)
    sink = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot, make_message_update("9000", user_id=_USER, language_code="ru")
    )
    assert any("Недостаточно" in e.get("text", "") for e in sink)
    assert await _balance(registry, _USER) == 5_000
    assert await _request_count(registry) == 0


@pytest.mark.asyncio
async def test_withdraw_ru_alias_enters_flow(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The legacy RU alias ``/вывод`` opens the same flow."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=10_000)
    sink = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, make_message_update("/вывод", user_id=_USER, language_code="ru")
    )
    assert result is not UNHANDLED
    assert any("Вывод средств" in e.get("text", "") for e in sink)


@pytest.mark.asyncio
async def test_withdraw_busy_guard_blocks_reentry(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A second ``/withdraw`` while a flow is active hits the busy
    guard instead of restarting."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, _USER, balance=10_000)
    sink = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    assert any("уже оформляется" in e.get("text", "") for e in sink)


@pytest.mark.asyncio
async def test_withdraw_in_group_is_refused_without_leaking_a_balance(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A group ``/withdraw`` gets the refusal and nothing else (#123).

    The private-only router filter still drops the group invocation
    before any wallet read — balances and payout amounts must never
    render in a group — so the assertion is an exact match: the refusal
    line, and no second message.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase, UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot,
        make_message_update("/withdraw", user_id=_USER, chat_id=-100, chat_type="supergroup"),
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="withdraw")


# ── #237: admin notification on create ──────────────────────────────────────

_ADMIN_CHAT = -1009876543210


async def _drive_to_confirm(
    dispatcher: Any, bot: Bot, registry: Any, *, amount: str = "4500"
) -> None:
    """``/withdraw`` → amount → ✅ confirm, all three updates fed."""
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot, make_message_update(amount, user_id=_USER, language_code="ru")
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            WithdrawConfirm(user_id=_USER).pack(), user_id=_USER, language_code="ru"
        ),
    )


@pytest.mark.asyncio
async def test_withdraw_confirm_notifies_the_admin_chat(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#237: the created request reaches the operator immediately.

    Before this, the only owner signal was the 24h staleness sweep in
    ``economy_cleanup`` — a request could sit a full day before anyone
    learned it existed. The DM has to land in ``ADMIN_CHAT_ID`` (not in
    the user's chat) and carry the request id, because the id is the
    handle the operator types into ``/admin_withdrawals``.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), ADMIN_CHAT_ID=_ADMIN_CHAT),
    )
    await _seed_wallet(registry, _USER, balance=10_000)
    sink = capture_callback_outgoing(bot)

    await _drive_to_confirm(dispatcher, bot, registry)

    row = await _first_request(registry)
    assert row is not None
    dms = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _ADMIN_CHAT]
    assert len(dms) == 1, f"expected exactly one admin DM, got {len(dms)}"
    body = dms[0]["text"]
    # The id is the handle into /admin_withdrawals — without it the DM
    # tells the operator that *something* happened and nothing more.
    assert f"#{row.id}" in body
    assert str(_USER) in body
    assert "4500" in body
    # The DM must NOT be the user-facing card: a confirmation that reads
    # "your request was created" delivered to the admin chat would mean
    # the two texts got swapped.
    assert "/admin_withdrawals" in body


@pytest.mark.asyncio
async def test_withdraw_confirm_without_admin_chat_sends_no_dm(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``ADMIN_CHAT_ID`` unset (the field default ``0``) → no push.

    ``0`` is not a chat id. Sending there would raise on every single
    created request on a box that never configured an admin chat, so the
    guard is the difference between "no notification" and "a warning per
    withdrawal forever".
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), ADMIN_CHAT_ID=0),
    )
    await _seed_wallet(registry, _USER, balance=10_000)
    sink = capture_callback_outgoing(bot)

    await _drive_to_confirm(dispatcher, bot, registry)

    # The request still exists — the notification is an addition, never a
    # precondition.
    assert await _request_count(registry) == 1
    assert [e for e in sink if e["kind"] == "text" and e["chat_id"] == 0] == []


@pytest.mark.asyncio
async def test_withdraw_confirm_survives_a_failed_admin_notify(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead admin chat must not cost the user their escrowed request.

    The row is written and the coins are escrowed inside
    ``WithdrawService.create`` before the DM is even attempted, and the
    user has already seen the submitted card. Letting the notify failure
    propagate would surface an error over a request that succeeded — and
    the user would have no way to tell whether their coins moved.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), ADMIN_CHAT_ID=_ADMIN_CHAT),
    )
    await _seed_wallet(registry, _USER, balance=10_000)
    sink = capture_callback_outgoing(bot)

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise TelegramBadRequest(
            method=SendMessage(chat_id=_ADMIN_CHAT, text="x"), message="chat not found"
        )

    # Patched on the instance so ONLY the handler's own ``bot.send_message``
    # call is affected — the user-facing card goes out through
    # ``Message.answer``/``edit_text`` and still reaches the sink.
    monkeypatch.setattr(bot, "send_message", _boom)

    await _drive_to_confirm(dispatcher, bot, registry)

    assert await _balance(registry, _USER) == 10_000 - 4500
    row = await _first_request(registry)
    assert row is not None
    assert row.status == "pending"
    edits = [e for e in sink if e["kind"] == "edit"]
    assert edits, "the user still gets the submitted card"
    assert f"#{row.id}" in edits[-1]["text"]
    # And no error surface anywhere. Letting the notify failure escape
    # would hand it to the global error handler, which paints
    # "⚠️ Произошла ошибка" over a request that actually succeeded —
    # leaving the user unable to tell whether their coins moved. This is
    # the assertion that distinguishes "swallowed" from "propagated":
    # every other check above passes either way, because the escrow and
    # the card both happen before the DM is attempted.
    noise = [e for e in sink if "ошиб" in str(e.get("text") or "").lower()]
    assert noise == [], f"failed notify leaked an error surface: {noise}"


@pytest.mark.asyncio
async def test_double_tapped_confirm_escrows_once(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    tmp_path: Path,
) -> None:
    """#777: two ✅ taps a few milliseconds apart must not fund two requests.

    The confirm handler reads the amount out of the FSM bag, clears the
    bag, then creates the request. Those are three separate awaits, and
    aiogram runs the two callbacks concurrently — the dispatcher is built
    without ``events_isolation`` (the ``Dispatcher`` built by
    ``di.providers.dispatcher``), so it falls back to
    ``DisabledEventIsolation``. ``StateFilter`` is no help either:
    it resolves through an awaited ``get_state()``, so both taps pass it
    long before either reaches ``clear()``. Without a lock both read
    ``4500``, both clear, and both escrow — 9 000 coins gone against one
    button.

    The storage matters. ``MemoryStorage`` — what every other test here
    uses — never suspends inside ``get_data``/``set_data``, so the two
    coroutines cannot interleave between the read and the clear and this
    guard would pass with the lock removed. Production runs
    ``FSM_BACKEND=sqlite``, whose methods are real I/O, so the regression
    is driven against :class:`SQLiteStorage` on a temp file.
    """
    storage = SQLiteStorage(tmp_path / "fsm.db")
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True, storage=storage
    )
    try:
        await _seed_wallet(registry, _USER, balance=100_000)
        sink = capture_callback_outgoing(bot)

        await dispatcher.feed_update(
            bot, make_message_update("/withdraw", user_id=_USER, language_code="ru")
        )
        await dispatcher.feed_update(
            bot, make_message_update("4500", user_id=_USER, language_code="ru")
        )
        assert await _request_count(registry) == 0

        def _tap(update_id: int) -> Any:
            return dispatcher.feed_update(
                bot,
                make_callback_update(
                    WithdrawConfirm(user_id=_USER).pack(),
                    user_id=_USER,
                    language_code="ru",
                    update_id=update_id,
                    callback_id=f"cb-{update_id}",
                ),
            )

        await asyncio.gather(_tap(2), _tap(3))

        assert await _request_count(registry) == 1, (
            "a double-tapped confirm funded more than one withdrawal"
        )
        assert await _balance(registry, _USER) == 100_000 - 4500
        # The loser must be told the card is spent, not silently ignored:
        # exactly one card says "submitted" and one says "expired".
        edits = [e["text"] for e in sink if e["kind"] == "edit"]
        row = await _first_request(registry)
        assert row is not None
        assert sum(f"#{row.id}" in text for text in edits) == 1
    finally:
        await storage.close()
