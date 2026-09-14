"""End-to-end ``/cpc`` flow (Stage 34).

Stage 34 lands the first FSM-driven flow in the strangler pipeline.
The matrix here pins:

* Parse-time rejections (no args, malformed, same player) without
  any service / FSM mutation.
* @username resolution → opponent_id lookup against UsersRepo.
* Challenge → Accept → both moves → result, with wallet conservation
  and FSM cleared on every terminal outcome.
* Decline path: FSM cleared, no escrow, both seats notified.
* Single-side move stays in ``awaiting_moves`` (race-faithful — second
  click resolves).
* Cross-user click (C clicks A vs B's challenge) is rejected silently
  with no state mutation.
* Cancel-via-/cancel during ``awaiting_acceptance`` clears the FSM
  cleanly.
* Service-level rejection at resolve (insufficient funds after a wallet
  drain between Accept and Move) lands as a typed-rejection card with
  FSM cleared, no leak.

The tests use the same WiredFactory/make_message_update/make_callback_update
fixtures as Stage 24-29 callbacks. FSM uses MemoryStorage attached by
``conftest.make_wired``; we read state via a constructed FSMContext
keyed on the challenger's (chat_id, user_id) — same shape as
``handlers/rps.py``'s ``_fsm_context_for``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from loguru import logger

from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.users import User as UserRow
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.rps import on_expire_awaiting_acceptance
from telegram_invite_bot.keyboards.builders import (
    RpsAccept,
    RpsDecline,
    RpsMoveCallback,
)
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import (
    _try_capture_send,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot, Dispatcher

    from tests.e2e.handlers.conftest import WiredFactory


# ── Seed helpers ─────────────────────────────────────────────────────


async def _seed_wallet(registry: Any, user_id: int, *, balance: int = 1_000) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _seed_user(registry: Any, *, user_id: int, username: str | None) -> None:
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(UserRow(user_id=user_id, username=username, first_name="X"))
        await session.commit()


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    return wallet.balance if wallet is not None else None


async def _set_balance(registry: Any, user_id: int, balance: int) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
        if wallet is None:
            session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        else:
            from sqlalchemy import update

            await session.execute(
                update(EconomyUser).where(EconomyUser.user_id == user_id).values(balance=balance)
            )
        await session.commit()


async def _get_state_name(bot: Bot, dispatcher: Dispatcher, challenger_id: int) -> str | None:
    """Read the challenger's FSM state out-of-band — mirrors what the
    handler does internally via ``_fsm_context_for``."""
    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=challenger_id, user_id=challenger_id),
    )
    return await state.get_state()


async def _get_state_data(bot: Bot, dispatcher: Dispatcher, challenger_id: int) -> dict[str, Any]:
    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=challenger_id, user_id=challenger_id),
    )
    return await state.get_data()


def _cpc_message(text: str, *, user_id: int = 100) -> Any:
    return make_message_update(text, user_id=user_id, first_name="C", language_code="ru")


# ── Tests ────────────────────────────────────────────────────────────


async def test_cpc_no_args_renders_usage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc"))

    assert len(sent) == 1
    assert "Использование" in sent[0]["text"]
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_username_unknown_renders_opponent_not_found(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/cpc @unknown 100`` → UsersRepo returns None → opponent_not_found
    card. No FSM state set, no outbound send to opponent."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc @unknown 100"))

    assert "Оппонент не найден" in sent[0]["text"]
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_self_rejected_no_fsm(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_user(registry, user_id=100, username="me")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 100 100"))

    assert "С собой играть нельзя" in sent[0]["text"]
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_challenge_sets_fsm_and_pings_opponent(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Happy entry: ``/cpc <id> <bet>`` sets FSM ``awaiting_acceptance``
    AND sends a message to opponent_id AND replies to challenger.
    No wallet writes at this stage (escrow at move-time)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))

    # Two outbound messages: one to opponent (challenge card), one to
    # challenger (waiting reply).
    chat_ids = {m["chat_id"] for m in sent}
    assert chat_ids == {100, 200}
    state = await _get_state_name(bot, dispatcher, 100)
    assert state == "RpsStates:awaiting_acceptance"
    data = await _get_state_data(bot, dispatcher, 100)
    # Stage 35 adds ``state_entered_at`` (sweeper deadline reference)
    # and ``opponent_accept_message_id`` (keyboard-drop target). The
    # Stage 34 contract — opponent_id and bet are present and correct
    # — is still the load-bearing part; the extra fields are
    # additive metadata.
    assert data["opponent_id"] == 200
    assert data["bet"] == 100
    assert isinstance(data["state_entered_at"], str)
    assert isinstance(data["opponent_accept_message_id"], int)
    # No wallet movement yet.
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500


async def test_cpc_happy_path_challenger_wins(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A invites B → B accepts → A plays rock, B plays scissors → A wins.
    Pins wallet conservation: A=500→600, B=500→400. FSM cleared."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    # B accepts.
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_moves"
    # A picks rock.
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="rock", chat_id=100).pack(),
            user_id=100,
            language_code="ru",
        ),
    )
    # B picks scissors → A wins (rock beats scissors).
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="scissors", chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )

    # 500 - 100 stake + 190 payout (T-020/R8 keeps 10 of the 200 pot).
    assert await _balance(registry, 100) == 590
    assert await _balance(registry, 200) == 400
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_tie_path_refunds_both(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Both pick rock → tie → both stakes refunded. Balances unchanged."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="rock", chat_id=100).pack(),
            user_id=100,
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="rock", chat_id=100).pack(),
            user_id=200,
        ),
    )

    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_decline_clears_fsm_no_escrow(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(RpsDecline(challenger_id=100, chat_id=100).pack(), user_id=200),
    )

    assert await _get_state_name(bot, dispatcher, 100) is None
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500
    # The challenger received a decline notification (chat_id=100).
    assert any(m.get("chat_id") == 100 and "отказ" in (m.get("text") or "").lower() for m in sent)


async def test_cpc_only_one_move_keeps_state_awaiting(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A moves but B hasn't yet → FSM stays in awaiting_moves; balances
    untouched. Pins that escrow doesn't fire until BOTH moves land."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="paper", chat_id=100).pack(),
            user_id=100,
        ),
    )

    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_moves"
    data = await _get_state_data(bot, dispatcher, 100)
    assert data["challenger_move"] == "paper"
    assert data.get("opponent_move") is None
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500


async def test_cpc_cross_user_click_rejected_silently(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A invites B; user C clicks Accept on the challenge → toast,
    no state mutation, FSM still awaiting_acceptance."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    pre_state = await _get_state_name(bot, dispatcher, 100)
    # Reset captured outbound to focus on the cross-user click.
    sent.clear()

    # User C (id=999) clicks B's Accept button.
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=999,
            language_code="ru",
        ),
    )

    assert await _get_state_name(bot, dispatcher, 100) == pre_state
    # Only a callback toast — no fresh send_message / edit_text to A or B.
    kinds = {m["kind"] for m in sent}
    assert kinds.issubset({"callback_answer"})


async def test_cpc_cancel_clears_fsm_during_awaiting_acceptance(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Challenger types /cancel after /cpc → FSM cleared, no escrow.

    /cancel is wired globally (handlers/cancel.py) and uses
    ``state.clear()`` — the Stage 34 handler relies on this for the
    challenger-only escape hatch (opponent must use Decline button).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"

    await dispatcher.feed_update(bot, _cpc_message("/cancel"))

    assert await _get_state_name(bot, dispatcher, 100) is None
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500


async def test_cpc_parallel_second_challenge_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A already has an in-flight /cpc; a second /cpc from A is
    rejected with the busy card. Pins the FSM-state-based busy guard
    (mirror of legacy CPCManager get_active_for_user at ``:99``)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    await _seed_wallet(registry, 300, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    sent.clear()
    await dispatcher.feed_update(bot, _cpc_message("/cpc 300 100"))

    # First parallel call is rejected with busy message — to challenger.
    assert sent
    assert "уже в игре" in sent[0]["text"].lower()
    # First FSM still intact, pointing at 200.
    data = await _get_state_data(bot, dispatcher, 100)
    assert data["opponent_id"] == 200


async def test_cpc_username_resolves_and_plays(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/cpc @bob 100`` → UsersRepo resolves to id=200 → normal flow."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_user(registry, user_id=200, username="bob")
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc @bob 100"))

    data = await _get_state_data(bot, dispatcher, 100)
    assert data["opponent_id"] == 200
    assert data["bet"] == 100


async def test_cpc_insufficient_funds_at_resolve_clears_fsm(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A invites B → B accepts → between accept and move, B's balance
    drops below the stake → resolve surfaces OPPONENT_INSUFFICIENT_FUNDS,
    FSM cleared, no leak (no escrow lands, no double-credit)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
        ),
    )
    # Drain B's wallet between Accept and the second move.
    await _set_balance(registry, 200, 0)
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="rock", chat_id=100).pack(),
            user_id=100,
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="paper", chat_id=100).pack(),
            user_id=200,
        ),
    )

    # No escrow has run — A still has 500, B still has 0.
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 0
    assert await _get_state_name(bot, dispatcher, 100) is None


# ── Stage 35: /cpc_cancel + sweeper-driven timeout expiry ────────────


async def test_cpc_cancel_no_active_match(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/cpc_cancel`` with a clean FSM → ``cancel_no_active`` card.
    No outbound to anyone else, no state mutation. Pins idempotency:
    typing /cpc_cancel without an in-flight match is harmless."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc_cancel"))

    assert len(sent) == 1
    assert "нет активной партии" in sent[0]["text"].lower()
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_cancel_during_awaiting_acceptance(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Challenger /cpc_cancel after the challenge card hit the
    opponent. Pins: FSM cleared, BOTH players notified, opponent's
    accept-keyboard dropped via edit_message_reply_markup, no
    wallet movement (no escrow has run yet)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"
    sent.clear()

    # Patch the bot to also record EditMessageReplyMarkup — the
    # default capture_callback_outgoing rejects it. Done inline to
    # keep the shared fixture narrow.
    extra_calls: list[dict[str, Any]] = []
    original_make_request = bot.session.make_request

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "EditMessageReplyMarkup":
            extra_calls.append(
                {
                    "kind": "edit_markup",
                    "chat_id": method.chat_id,
                    "message_id": method.message_id,
                }
            )
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    await dispatcher.feed_update(bot, _cpc_message("/cpc_cancel"))

    assert await _get_state_name(bot, dispatcher, 100) is None
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500
    # Both sides got a notification.
    chat_ids = {m["chat_id"] for m in sent if m.get("kind") == "text"}
    assert 100 in chat_ids  # challenger got cancel confirmation
    assert 200 in chat_ids  # opponent got cancel notice
    # Opponent's accept-keyboard was dropped.
    assert any(c["chat_id"] == 200 for c in extra_calls)


async def test_cpc_cancel_during_awaiting_moves(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Challenger /cpc_cancel after opponent accepted but before
    either side moved. Pins: FSM cleared in awaiting_moves, both
    notified, no escrow (RpsService.play never called)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
        ),
    )
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_moves"
    sent.clear()

    # Patch in EditMessageReplyMarkup tolerance.
    original_make_request = bot.session.make_request

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "EditMessageReplyMarkup":
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    await dispatcher.feed_update(bot, _cpc_message("/cpc_cancel"))

    assert await _get_state_name(bot, dispatcher, 100) is None
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500
    chat_ids = {m["chat_id"] for m in sent if m.get("kind") == "text"}
    assert {100, 200}.issubset(chat_ids)


async def test_cpc_cancel_after_resolve_returns_no_active(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A full match plays out → FSM cleared by resolution → a /cpc_cancel
    afterwards lands as ``cancel_no_active``. Pins that /cpc_cancel
    doesn't accidentally re-cancel a finished match."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="rock", chat_id=100).pack(),
            user_id=100,
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=100, move="rock", chat_id=100).pack(),
            user_id=200,
        ),
    )
    assert await _get_state_name(bot, dispatcher, 100) is None

    # Switch to a fresh narrow capture for the /cpc_cancel surface so
    # we don't accumulate the resolve-card sends.
    from tests.e2e.handlers.conftest import _try_capture_send

    fresh_sink: list[dict[str, Any]] = []
    original_make_request = bot.session.make_request

    async def fresh(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        out = _try_capture_send(method, fresh_sink)
        if out is None:
            return await original_make_request(_bot, method, timeout=timeout)
        return out

    bot.session.make_request = fresh  # type: ignore[method-assign,assignment]

    await dispatcher.feed_update(bot, _cpc_message("/cpc_cancel"))

    assert any(
        m.get("chat_id") == 100 and "нет активной партии" in (m.get("text") or "").lower()
        for m in fresh_sink
    )


async def test_sweeper_expires_awaiting_acceptance(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Drive an in-flight ``awaiting_acceptance`` match through one
    sweep pass with a clock-mock that pushes past the 60s budget.
    Pins: FSM cleared, both seats notified, opponent's accept
    keyboard dropped. The handler-side ``state_entered_at`` stamp is
    what the sweeper measures against — without that stamp the sweep
    is a no-op (covered separately in the unit suite)."""
    from datetime import UTC, datetime, timedelta

    from telegram_invite_bot.app import _rps_timeout_rules
    from telegram_invite_bot.scheduler import FsmTimeoutSweeper

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_outgoing(bot)

    # Set up the match.
    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"
    sent.clear()

    # Widen the capture so the sweeper's edit_message_reply_markup
    # call doesn't trip the AssertionError in the narrow fixture.
    original_make_request = bot.session.make_request
    edit_markup_calls: list[int] = []

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "EditMessageReplyMarkup":
            edit_markup_calls.append(method.chat_id)
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    # Build a sweeper that thinks the wall clock is 120s past the
    # state's entered_at — every awaiting_acceptance is therefore
    # expired in a single pass.
    future = datetime.now(UTC) + timedelta(seconds=120)
    sweeper = FsmTimeoutSweeper(
        storage=dispatcher.storage,
        bot=bot,
        rules=_rps_timeout_rules(),
        clock=lambda: future,
    )
    report = await sweeper.sweep_once()

    assert report.scanned == 1
    assert report.expired == 1
    assert report.errors == 0
    assert await _get_state_name(bot, dispatcher, 100) is None
    # Both seats received a timeout notification (text sends to 100 and 200).
    chat_ids = {m["chat_id"] for m in sent if m.get("kind") == "text"}
    assert {100, 200}.issubset(chat_ids)
    # Opponent's accept keyboard was dropped.
    assert 200 in edit_markup_calls


