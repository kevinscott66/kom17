"""End-to-end ``/cancel``.

Pins:

* Clears FSM state + data when something was set.
* Idempotent — works with no prior state, no crash.
* Replies in RU by default, EN when caller's ``language_code`` is
  ``en``-ish.
* Works in BOTH private and group chats (the whole point — escape
  hatch must be available everywhere).
* Confirmation message reaches the user.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

from telegram_invite_bot.handlers import duel
from telegram_invite_bot.i18n import t
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


class _ProbeStates(StatesGroup):
    """Synthetic FSM group — proves /cancel clears arbitrary state, not
    just states the cancel handler happens to know about."""

    waiting_for_amount = State()


def _key(*, chat_id: int, user_id: int, bot_id: int = 0) -> StorageKey:
    """Default aiogram FSM key shape (bot, chat, user). The dispatcher's
    storage middleware uses the same key — tests must too, or the
    pre-seeded state won't be the one /cancel sees."""
    return StorageKey(bot_id=bot_id, chat_id=chat_id, user_id=user_id)


@pytest.mark.asyncio
async def test_clears_active_fsm_state(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    # Pre-seed a state + payload as if a withdraw flow had set them.
    storage = dispatcher.fsm.storage
    key = _key(chat_id=42, user_id=42, bot_id=bot.id if bot.id else 0)
    ctx = FSMContext(storage=storage, key=key)
    await ctx.set_state(_ProbeStates.waiting_for_amount)
    await ctx.update_data(pending_amount=1234)
    assert await ctx.get_state() == _ProbeStates.waiting_for_amount.state

    await dispatcher.feed_update(
        bot, make_message_update("/cancel", user_id=42, chat_type="private")
    )

    # Both the state name AND the data bag must be gone.
    assert await ctx.get_state() is None
    assert await ctx.get_data() == {}
    assert sent[0]["text"] == "✅ Отменено."


@pytest.mark.asyncio
async def test_idempotent_with_no_state(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """User issuing /cancel without an active flow still gets the
    confirmation — the legacy contract is "your next message won't be
    eaten by an unfinished flow", which holds whether or not there
    was one."""
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/cancel", user_id=7, chat_type="private")
    )
    assert sent[0]["text"] == "✅ Отменено."


@pytest.mark.asyncio
async def test_english_language_code(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/cancel", user_id=8, chat_type="private", language_code="en-US"),
    )
    assert sent[0]["text"] == "✅ Cancelled."


@pytest.mark.asyncio
async def test_works_in_group(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """/cancel must be reachable from a group — that's where users
    most often get stuck in a multi-step flow they started inline."""
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/cancel", user_id=9, chat_id=-100_555, chat_type="supergroup"),
    )
    assert len(sent) == 1
    assert sent[0]["text"] == "✅ Отменено."
    assert sent[0]["chat_id"] == -100_555


@pytest.mark.asyncio
async def test_case_insensitive(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """``/Cancel`` matches — the legacy command was case-insensitive
    via telebot's default; the new router uses ``ignore_case=True``
    on the filter."""
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/Cancel", user_id=10, chat_type="private")
    )
    assert sent[0]["text"] == "✅ Отменено."


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/отмена", "/Отмена"])
async def test_russian_alias_clears_state(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    text: str,
) -> None:
    """``/отмена`` is the word a stuck Russian-speaking user reaches for.

    The escape hatch was the one command in the catalog with no Russian
    alias while ``/вывод``, ``/п2п`` and ``/передать_права`` all had one
    (#163). This also pins the mechanism it relies on: aiogram reads the
    command off ``message.text``, not off Telegram's ``bot_command``
    entity — which Telegram only emits for ASCII — so a Cyrillic token
    resolves exactly like the ones already shipping.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    storage = dispatcher.fsm.storage
    key = _key(chat_id=43, user_id=43, bot_id=bot.id if bot.id else 0)
    ctx = FSMContext(storage=storage, key=key)
    await ctx.set_state(_ProbeStates.waiting_for_amount)

    await dispatcher.feed_update(
        bot, make_message_update(text, user_id=43, chat_id=43, chat_type="private")
    )

    assert await ctx.get_state() is None
    assert sent[0]["text"] == "✅ Отменено."


@pytest.mark.asyncio
async def test_cancel_drops_rps_acceptance_keyboard(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """M-G-4: /cancel during an active /cpc match must edit the
    opponent's challenge card keyboard out, not just clear the FSM.
    """
    bot, dispatcher, _ = await make_wired()
    capture_outgoing(bot)

    original_make_request = bot.session.make_request
    edit_markup_calls: list[int] = []

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "EditMessageReplyMarkup":
            edit_markup_calls.append(method.chat_id)
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    # Seed an awaiting_acceptance state for user 100 with opponent 200
    # and a known accept-message id. The cancel handler should read
    # this and drop the opponent-side keyboard.
    storage = dispatcher.fsm.storage
    key = _key(chat_id=100, user_id=100, bot_id=bot.id if bot.id else 0)
    ctx = FSMContext(storage=storage, key=key)
    await ctx.set_state("RpsStates:awaiting_acceptance")
    await ctx.update_data(
        opponent_id=200,
        bet=100,
        opponent_accept_message_id=4242,
    )

    await dispatcher.feed_update(
        bot, make_message_update("/cancel", user_id=100, chat_type="private")
    )

    assert await ctx.get_state() is None
    assert 200 in edit_markup_calls


@pytest.mark.asyncio
async def test_cancel_drops_duel_challenge_keyboard(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """M-G-4: /cancel during an active /duel must drop the keyboard
    on the group-chat challenge card."""
    bot, dispatcher, _ = await make_wired()
    capture_outgoing(bot)

    original_make_request = bot.session.make_request
    edit_markup_calls: list[int] = []

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "EditMessageReplyMarkup":
            edit_markup_calls.append(method.chat_id)
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    group_chat_id = -1001
    storage = dispatcher.fsm.storage
    key = _key(chat_id=group_chat_id, user_id=100, bot_id=bot.id if bot.id else 0)
    ctx = FSMContext(storage=storage, key=key)
    await ctx.set_state("DuelStates:awaiting_acceptance")
    await ctx.update_data(
        opponent_id=200,
        bet=100,
        chat_id=group_chat_id,
        challenge_message_id=4242,
    )

    await dispatcher.feed_update(
        bot,
        make_message_update("/cancel", user_id=100, chat_id=group_chat_id, chat_type="supergroup"),
    )

    assert await ctx.get_state() is None
    assert group_chat_id in edit_markup_calls


# ── #259: /cancel on a LIVE match is a terminal teardown, not a poke ──
#
# Three things separate a game FSM from a withdraw form, and /cancel
# used to honour none of them. The tests below pin each one. They fail
# on the pre-#259 handler in three distinct ways: no card edit, no
# opponent notice, and a clear that lands while the match lock is held
# by somebody else.


@pytest.mark.asyncio
async def test_cancel_during_duel_rolls_edits_card_to_cancelled(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A duel in ``awaiting_rolls`` has a live card with a Roll button on
    it, in a group both seats are reading. Dropping the buttons in
    silence leaves the opponent waiting on a match that no longer
    exists — so the card is edited to the cancel notice, the same
    posture the roll timeout already takes.
    """
    bot, dispatcher, _ = await make_wired()
    capture_outgoing(bot)

    original_make_request = bot.session.make_request
    edited: list[tuple[int, str]] = []

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "EditMessageText":
            edited.append((method.chat_id, method.text))
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    group_chat_id = -1002
    storage = dispatcher.fsm.storage
    key = _key(chat_id=group_chat_id, user_id=101, bot_id=bot.id if bot.id else 0)
    ctx = FSMContext(storage=storage, key=key)
    await ctx.set_state("DuelStates:awaiting_rolls")
    await ctx.update_data(
        opponent_id=202,
        bet=100,
        chat_id=group_chat_id,
        challenge_message_id=7777,
        challenger_roll=6,
    )

    await dispatcher.feed_update(
        bot,
        make_message_update("/cancel", user_id=101, chat_id=group_chat_id, chat_type="supergroup"),
    )

    assert await ctx.get_state() is None
    assert edited == [(group_chat_id, t("h_duel_cancelled", "ru"))]


@pytest.mark.asyncio
async def test_cancel_during_cpc_notifies_the_opponent(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """``/cpc_cancel`` tells the opponent their match is over; the generic
    ``/cancel`` reached the same FSM by the same key and said nothing.
    Same command, same effect, one of them silent — now neither is.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    original_make_request = bot.session.make_request

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "EditMessageReplyMarkup":
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    storage = dispatcher.fsm.storage
    key = _key(chat_id=300, user_id=300, bot_id=bot.id if bot.id else 0)
    ctx = FSMContext(storage=storage, key=key)
    await ctx.set_state("RpsStates:awaiting_moves")
    await ctx.update_data(
        opponent_id=400,
        bet=100,
        challenger_chat_id=300,
        opponent_move_message_id=11,
        challenger_move_message_id=12,
    )

    await dispatcher.feed_update(
        bot, make_message_update("/cancel", user_id=300, chat_id=300, chat_type="private")
    )

    assert await ctx.get_state() is None
    to_opponent = [m for m in sent if m["chat_id"] == 400]
    assert len(to_opponent) == 1
    assert "300" in to_opponent[0]["text"]


@pytest.mark.asyncio
async def test_cancel_waits_for_the_duel_match_lock(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The teardown must serialise on the per-match lock (#259).

    This is the half that is about money rather than about tidiness.
    Accept, decline, roll and the sweeper all serialise on
    ``duel._match_locks``; the old ``/cancel`` did not, so a challenger
    could clear the FSM in the window between a decisive 🎲 Roll reading
    its snapshot and ``DuelService.play`` committing the escrow and
    payout — the #126 interleave, aimed by hand instead of arriving on a
    deadline.

    What is pinned, precisely: with the match lock held by somebody else,
    ``/cancel`` reaches the lock and stops there, with the FSM still set.
    That is the property the race needs — the roll path holds the lock
    across read → play → clear, so a /cancel that must queue behind it
    cannot land in the middle.

    Deterministic, not timed: a spy on ``acquire`` reports the exact
    moment the handler asks for the lock, and the assertions run then.
    No yield-counting, no wall-clock threshold. A handler that clears
    without asking for the lock never trips the spy and the wait fails;
    one that clears BEFORE asking trips it with the state already gone.
    """
    bot, dispatcher, _ = await make_wired()
    capture_outgoing(bot)

    group_chat_id = -1003
    challenger_id = 111
    storage = dispatcher.fsm.storage
    key = _key(chat_id=group_chat_id, user_id=challenger_id, bot_id=bot.id if bot.id else 0)
    ctx = FSMContext(storage=storage, key=key)
    await ctx.set_state("DuelStates:awaiting_rolls")
    await ctx.update_data(
        opponent_id=222,
        bet=100,
        chat_id=group_chat_id,
        challenge_message_id=8888,
    )

    update = make_message_update(
        "/cancel", user_id=challenger_id, chat_id=group_chat_id, chat_type="supergroup"
    )

    async with duel._match_locks.acquire((bot.id, group_chat_id, challenger_id)):
        real_acquire = duel._match_locks.acquire
        asked_for_lock = asyncio.Event()

        def _spy(lock_key: Any) -> Any:
            asked_for_lock.set()
            return real_acquire(lock_key)

        monkeypatch.setattr(duel._match_locks, "acquire", _spy)

        task = asyncio.create_task(dispatcher.feed_update(bot, update))
        await asyncio.wait_for(asked_for_lock.wait(), timeout=5)

        assert await ctx.get_state() == "DuelStates:awaiting_rolls"
        assert not task.done()

    await asyncio.wait_for(task, timeout=5)
    assert await ctx.get_state() is None
