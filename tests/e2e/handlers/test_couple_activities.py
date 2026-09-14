"""End-to-end tests for the couple joint-activities money path.

Each test feeds an aiogram :class:`Update` through the full dispatcher
(including BOTH the ``SessionMiddleware`` that attaches
``bonds_write_repo`` over users.db AND the ``EconomyMiddleware`` that
attaches ``economy_repo`` over economy.db) and asserts on the outgoing
Telegram wire calls + the resulting DB rows.

Scenarios:
* married pair → marriage activity click → coins debited by cost +
  marriage experience += xp + done message;
* relationship below the activity's level → locked alert, no debit,
  no XP;
* insufficient coins → no_coins alert, no debit, no XP;
* successful relationship activity → debit + rel XP + effect line;
* ``/activities`` menu render for a married caller;
* every charge — and every compensating refund — leaves a
  ``transactions`` row.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.users import Marriage, Relationship
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.keyboards.builders import CoupleActivity
from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot
    from aiogram.types import Update

    from tests.e2e.handlers.conftest import WiredFactory

_CHAT_ID = -100
_USER = 10
_PARTNER = 20


def _group_cmd(text: str, *, user_id: int = _USER, update_id: int = 1) -> Update:
    return make_message_update(
        text,
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        user_id=user_id,
        first_name="Alice",
        update_id=update_id,
    )


def _group_callback(data: str, *, user_id: int = _USER, update_id: int = 2) -> Update:
    """A callback_query whose ``message`` lives in the supergroup chat.

    The handler reads ``callback.message.chat.id`` for the bond scope, so
    the synthetic message must sit in ``_CHAT_ID`` (the conftest helper
    pins callbacks to a private chat keyed on user_id, which would scope
    the bond lookup to the wrong chat).
    """
    from aiogram.types import Update as _Update

    return _Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": user_id, "is_bot": False, "first_name": "Alice"},
                "chat_instance": "ci-1",
                "data": data,
                "message": {
                    "message_id": 10,
                    "date": 1_700_000_000,
                    "chat": {"id": _CHAT_ID, "type": "supergroup", "title": "T"},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "menu",
                },
            },
        }
    )


async def _seed_marriage(registry: Any, *, exp: int = 0) -> None:
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Marriage(
                chat_id=_CHAT_ID,
                user1_id=_USER,
                user2_id=_PARTNER,
                created_at=datetime(2024, 1, 1),
                experience=exp,
                status="active",
            )
        )
        await session.commit()


async def _seed_relationship(registry: Any, *, exp: int) -> None:
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=_CHAT_ID,
                user1_id=_USER,
                user2_id=_PARTNER,
                created_at=datetime(2024, 1, 1),
                experience=exp,
                status="active",
            )
        )
        await session.commit()


async def _seed_wallet(registry: Any, *, balance: int, user_id: int = _USER) -> None:
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _marriage_exp(registry: Any) -> int:
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        row = (
            await session.execute(select(Marriage).where(Marriage.chat_id == _CHAT_ID))
        ).scalar_one()
        return row.experience or 0


async def _relationship_exp(registry: Any) -> int:
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        row = (
            await session.execute(select(Relationship).where(Relationship.chat_id == _CHAT_ID))
        ).scalar_one()
        return row.experience or 0


async def _balance(registry: Any, user_id: int = _USER) -> int:
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        wallet = await session.get(EconomyUser, user_id)
        assert wallet is not None
        return wallet.balance


# ---------------------------------------------------------------------------
# Menu render
# ---------------------------------------------------------------------------


async def test_activities_menu_married(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_marriage(registry, exp=0)
    await _seed_wallet(registry, balance=500)

    sent = capture_outgoing(bot)
    result = await dp.feed_update(bot, _group_cmd("/activities"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "браке" in sent[0]["text"].lower()


async def test_activities_no_pair(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_wallet(registry, balance=500)

    sent = capture_outgoing(bot)
    await dp.feed_update(bot, _group_cmd("/activities"))
    assert len(sent) == 1
    assert "нет пары" in sent[0]["text"].lower()


# ---------------------------------------------------------------------------
# Marriage activity click — debit + marriage XP + done
# ---------------------------------------------------------------------------


async def test_married_activity_debits_and_grants_xp(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_marriage(registry, exp=0)
    await _seed_wallet(registry, balance=500)

    sent = capture_callback_outgoing(bot)
    # dinner: cost 100, xp 15
    data = CoupleActivity(kind="marry", key="dinner", partner_id=_PARTNER, owner_id=_USER).pack()
    result = await dp.feed_update(bot, _group_callback(data))
    assert result is not UNHANDLED

    # Confirmation message sent + callback answered.
    texts = [m for m in sent if m["kind"] == "text"]
    assert len(texts) == 1
    body = texts[0]["text"]
    assert "+15" in body
    assert body.startswith("💕 🍽 | ")
    # RR-5: the tier line used to render an empty <b></b> because the
    # caller passed ``level=None``; the level now comes from the
    # post-grant XP.
    assert "<b></b>" not in body

    assert await _balance(registry) == 400  # 500 - 100
    assert await _marriage_exp(registry) == 15


# ---------------------------------------------------------------------------
# Relationship below level → locked alert, no debit, no XP
# ---------------------------------------------------------------------------


async def test_relationship_below_level_locked_no_debit(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    # big_gift needs level 7; exp=150 → level 1.
    await _seed_relationship(registry, exp=150)
    await _seed_wallet(registry, balance=5000)

    sent = capture_callback_outgoing(bot)
    data = CoupleActivity(kind="rel", key="big_gift", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    # Alert answer, no confirmation text.
    answers = [m for m in sent if m["kind"] == "callback_answer"]
    assert answers and answers[0]["text"] is not None
    assert "уровень" in answers[0]["text"].lower()
    assert not [m for m in sent if m["kind"] == "text"]

    assert await _balance(registry) == 5000  # untouched
    assert await _relationship_exp(registry) == 150  # untouched


# ---------------------------------------------------------------------------
# Insufficient coins → no_coins, no debit, no XP
# ---------------------------------------------------------------------------


async def test_insufficient_coins_no_debit_no_xp(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    # cinema needs level 3 + cost 100; exp=10000 → level 4 (passes level).
    await _seed_relationship(registry, exp=10000)
    await _seed_wallet(registry, balance=50)  # < 100

    sent = capture_callback_outgoing(bot)
    data = CoupleActivity(kind="rel", key="cinema", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    answers = [m for m in sent if m["kind"] == "callback_answer"]
    assert answers and answers[0]["text"] is not None
    assert "монет" in answers[0]["text"].lower()
    assert not [m for m in sent if m["kind"] == "text"]

    assert await _balance(registry) == 50  # untouched
    assert await _relationship_exp(registry) == 10000  # untouched


# ---------------------------------------------------------------------------
# Successful relationship activity → debit + rel XP + effect line
# ---------------------------------------------------------------------------


async def test_relationship_activity_success_debit_xp_effect(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    # cinema: cost 100, xp 200, min_level 3, effect_hours 40 (1d 16h).
    await _seed_relationship(registry, exp=10000)  # level 4
    await _seed_wallet(registry, balance=500)

    sent = capture_callback_outgoing(bot)
    data = CoupleActivity(kind="rel", key="cinema", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    texts = [m for m in sent if m["kind"] == "text"]
    assert len(texts) == 1
    body = texts[0]["text"]
    assert "+200" in body
    # The cosmetic effect flavour line (40h → days+hours template).
    assert "🕔" in body
    # RR-5 #51: the flavoured story line leads the card, with the
    # activity's own icon and the actor's mention — not a bare label.
    assert body.startswith("💕 🎬 | ")
    assert "Alice" in body
    assert "{actor}" not in body

    assert await _balance(registry) == 400  # 500 - 100
    assert await _relationship_exp(registry) == 10200  # 10000 + 200


# ---------------------------------------------------------------------------
# Result undeliverable → the whole activity rolls back
# ---------------------------------------------------------------------------


def _capture_with_dead_chat(bot: Bot) -> list[str]:
    """Answer the callback, then fail both deliveries into the chat.

    The realistic shape of the worst case: the card the couple tapped
    was deleted (so the *reply* is refused for its target), and by the
    time we retry as a plain send the bot is out of the group. That is
    the only route to ``reply_or_send`` returning ``False`` — a bare
    ``Forbidden`` on the reply propagates on its own.

    ``AnswerCallbackQuery`` still has to succeed, otherwise the update
    dies before it reaches the delivery path this test is about.
    """
    attempted: list[str] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        name = type(method).__name__
        attempted.append(name)
        if name == "AnswerCallbackQuery":
            return True
        if attempted.count("SendMessage") == 1:
            raise TelegramBadRequest(
                method=method, message="Bad Request: message to be replied not found"
            )
        raise TelegramForbiddenError(method=method, message="bot was kicked from the group")

    bot.session.make_request = fake_make_request  # type: ignore[method-assign,assignment]
    return attempted


async def test_undeliverable_result_rolls_back_the_whole_activity(
    make_wired: WiredFactory,
) -> None:
    """The couple paid, the XP was written — and then nothing could be
    delivered.

    Charging for an activity nobody can see is the worst of the three
    possible endings, and it is the one a blanket ``suppress`` used to
    produce. The handler raises instead, so the session middleware rolls
    the debit and the XP grant back together and the tap simply did not
    happen.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_relationship(registry, exp=10000)  # level 4
    await _seed_wallet(registry, balance=500)

    attempted = _capture_with_dead_chat(bot)
    data = CoupleActivity(kind="rel", key="cinema", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    # The delivery was really attempted — both the reply and the plain
    # send — so the assertions below are not passing for a cheaper
    # reason (e.g. the handler bailing out before the debit).
    assert attempted.count("SendMessage") >= 2
    assert await _balance(registry) == 500
    assert await _relationship_exp(registry) == 10000


# ---------------------------------------------------------------------------
# Every charge leaves a ledger row
# ---------------------------------------------------------------------------


async def _ledger(registry: Any, user_id: int = _USER) -> list[Transaction]:
    """Rows where ``user_id`` is the payer."""
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        rows = await session.execute(select(Transaction).where(Transaction.from_id == user_id))
        return list(rows.scalars())


async def _all_ledger(registry: Any, user_id: int = _USER) -> list[Transaction]:
    """Rows on EITHER side of ``user_id`` — a refund credits them back."""
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        rows = await session.execute(
            select(Transaction).where(
                (Transaction.from_id == user_id) | (Transaction.to_id == user_id)
            )
        )
        return list(rows.scalars())


async def test_activity_charge_is_recorded_in_the_ledger(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Coins must never leave a wallet without a transactions row.

    This surface talked to ``EconomyRepo`` directly, so it was the one
    spend in the project with no ledger entry: the balance dropped and
    ``/balance``'s weekly "sent" total never moved, which to a user reads
    as coins vanishing.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_marriage(registry, exp=0)
    await _seed_wallet(registry, balance=500)

    capture_callback_outgoing(bot)
    data = CoupleActivity(kind="marry", key="dinner", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    assert await _balance(registry) == 400
    rows = await _ledger(registry)
    assert len(rows) == 1
    assert rows[0].type == "couple_activity"
    assert rows[0].reason == "marry:dinner"
    # ``amount`` is positive; DIRECTION lives in from_id/to_id (the new
    # pipeline's convention — see TransactionsRepo.window_stats, which
    # sums from_id rows into "sent"). A pure spend has no counterparty.
    assert rows[0].amount == 100
    assert rows[0].to_id is None


async def test_refunded_activity_records_both_legs(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compensating refund is an audit event, not an erasure.

    The bond vanishing between the gate and the XP grant nets the money
    to zero, but the trail must show BOTH movements — otherwise a charge
    that was refunded is indistinguishable from one that never happened.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_marriage(registry, exp=0)
    await _seed_wallet(registry, balance=500)

    async def _vanished(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(BondsWriteRepo, "add_marriage_xp", _vanished)

    capture_callback_outgoing(bot)
    data = CoupleActivity(kind="marry", key="dinner", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    assert await _balance(registry) == 500  # net zero
    rows = sorted(await _all_ledger(registry), key=lambda r: r.id)
    assert [r.type for r in rows] == ["couple_activity", "couple_activity_refund"]
    # Same size, opposite direction: the charge leaves the user
    # (``from_id``), the refund returns to them (``to_id``).
    assert [r.amount for r in rows] == [100, 100]
    assert rows[0].from_id == _USER
    assert rows[1].to_id == _USER


async def test_a_refund_outlives_a_failed_alert(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1869: the refund is committed before the popup is attempted.

    Same vanished-bond scenario as above, except the alert cannot be
    delivered. Rolling back here would net the money to zero as well —
    but it would take BOTH ledger legs with it, and the user would have
    seen nothing at all. The checkpoint after ``release`` is what keeps
    the audit trail. Drop it and this test fails.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_marriage(registry, exp=0)
    await _seed_wallet(registry, balance=500)

    async def _vanished(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(BondsWriteRepo, "add_marriage_xp", _vanished)

    capture_callback_outgoing(bot)
    original = bot.session.make_request

    async def refuse(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "AnswerCallbackQuery":
            raise TelegramForbiddenError(method=method, message="Forbidden: blocked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = refuse  # type: ignore[method-assign,assignment]
    data = CoupleActivity(kind="marry", key="dinner", partner_id=_PARTNER, owner_id=_USER).pack()
    with contextlib.suppress(TelegramForbiddenError):
        await dp.feed_update(bot, _group_callback(data))

    assert await _balance(registry) == 500  # net zero, as always
    rows = sorted(await _all_ledger(registry), key=lambda r: r.id)
    assert [r.type for r in rows] == ["couple_activity", "couple_activity_refund"]


# ---------------------------------------------------------------------------
# #463: a click from someone the card was not rendered for
# ---------------------------------------------------------------------------


async def test_foreign_click_on_activity_button_is_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A stranger clicking the owner's activity button changes nothing.

    #463. The alert asserted below is the owner-tag refusal specifically
    — not the "you are not married" one a stranger would also hit — so
    this proves the gate fires BEFORE any bond is re-resolved, which is
    what keeps the owner's card from being repainted.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_marriage(registry, exp=0)
    await _seed_wallet(registry, balance=500)

    sent = capture_callback_outgoing(bot)
    data = CoupleActivity(kind="marry", key="dinner", partner_id=_PARTNER, owner_id=_USER).pack()
    # Same payload, a different clicker.
    await dp.feed_update(bot, _group_callback(data, user_id=_USER + 999))

    answers = [m for m in sent if m["kind"] == "callback_answer"]
    assert answers and answers[0]["text"] is not None
    assert "карточка" in answers[0]["text"].lower()
    assert not [m for m in sent if m["kind"] == "text"]

    assert await _balance(registry) == 500  # untouched
    assert await _marriage_exp(registry) == 0  # untouched


# ---------------------------------------------------------------------------
# #1546: the escrow pair, not debit/credit — lifetime counters
# ---------------------------------------------------------------------------


async def _lifetime(registry: Any, user_id: int = _USER) -> tuple[int, int]:
    """``(total_spent, total_earned)`` — the two ``/balance`` card numbers."""
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        wallet = await session.get(EconomyUser, user_id)
        assert wallet is not None
        return wallet.total_spent, wallet.total_earned


async def test_refunded_activity_leaves_the_lifetime_counters_alone(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1546: a round trip that moved no coins must move no history.

    The old ``debit``/``credit`` pair netted the BALANCE to zero and
    added ``cost`` to ``total_spent`` AND ``total_earned`` every time.
    Both halves of the race are user-controlled — their own click
    against their own ``/breakup`` — so the inflation was free and
    repeatable, and it feeds every rank/achievement threshold that
    reads those columns.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_marriage(registry, exp=0)
    await _seed_wallet(registry, balance=500)

    async def _vanished(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(BondsWriteRepo, "add_marriage_xp", _vanished)

    capture_callback_outgoing(bot)
    data = CoupleActivity(kind="marry", key="dinner", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    assert await _balance(registry) == 500
    assert await _lifetime(registry) == (0, 0)


async def test_successful_activity_books_the_escrow_as_a_spend(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The settlement leg: a purchase that stuck IS lifetime spending.

    ``hold`` alone would under-report it, which is the opposite failure
    to the one #1546 fixes. Legacy's ``remove_coins`` bumped
    ``total_spent`` on this path (bot.py:9938) and so must we.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_marriage(registry, exp=0)
    await _seed_wallet(registry, balance=500)

    capture_callback_outgoing(bot)
    data = CoupleActivity(kind="marry", key="dinner", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    assert await _balance(registry) == 400
    assert await _lifetime(registry) == (100, 0)


async def test_relationship_activity_settles_and_refunds_symmetrically(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relationship branch carries the same two legs as the marriage one."""
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_relationship(registry, exp=10000)  # level 4
    await _seed_wallet(registry, balance=500)

    async def _vanished(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(BondsWriteRepo, "add_relationship_xp", _vanished)

    capture_callback_outgoing(bot)
    data = CoupleActivity(kind="rel", key="cinema", partner_id=_PARTNER, owner_id=_USER).pack()
    await dp.feed_update(bot, _group_callback(data))

    assert await _balance(registry) == 500
    assert await _lifetime(registry) == (0, 0)