async def test_sweeper_expires_awaiting_moves(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Same as the accept-stage test but driven through the move
    stage. Pins: sweep clears awaiting_moves cleanly with no escrow
    (RpsService.play not called)."""
    from datetime import UTC, datetime, timedelta

    from telegram_invite_bot.app import _rps_timeout_rules
    from telegram_invite_bot.scheduler import FsmTimeoutSweeper

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
        ),
    )
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_moves"
    sent.clear()

    original_make_request = bot.session.make_request

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "EditMessageReplyMarkup":
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    future = datetime.now(UTC) + timedelta(seconds=120)
    sweeper = FsmTimeoutSweeper(
        storage=dispatcher.storage,
        bot=bot,
        rules=_rps_timeout_rules(),
        clock=lambda: future,
    )
    report = await sweeper.sweep_once()

    assert report.scanned == 1
    assert report.expired == 1
    assert await _get_state_name(bot, dispatcher, 100) is None
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500
    chat_ids = {m["chat_id"] for m in sent if m.get("kind") == "text"}
    assert {100, 200}.issubset(chat_ids)


async def test_cpc_bet_below_min_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bet < cfg.min_bet=10 → invalid_bet card, no FSM."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 5"))

    assert sent
    assert "диапазоне" in sent[0]["text"]
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_two_groups_same_user_independent_fsms(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """R-FIX-008 regression: same challenger issues ``/cpc`` in two
    different group chats. Each (chat_id, user_id) pair must own an
    independent FSM slot. Before the fix, both /cpc shared a single
    StorageKey(chat_id=user_id, user_id=user_id) slot — the second
    would either overwrite the first or be rejected as "busy"."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    await _seed_wallet(registry, 300, balance=500)
    capture_outgoing(bot)

    update_g1 = make_message_update(
        "/cpc 200 100",
        user_id=100,
        chat_id=-1001,
        chat_type="supergroup",
        first_name="C",
        language_code="ru",
    )
    await dispatcher.feed_update(bot, update_g1)

    update_g2 = make_message_update(
        "/cpc 300 100",
        user_id=100,
        chat_id=-1002,
        chat_type="supergroup",
        first_name="C",
        language_code="ru",
    )
    await dispatcher.feed_update(bot, update_g2)

    # Two independent FSM slots — one per group chat.
    state_g1 = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=-1001, user_id=100),
    )
    state_g2 = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=-1002, user_id=100),
    )
    assert await state_g1.get_state() == "RpsStates:awaiting_acceptance"
    assert await state_g2.get_state() == "RpsStates:awaiting_acceptance"

    data_g1 = await state_g1.get_data()
    data_g2 = await state_g2.get_data()
    assert data_g1["opponent_id"] == 200
    assert data_g2["opponent_id"] == 300

    # The buggy slot — keyed on (user_id, user_id) — must be empty.
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_concurrent_moves_resolve_once(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """R-FIX-009 regression: TOCTOU race between two move callbacks.

    Before the fix, both clicks read the FSM, both saw the other side
    as ``None``, both stamped, both reached ``rps_service.play`` →
    double escrow + double payout. With the per-match asyncio.Lock,
    the second click sees ``challenger_move``/``opponent_move``
    already set (or state cleared) and bails. Wallet conservation
    holds: A=500→600, B=500→400 (single resolution), NOT A=700/B=300.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_moves"

    # Fire both move callbacks in parallel. ``rock`` vs ``scissors``
    # → challenger wins.
    move_a = make_callback_update(
        RpsMoveCallback(challenger_id=100, move="rock", chat_id=100).pack(),
        user_id=100,
        language_code="ru",
    )
    move_b = make_callback_update(
        RpsMoveCallback(challenger_id=100, move="scissors", chat_id=100).pack(),
        user_id=200,
        language_code="ru",
    )
    await asyncio.gather(
        dispatcher.feed_update(bot, move_a),
        dispatcher.feed_update(bot, move_b),
    )

    # Single resolution — balances reflect ONE play(), not two.
    # 500 - 100 stake + 190 payout (T-020/R8 keeps 10 of the 200 pot).
    assert await _balance(registry, 100) == 590
    assert await _balance(registry, 200) == 400
    assert await _get_state_name(bot, dispatcher, 100) is None


# ── R-FIX-009-fp lock lifecycle on terminal paths ────────────────────


async def test_cpc_decline_releases_match_lock(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """R-FIX-009-fp: the decline path runs under the per-match lock and
    leaves no registry slot behind. A leaked slot would sit there until
    the process restarts, and enough of them is the #73 growth class.
    The registry frees itself (:class:`KeyedLocks` refcounts), so this
    asserts the property rather than the drop that used to implement
    it."""
    from telegram_invite_bot.handlers.rps import _match_locks

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(RpsDecline(challenger_id=100, chat_id=100).pack(), user_id=200),
    )

    assert len(_match_locks) == 0


async def test_cpc_cancel_releases_match_lock(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """R-FIX-009-fp: /cpc_cancel is terminal — no slot survives it,
    mirroring the decline path."""
    from telegram_invite_bot.handlers.rps import _match_locks

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(bot, _cpc_message("/cpc_cancel"))

    assert len(_match_locks) == 0


# ── R-FIX-008-fp legacy callback compat (deploy window) ──────────────


async def test_cpc_legacy_accept_payload_resolves_via_message_chat(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """R-FIX-008-fp: a button rendered before the deploy carries the
    OLD payload ``rps_acc:<challenger>:<bet>`` (no ``chat_id`` field).
    The legacy fallback handler derives ``chat_id`` from
    ``callback.message.chat.id`` and routes to the canonical handler —
    state flips to ``awaiting_moves`` exactly like the new-format path.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    # Seed FSM at the legacy-derived key (chat_id = opponent's private
    # chat = opponent's user_id, since make_callback_update wires chat
    # to from_user.id for callbacks).
    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=200, user_id=100),
    )
    await state.set_state("RpsStates:awaiting_acceptance")
    await state.update_data(opponent_id=200, bet=100)

    # OLD wire-format: 2 colons (prefix + 2 fields), no chat_id.
    await dispatcher.feed_update(
        bot, make_callback_update("rps_acc:100:100", user_id=200, language_code="ru")
    )
    assert await state.get_state() == "RpsStates:awaiting_moves"


async def test_cpc_legacy_decline_payload_clears_fsm(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """R-FIX-008-fp: legacy ``rps_dec:<challenger>`` payload (1 colon)
    is recognised and clears the FSM via the canonical decline handler.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=200, user_id=100),
    )
    await state.set_state("RpsStates:awaiting_acceptance")
    await state.update_data(opponent_id=200, bet=100)

    # OLD wire-format: 1 colon.
    await dispatcher.feed_update(
        bot, make_callback_update("rps_dec:100", user_id=200, language_code="ru")
    )
    assert await state.get_state() is None


async def test_cpc_new_and_legacy_formats_dispatch_disjointly(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """R-FIX-008-fp: the new-format CallbackData filter and the legacy
    field-count filter are disjoint — a NEW-shaped payload routes to
    the canonical handler (uses payload's chat_id, NOT message.chat.id)
    and a LEGACY-shaped payload routes to the fallback. Pins that the
    two registrations don't double-fire on the same update.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    # NEW format: chat_id=100 inside the payload, message.chat.id=200.
    # If the legacy path fired, FSM would be looked up at chat_id=200
    # and the click would mis-resolve. The new path MUST win.
    new_state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=100, user_id=100),
    )
    await new_state.set_state("RpsStates:awaiting_acceptance")
    await new_state.update_data(opponent_id=200, bet=100)

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )
    assert await new_state.get_state() == "RpsStates:awaiting_moves"


async def test_cpc_accept_payload_bet_mismatch_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """M-G-1: a forged Accept payload carrying a different bet than the
    FSM-stored bet must be silently rejected — no state flip, no money
    movement. Pins the tamper-guard on
    :func:`handle_rps_accept`.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    capture_callback_outgoing(bot)

    # Challenger issues /cpc with bet=100.
    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"

    # Forged Accept carrying bet=1 instead of 100. The payload's bet
    # field diverges from the FSM-stored stake — must be rejected.
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=1, chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )

    # State stays in awaiting_acceptance (no flip), balances untouched.
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500


async def test_cpc_accept_opponent_already_in_other_match_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """M-G-2: Bob is already the recorded opponent of Alice's pending
    /cpc match. Carol issues a fresh /cpc to Bob; Bob attempts to
    accept Carol's challenge. The accept must be rejected, leaving
    Carol's FSM stuck in awaiting_acceptance (Carol's match is not
    flipped to awaiting_moves) and Alice's match untouched.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)  # Alice
    await _seed_wallet(registry, 200, balance=500)  # Bob
    await _seed_wallet(registry, 300, balance=500)  # Carol
    capture_callback_outgoing(bot)

    # Alice -> Bob.
    await dispatcher.feed_update(
        bot, make_message_update("/cpc 200 100", user_id=100, language_code="ru")
    )
    # Carol -> Bob (a second, parallel challenge).
    await dispatcher.feed_update(
        bot, make_message_update("/cpc 200 100", user_id=300, language_code="ru")
    )
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"
    assert await _get_state_name(bot, dispatcher, 300) == "RpsStates:awaiting_acceptance"

    # Bob tries to accept Carol's challenge while still listed as
    # Alice's opponent. The guard rejects the flip.
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=300, bet=100, chat_id=300).pack(),
            user_id=200,
            language_code="ru",
        ),
    )

    # Carol's match stayed in acceptance; Alice's untouched.
    assert await _get_state_name(bot, dispatcher, 300) == "RpsStates:awaiting_acceptance"
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"
    # No money moved.
    assert await _balance(registry, 200) == 500


async def test_cpc_from_the_opponent_seat_of_another_match_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1517, creation path: an ACCEPTOR is busy too.

    A match lives on the challenger's key alone (``fsm/rps.py``,
    "Variant A"), so Bob — the recorded ``opponent_id`` of Alice's
    match — has an empty key of his own and used to walk straight
    through the ``prior is not None`` check into a second match.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)  # Alice
    await _seed_wallet(registry, 200, balance=500)  # Bob
    await _seed_wallet(registry, 300, balance=500)  # Carol
    sent = capture_outgoing(bot)

    # Alice -> Bob. Bob's own key stays empty; only Alice's carries the match.
    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    assert await _get_state_name(bot, dispatcher, 200) is None
    sent.clear()

    # Bob now tries to open his own match against Carol.
    await dispatcher.feed_update(
        bot, make_message_update("/cpc 300 100", user_id=200, language_code="ru")
    )

    assert sent
    assert "уже в игре" in sent[0]["text"].lower()
    # No second match was created, and Alice's is untouched.
    assert await _get_state_name(bot, dispatcher, 200) is None
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"
    assert (await _get_state_data(bot, dispatcher, 100))["opponent_id"] == 200


async def test_cpc_accept_by_the_challenger_of_another_match_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1517, accept path: the clicker's OWN match counts.

    The M-G-2 scan matched ``opponent_id`` only, so a clicker who is
    the CHALLENGER of another live match read as free — and nothing
    else caught it, because the accept handler opens the key of the
    match being accepted, never the clicker's own.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)  # Alice
    await _seed_wallet(registry, 200, balance=500)  # Bob
    await _seed_wallet(registry, 400, balance=500)  # Dave
    capture_callback_outgoing(bot)

    # Bob -> Dave: Bob is the CHALLENGER here, so the match sits on Bob's key.
    await dispatcher.feed_update(
        bot, make_message_update("/cpc 400 100", user_id=200, language_code="ru")
    )
    # Alice -> Bob: Alice is free, so this one is allowed to open.
    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    assert await _get_state_name(bot, dispatcher, 200) == "RpsStates:awaiting_acceptance"
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"

    # Bob accepts Alice's challenge while his own match is still live.
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )

    # Neither match flipped, and no escrow was taken.
    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_acceptance"
    assert await _get_state_name(bot, dispatcher, 200) == "RpsStates:awaiting_acceptance"
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500
    assert await _balance(registry, 400) == 500


async def test_cpc_challenger_move_card_uses_challenger_locale(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """M-G-6: when the opponent accepts, the move-keyboard sent to the
    CHALLENGER must render in the challenger's locale, not in the
    accepting opponent's locale (the Telegram update for the Accept
    callback only carries the clicker's ``language_code``).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_callback_outgoing(bot)

    # Challenger (id=100) issues /cpc in RU (_cpc_message default).
    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    sent.clear()

    # Opponent (id=200) accepts in EN.
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
            language_code="en",
        ),
    )

    # The move-card sent to the challenger (chat_id=100) MUST be the RU
    # text, even though the opponent's lang was EN.
    challenger_cards = [m for m in sent if m.get("chat_id") == 100]
    assert challenger_cards, "expected a move-card sent to the challenger"
    assert challenger_cards[0]["text"] == "🎮 Сделай выбор:"
    # And the opponent's own card was rendered in EN (sanity-checks
    # that we're not accidentally forcing RU everywhere).
    opponent_cards = [m for m in sent if m.get("chat_id") == 200 and m.get("kind") == "edit"]
    assert opponent_cards, "expected an edit of the opponent's challenge card"
    assert opponent_cards[0]["text"] == "🎮 Pick your move:"


async def test_group_cpc_logs_no_warning(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A group ``/cpc`` must reach the handler without logging a warning.

    The removed ``RequireFeature("cpc")`` filter tripped its own
    "used without FeatureGateMiddleware" branch on every single
    invocation: aiogram 3 runs filters BEFORE inner middlewares, so the
    filter never saw the ``feature_gate`` that ``build_router``'s
    ``router.message.middleware(FeatureGateMiddleware())`` injects. The
    gate was inert — it returned True and the challenge went out anyway
    — so no behavioural test caught it, and prod would have logged one
    false WARNING per game.

    Asserting on the *absence* of warnings rather than on the absence of
    the class keeps this honest: any future gate that silently no-ops
    while shouting into the journal fails here too.
    """
    warnings: list[str] = []

    def _sink(record: Any) -> None:
        rec = record.record
        if rec["level"].no >= 30:  # WARNING and above
            warnings.append(f"{rec['level'].name} {rec['message']}")

    # ModerationBase on top of the usual pair: the wordfilter / antiflood
    # / rank-override readers on the message path each log a warning and
    # pass the message through when their table is missing. Creating
    # those tables is what lets this test assert on an EMPTY warning
    # list instead of on a hand-maintained allowlist of benign ones.
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase, ModerationBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)
    sent = capture_outgoing(bot)

    handler_id = logger.add(_sink, level="WARNING")
    try:
        await dispatcher.feed_update(
            bot,
            make_message_update(
                "/cpc 200 100",
                user_id=100,
                chat_id=-1001,
                chat_type="supergroup",
                first_name="C",
                language_code="ru",
            ),
        )
    finally:
        logger.remove(handler_id)

    # The command really ran — otherwise "no warnings" would be trivially
    # true for a /cpc that never got dispatched at all.
    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=-1001, user_id=100),
    )
    assert await state.get_state() == "RpsStates:awaiting_acceptance"
    assert sent, "expected the challenge card to be posted in the group"

    assert warnings == []


# ── #305: an unreachable opponent is two errors, not one ─────────────


async def test_cpc_challenge_bad_request_clears_fsm_like_forbidden(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``/cpc`` whose challenge card cannot be delivered because the
    opponent never opened a PM with the bot must land exactly where a
    blocked opponent lands: FSM cleared, apology to the challenger, no
    wallet movement.

    The distinction the handler used to miss is Telegram's, not
    aiogram's. ``TelegramForbiddenError`` means *the user blocked the
    bot* — it presupposes a private chat that once existed. A user who
    has simply never written to the bot has no such chat, and the API
    answers ``Bad Request: chat not found`` instead. That is the case
    ``h_rps_opponent_blocked_bot`` describes in so many words
    ("возможно, он не писал боту"), and it was the one that escaped.

    Escaping mattered because the FSM is set *before* the send, on
    purpose, so that this recovery branch can clear it. An escaping
    exception skipped the clear and pinned the challenger in
    ``awaiting_acceptance`` — every later ``/cpc`` answered "уже в игре",
    and the sweeper re-raised on the same unreachable id every 30s
    because :func:`~telegram_invite_bot.scheduler.fsm_sweeper.sweep_once`
    deliberately keeps state when a callback fails.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)

    sent: list[dict[str, Any]] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if getattr(method, "chat_id", None) == 200:
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        response = _try_capture_send(method, sent)
        if response is None:
            raise AssertionError(f"unexpected Telegram call: {type(method).__name__}")
        return response

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))

    assert await _get_state_name(bot, dispatcher, 100) is None
    assert [m["chat_id"] for m in sent] == [100]
    assert "не писал боту" in sent[0]["text"]
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500


async def test_expire_awaiting_acceptance_survives_unreachable_seats(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accept-stage timeout callback must not propagate when neither
    seat can be reached.

    ``FsmTimeoutSweeper`` catches whatever ``on_expire`` raises, counts
    an error and *keeps the state* so the next pass retries. For a
    transient fault that is the right posture; for a permanently
    unreachable chat it turns one lost notification into a 30-second
    loop that never converges. Both notifications are therefore
    best-effort against both unreachable shapes.
    """
    bot, _dispatcher, _registry = await make_wired(schemas=[EconomyBase, UsersBase])

    async def always_chat_not_found(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        raise TelegramBadRequest(method=method, message="Bad Request: chat not found")

    monkeypatch.setattr(bot.session, "make_request", always_chat_not_found)

    await on_expire_awaiting_acceptance(
        bot,
        StorageKey(bot_id=bot.id, chat_id=100, user_id=100),
        {"opponent_id": 200, "bet": 100, "lang": "ru", "opponent_accept_message_id": 7},
    )


# ── #306: the card-edit fallback could not fall back ─────────────────


def _blocking_session(
    monkeypatch: pytest.MonkeyPatch,
    bot: Any,
    sink: list[dict[str, Any]],
    blocked_chat_id: int,
) -> dict[str, bool]:
    """Patch ``bot.session`` so that, once armed, every card surface
    aimed at ``blocked_chat_id`` fails the two ways a card can fail.

    ``EditMessageText`` raises ``Forbidden`` — the shape a group ``/cpc``
    hits when the bot has been kicked or restricted since the challenge
    was posted. The PM fallback then raises ``chat not found``, which is
    what Telegram answers for an opponent who has never opened a private
    chat with the bot. Everything aimed elsewhere is recorded normally,
    so the test can assert on what the *other* seat received.

    Returns the arming switch: the ``/cpc`` that sets up the match has to
    run against a working session first.
    """
    armed = {"on": False}

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "AnswerCallbackQuery":
            return True
        if armed["on"] and name == "EditMessageText":
            raise TelegramForbiddenError(
                method=method, message="Forbidden: bot was kicked from the group chat"
            )
        if armed["on"] and getattr(method, "chat_id", None) == blocked_chat_id:
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        response = _try_capture_send(method, sink)
        if response is None:
            raise AssertionError(f"unexpected Telegram call: {name}")
        return response

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return armed


async def test_accept_with_undeliverable_opponent_card_still_arms_challenger(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An opponent whose move card cannot be delivered must not take the
    challenger's card down with it.

    ``_edit_opponent_card`` runs after ``set_state(awaiting_moves)`` and
    before the challenger's own move keyboard is sent, so an escaping
    error left the match armed with neither seat holding a keyboard —
    live for the full sweeper budget, playable by nobody. It caught only
    ``TelegramBadRequest`` on the edit, and its fallback ``send_message``
    carried no guard at all, which is the sharper half: that fallback
    DMs the opponent, and the opponent of a group challenge need never
    have opened a PM with the bot.

    ``None`` for ``opponent_move_message_id`` is the contract, not a
    swallow — ``_drop_keyboard`` already accepts it.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)

    sent: list[dict[str, Any]] = []
    armed = _blocking_session(monkeypatch, bot, sent, blocked_chat_id=200)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    armed["on"] = True
    sent.clear()

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=100, bet=100, chat_id=100).pack(),
            user_id=200,
            language_code="ru",
        ),
    )

    assert await _get_state_name(bot, dispatcher, 100) == "RpsStates:awaiting_moves"
    assert [m["chat_id"] for m in sent] == [100]
    assert "Сделай выбор" in sent[0]["text"]
    data = await _get_state_data(bot, dispatcher, 100)
    assert data["opponent_move_message_id"] is None
    assert isinstance(data["challenger_move_message_id"], int)
    # Nothing was staked yet — escrow fires at move time.
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500


async def test_decline_with_undeliverable_card_still_notifies_challenger(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decliner's own confirmation is the expendable half.

    ``decline_rps_challenge`` edits the decliner's card *before* it tells
    the challenger, so an escape there cost the challenger the one
    notification that mattered: their FSM had already been cleared, and
    the "waiting" message they were left looking at would never resolve.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=500)
    await _seed_wallet(registry, 200, balance=500)

    sent: list[dict[str, Any]] = []
    armed = _blocking_session(monkeypatch, bot, sent, blocked_chat_id=200)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    armed["on"] = True
    sent.clear()

    await dispatcher.feed_update(
        bot,
        make_callback_update(RpsDecline(challenger_id=100, chat_id=100).pack(), user_id=200),
    )

    assert await _get_state_name(bot, dispatcher, 100) is None
    assert [m["chat_id"] for m in sent] == [100]
    assert "отказ" in sent[0]["text"].lower()
    assert await _balance(registry, 100) == 500
    assert await _balance(registry, 200) == 500


# ── #1754: /cpc must read both wallets before it publishes a card ────


async def test_cpc_is_refused_when_the_challenger_cannot_cover_the_bet(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A player with nothing to lose could still put a card in front of you.

    ``/cpc`` never read a wallet. ``RpsService.play`` is the only place
    money is checked, and it runs at MOVE time — after the challenge has
    been sent, after the opponent accepted, and after the accept stamped
    a ``game_plays`` row against the opponent's shared 25-a-day budget.
    So a zero-balance account could burn a victim's whole day of games
    for free, one dead match at a time. ``handle_duel`` has read both
    wallets since it was written; this is the same read.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=0)
    await _seed_wallet(registry, 200, balance=5_000)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))

    assert [m["chat_id"] for m in sent] == [100]
    assert "недостаточно монет" in sent[0]["text"]
    assert await _get_state_name(bot, dispatcher, 100) is None


async def test_cpc_is_refused_when_the_opponent_cannot_cover_the_bet(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The other half of the same read, and the friendlier one.

    Without it the opponent is handed a card they can only lose by
    accepting: the accept spends one of their daily slots, both sides
    move, and the settlement then refuses for funds they never had.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 100, balance=5_000)
    await _seed_wallet(registry, 200, balance=10)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))

    assert [m["chat_id"] for m in sent] == [100]
    assert "оппонента недостаточно монет" in sent[0]["text"]
    assert await _get_state_name(bot, dispatcher, 100) is None
