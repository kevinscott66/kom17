"""End-to-end ``/check`` + deep-link claim routing (#26).

The unit/integration suites already pin the atomic money mechanics
(``test_check_service.py`` / ``test_checks_repo.py``). This file pins the
*wiring*: that ``/check <code>``, the bare-args info blurb, the DEV-only
``/create_check``, and — crucially for this change — the
``/start check_<code>`` deep link all reach the right handler and route
through the same claim path.

The deep-link test is the new contract: ``/create_check`` hands out a
``t.me/<bot>?start=check_<code>`` link; the bare-/start handler only
matches when there are NO args, so a future refactor that drops the
``CommandStart(deep_link=True, magic=...)`` registration would silently
make every voucher link dead. Pinning it here keeps that visible.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramForbiddenError
from pydantic import SecretStr
from sqlalchemy import select

from telegram_invite_bot.config.settings import BotConfig, EconomyConfig
from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase, UsersBase
from telegram_invite_bot.db.models.economy import Check, CheckClaim, EconomyUser
from telegram_invite_bot.db.models.users import User as UserRow
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.keyboards.builders.checks import CheckSubVerify
from tests.e2e.handlers.conftest import (
    assert_unknown_form_hint,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, user_id: int = 555, chat_type: str = "private") -> Any:
    return make_message_update(text, chat_type=chat_type, user_id=user_id)


async def _seed_check(
    registry: Any,
    *,
    code: str = "ABCD1234",
    creator_id: int = 100,
    remaining: int = 100,
    fixed: int = 50,
    claimer_id: int = 555,
    claimer_balance: int = 0,
) -> None:
    """Seed a creator-less fixed check + a claimer wallet."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            Check(
                code=code,
                creator_id=creator_id,
                type="fixed",
                total_amount=remaining,
                remaining_amount=remaining,
                fixed_amount=fixed,
                max_claims=0,
                claims_count=0,
                required_premium=0,
                required_subscription=0,
                is_active=1,
                created_at=datetime(2026, 6, 5, 12, 0, 0),
            )
        )
        session.add(EconomyUser(user_id=claimer_id, balance=claimer_balance, language="ru"))
        await session.commit()


async def test_check_no_args_renders_info(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/check`` with no code → info blurb, not a claim attempt."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/check"))
    assert result is not UNHANDLED
    assert "Чеки" in sent[0]["text"]


async def test_check_claim_credits_claimer(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/check <code>`` claims and credits the claimer's wallet."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_check(registry, fixed=50, claimer_balance=10)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/check ABCD1234"))
    assert result is not UNHANDLED
    assert "50" in sent[0]["text"]

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 60  # 10 + 50


async def test_start_deeplink_claims_check(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/start check_<code>`` deep link routes through the claim path.

    This is the new wiring under test: the link ``/create_check`` hands
    out fires this exact update, and it must credit the claimer the same
    way ``/check <code>`` does.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_check(registry, fixed=50, claimer_balance=0)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/start check_ABCD1234"))
    assert result is not UNHANDLED
    assert "50" in sent[0]["text"]

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 50


async def test_start_deeplink_double_claim_blocked(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A second deep-link claim by the same user is rejected, not paid."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_check(registry, fixed=50, claimer_balance=0)

    # Pre-seed an existing claim row for user 555 → ALREADY_CLAIMED path.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        check = await session.get(Check, 1)
        assert check is not None
        session.add(
            CheckClaim(
                check_id=check.id,
                user_id=555,
                amount=50,
                claimed_at=datetime(2026, 6, 5, 12, 0, 0),
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/start check_ABCD1234"))
    assert result is not UNHANDLED
    assert "уже" in sent[0]["text"].lower()

    # Balance untouched — no double credit.
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 0


async def test_start_deeplink_group_gets_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Deep-link claims stay private-only — but the group form answers now.

    The claim path is unchanged: no check is looked up, no balance moves,
    nothing about the voucher is confirmed to the group. What changed is
    the tail (#158). This assertion used to read ``is UNHANDLED``, which
    was a statement about the telebot monolith owning the unmatched form,
    not about what the user experienced; with legacy gone it had become
    "the bot ignores you". Now the same non-claim ends in the generic
    "I did not understand that form" hint, which names ``/start`` and
    points at ``/help`` without saying a word about checks.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_check(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/start check_ABCD1234", chat_type="supergroup")
    )
    assert result is not UNHANDLED
    assert_unknown_form_hint(sent, command="start")

    # And the claim really did not happen: the wallet is untouched and the
    # check still has all of its money. Asserting it here rather than
    # trusting "the reply was the generic hint" — the hint is what the tail
    # says, not proof of what the routers ahead of it did or did not do.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 0
        check = (await session.execute(select(Check).where(Check.code == "ABCD1234"))).scalar_one()
        assert check.remaining_amount == 100
        assert check.claims_count == 0


async def test_create_check_all_key_value_args_no_crash(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """BUG audit: ``/create_check`` with 3+ tokens that are ALL key=value
    (no positional) must render the usage message, not raise IndexError.
    ``len(parts) >= 3`` passes but ``positional`` is empty, and
    ``positional[0]`` sits outside the try/except guarding later indices.
    """
    from pydantic import SecretStr

    from telegram_invite_bot.config.settings import BotConfig

    dev_id = 4242
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=dev_id),
    )
    sent = capture_outgoing(bot)

    # Must not raise — feed_update would propagate an uncaught IndexError.
    result = await dispatcher.feed_update(
        bot,
        _update("/create_check lang=ru premium=true expires=1h", user_id=dev_id),
    )
    assert result is not UNHANDLED
    # Usage hint rendered (RU "Создать чек" header / params line).
    assert sent, "expected a usage reply"
    assert "чек" in (sent[-1]["text"] or "").lower()


@pytest.mark.parametrize(
    "expires",
    [
        "24000000000h",  # timedelta itself: days past its 999999999 ceiling
        "1000000000000000000000000000000h",  # wider than the C int timedelta takes
        "23999999000h",  # a legal timedelta whose SUM lands past year 9999
    ],
)
async def test_create_check_absurd_expiry_renders_usage_not_a_crash(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    expires: str,
) -> None:
    """An hour count too large to become a date must reply, not raise.

    ``expires=`` was guarded by ``except ValueError``, which reads like it
    covers "unusable value" and covers only "not a number". A number that
    parses can still fail to become a date, in three different places, and
    all three raise OverflowError — so the further the typo was from
    sensible, the more likely it produced an unhandled error rather than
    the usage hint that ``expires=abc`` already got.

    All three shapes are parametrized because they raise from different
    call sites (the ``timedelta`` constructor twice, the ``datetime``
    addition once); a fix that wrapped only the constructor would still
    leave the third one crashing.
    """
    from pydantic import SecretStr

    from telegram_invite_bot.config.settings import BotConfig

    dev_id = 4242
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=dev_id),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update(f"/create_check fixed 10 5 expires={expires}", user_id=dev_id)
    )

    assert result is not UNHANDLED
    assert sent, "expected a usage reply"
    assert "чек" in (sent[-1]["text"] or "").lower()


# --------------------------------------------------------------------------
# Claim-side subscription gate (#26).
# --------------------------------------------------------------------------

_CHANNEL = "@mychan"


async def _seed_sub_check(
    registry: Any,
    *,
    code: str = "SUBC1234",
    claimer_id: int = 555,
    claimer_balance: int = 0,
) -> None:
    """Seed a SUBSCRIPTION-GATED fixed check + a claimer wallet."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            Check(
                code=code,
                creator_id=100,
                type="fixed",
                total_amount=100,
                remaining_amount=100,
                fixed_amount=50,
                max_claims=0,
                claims_count=0,
                required_premium=0,
                required_subscription=1,
                is_active=1,
                created_at=datetime(2026, 6, 5, 12, 0, 0),
            )
        )
        session.add(EconomyUser(user_id=claimer_id, balance=claimer_balance, language="ru"))
        await session.commit()


def _patch_membership(
    bot: Any, monkeypatch: Any, *, subscribed: bool, fail_on: str | None = None
) -> list[dict[str, Any]]:
    """Patch ``bot.session.make_request`` to record SendMessage/edit calls
    and answer ``GetChatMember`` with a member/left status per
    ``subscribed``. Returns the recorded-outgoing sink.

    Mirrors the conftest capture but overrides the ``GetChatMember`` reply
    so the subscription gate sees a non-member when ``subscribed=False``.

    ``fail_on`` ("send" / "callback_answer") makes that one call raise
    ``TelegramForbiddenError`` after recording the attempt — the shape of
    a blocked bot or an expired query, and the two ways a delivery
    failure used to unwind a committed claim (#1201 / #1202).
    """
    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    sink: list[dict[str, Any]] = []

    def _synth(chat_id: int, text: str, mid: int = 1) -> Message:
        return Message(
            message_id=mid,
            date=datetime(2024, 1, 1),
            chat=Chat(id=chat_id, type="private"),
            from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
            text=text,
        )

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "SendMessage":
            sink.append({"kind": "text", "chat_id": method.chat_id, "text": method.text})
            if fail_on == "send":
                raise TelegramForbiddenError(method=method, message="bot was blocked")
            return _synth(method.chat_id, method.text)
        if name == "GetChatMember":
            from aiogram.types import ChatMemberLeft, ChatMemberMember

            tgt = TelegramUser(id=method.user_id, is_bot=False, first_name="X")
            return ChatMemberMember(user=tgt) if subscribed else ChatMemberLeft(user=tgt)
        if name == "AnswerCallbackQuery":
            sink.append({"kind": "callback_answer", "text": getattr(method, "text", None)})
            if fail_on == "callback_answer":
                raise TelegramForbiddenError(method=method, message="query is too old")
            return True
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return sink


async def test_sub_gated_claim_not_subscribed_shows_prompt(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """Claiming a sub-gated check while NOT subscribed → no credit, and a
    prompt carrying the verify button (``check_sub`` callback)."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        economy_config=EconomyConfig(CHECK_SUBSCRIPTION_CHANNEL=_CHANNEL),
    )
    await _seed_sub_check(registry, claimer_balance=0)
    sink = _patch_membership(bot, monkeypatch, subscribed=False)

    result = await dispatcher.feed_update(bot, _update("/check SUBC1234"))
    assert result is not UNHANDLED

    # Prompt rendered, not a claim receipt.
    assert sink, "expected a subscribe prompt"
    text = sink[-1]["text"]
    assert "подпис" in text.lower()

    # Balance untouched — gate blocked the claim.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 0


async def test_sub_gated_verify_callback_credits_when_subscribed(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """The verify callback, when the user is now subscribed, runs the
    claim and credits the wallet."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        economy_config=EconomyConfig(CHECK_SUBSCRIPTION_CHANNEL=_CHANNEL),
    )
    await _seed_sub_check(registry, claimer_balance=10)
    _patch_membership(bot, monkeypatch, subscribed=True)

    data = CheckSubVerify(code="SUBC1234").pack()
    result = await dispatcher.feed_update(bot, make_callback_update(data, user_id=555))
    assert result is not UNHANDLED

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 60  # 10 + 50


async def test_sub_gated_verify_callback_still_not_subscribed(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """The verify callback, when STILL not subscribed, answers a toast and
    does NOT credit."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        economy_config=EconomyConfig(CHECK_SUBSCRIPTION_CHANNEL=_CHANNEL),
    )
    await _seed_sub_check(registry, claimer_balance=0)
    sink = _patch_membership(bot, monkeypatch, subscribed=False)

    data = CheckSubVerify(code="SUBC1234").pack()
    result = await dispatcher.feed_update(bot, make_callback_update(data, user_id=555))
    assert result is not UNHANDLED

    # A callback answer (toast), no claim receipt.
    answers = [e for e in sink if e["kind"] == "callback_answer"]
    assert answers, "expected a still-not-subscribed toast"

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 0


async def test_non_sub_check_claims_without_gate(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A check WITHOUT required_subscription claims normally even when a
    channel is configured — the gate only fires on gated checks."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        economy_config=EconomyConfig(CHECK_SUBSCRIPTION_CHANNEL=_CHANNEL),
    )
    await _seed_check(registry, fixed=50, claimer_balance=0)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/check ABCD1234"))
    assert result is not UNHANDLED
    assert "50" in sent[0]["text"]

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 50


# --------------------------------------------------------------------------
# RR-2 #17/#18 — create receipt richness + claim-card activations counter.
# --------------------------------------------------------------------------


def _dev_config(dev_id: int) -> BotConfig:
    return BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=dev_id)


async def _seed_wallet(registry: Any, user_id: int, balance: int) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _seed_named_user(
    registry: Any, *, user_id: int, username: str, language_code: str = "ru"
) -> None:
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(
            UserRow(
                user_id=user_id,
                username=username,
                first_name="Friend",
                language_code=language_code,
            )
        )
        await session.commit()


async def test_create_check_receipt_shows_per_claim_amount_and_activations(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-2 #17: the create receipt names the PER-CLAIM amount and the
    activations count, not just the code and the total charge.

    The pre-restoration card said "Чек создан! Код: X. Списано 150." — a
    creator could not tell 3×50 from 50×3 from it. Both figures are back.
    """
    dev_id = 4242
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], bot_config=_dev_config(dev_id)
    )
    await _seed_wallet(registry, dev_id, 1000)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/create_check fixed 50 3", user_id=dev_id))
    assert result is not UNHANDLED
    text = sent[-1]["text"]
    assert "Номинал" in text  # per-claim amount line
    assert "50" in text
    assert "Активаций" in text  # activations count line
    assert "3" in text
    assert "150" in text  # total charged


async def test_create_check_random_receipt_shows_the_range(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A ``random`` check advertises its min..max band (legacy
    "💰 Сумма: от 10 до 50 монет"), which a flat figure cannot express."""
    dev_id = 4242
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], bot_config=_dev_config(dev_id)
    )
    await _seed_wallet(registry, dev_id, 1000)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/create_check random 10 50 4", user_id=dev_id)
    )
    assert result is not UNHANDLED
    text = sent[-1]["text"]
    assert "10" in text
    assert "50" in text
    assert "Активаций" in text


async def test_create_check_individual_binds_target_and_dms_them(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-2 #17: ``individual <amount> @user`` resolves the handle to a
    ``target_user_id`` AND hands the gift to the recipient by DM.

    The binding is the security half of this item: ``WRONG_USER`` in
    :meth:`CheckService.claim_check` is keyed off ``target_user_id``, so
    before the resolution landed a "personal" check was claimable by
    whoever saw the code first.
    """
    dev_id = 4242
    target_id = 900
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], bot_config=_dev_config(dev_id)
    )
    await _seed_wallet(registry, dev_id, 1000)
    await _seed_named_user(registry, user_id=target_id, username="friend")
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/create_check individual 70 @friend", user_id=dev_id)
    )
    assert result is not UNHANDLED

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (await session.execute(select(Check))).scalars().all()
    assert len(rows) == 1
    assert rows[0].target_user_id == target_id

    # The recipient got their own DM…
    dm = [m for m in sent if m["chat_id"] == target_id]
    assert dm, "expected a DM to the individual check's target"
    assert "70" in dm[0]["text"]
    # …and the creator's receipt names them and confirms the DM landed.
    receipt = [m for m in sent if m["chat_id"] != target_id][-1]["text"]
    assert "@friend" in receipt
    assert "Персональный чек создан" in receipt
    assert "уведомление" in receipt
    # An addressed check is single-claim by construction — "Активаций: 1"
    # would be noise, and legacy's personal receipt (bot.py:25400) omits
    # it too.
    assert "Активаций" not in receipt


async def test_create_check_individual_unknown_handle_creates_nothing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An unresolvable @handle aborts BEFORE the debit: no check row, no
    coins moved. Legacy refused the same way (bot.py:25386) — an
    individual check with no target would silently be a public one."""
    dev_id = 4242
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], bot_config=_dev_config(dev_id)
    )
    await _seed_wallet(registry, dev_id, 1000)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/create_check individual 70 @nobody", user_id=dev_id)
    )
    assert result is not UNHANDLED
    assert "@nobody" in sent[-1]["text"]

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (await session.execute(select(Check))).scalars().all()
        creator = await session.get(EconomyUser, dev_id)
    assert rows == []
    assert creator is not None
    assert creator.balance == 1000  # untouched


async def test_create_check_individual_without_handle_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``individual`` with no recipient token is refused rather than
    quietly creating an unbound (= public) voucher."""
    dev_id = 4242
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], bot_config=_dev_config(dev_id)
    )
    await _seed_wallet(registry, dev_id, 1000)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/create_check individual 70 lang=ru", user_id=dev_id)
    )
    assert result is not UNHANDLED

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (await session.execute(select(Check))).scalars().all()
    assert rows == []
    assert sent, "expected a refusal reply"


async def test_claim_card_shows_remaining_activations(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-2 #18: the claim card counts ACTIVATIONS left, not just coins.

    Seeded with ``max_claims=3``; after one claim the card must say two
    are left (legacy ``max_claims - new_claims``).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            Check(
                code="LIMITED1",
                creator_id=100,
                type="fixed",
                total_amount=150,
                remaining_amount=150,
                fixed_amount=50,
                max_claims=3,
                claims_count=0,
                required_premium=0,
                required_subscription=0,
                is_active=1,
                created_at=datetime(2026, 6, 5, 12, 0, 0),
            )
        )
        session.add(EconomyUser(user_id=555, balance=0, language="ru"))
        await session.commit()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/check LIMITED1"))
    assert result is not UNHANDLED
    claimer_card = [m for m in sent if m["chat_id"] == 555][0]["text"]
    assert "Осталось активаций" in claimer_card
    assert "2" in claimer_card
    assert "100" in claimer_card  # coins still in the check


async def test_claim_card_shows_infinity_for_unlimited_checks(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``max_claims = 0`` is legacy's "unlimited" sentinel and must render
    as ``∞``, never as ``0`` (which would read as "nothing left")."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_check(registry, fixed=50, claimer_balance=0)  # max_claims=0
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/check ABCD1234"))
    assert result is not UNHANDLED
    claimer_card = [m for m in sent if m["chat_id"] == 555][0]["text"]
    assert "∞" in claimer_card


def _install_lost_reply_target(bot: Any) -> list[str]:
    """Refuse the first send for its reply target; let the rest through.

    The message the claim was typed into (or the ``/start`` the deep
    link fired) is gone by the time the receipt goes out — deleted by
    the user, by a cleanup bot, by an admin sweep. Telegram refuses the
    whole call then, while the chat itself is perfectly alive.

    Returns the list of texts that actually reached Telegram.
    """
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    delivered: list[str] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        if not delivered and type(method).__name__ == "SendMessage":
            delivered.append("")  # the refused reply, replaced below
            raise TelegramBadRequest(
                method=method, message="Bad Request: message to be replied not found"
            )
        delivered.append(getattr(method, "text", ""))
        return Message(
            message_id=1,
            date=datetime(2024, 1, 1),
            chat=Chat(id=555, type="private"),
            from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
            text=getattr(method, "text", "ok"),
        )

    bot.session.make_request = fake_make_request
    return delivered


async def test_claim_receipt_falls_back_to_a_plain_send(
    make_wired: WiredFactory,
) -> None:
    """A vanished command message must not cost the claimer the check.

    The receipt used to be a bare ``message.reply``: a missing reply
    target raised, ``handlers.errors`` painted "⚠️ Произошла ошибка",
    and the session middleware unwound the credit and the activation
    counter with it. Losing the thread the receipt hangs off is not a
    reason to lose the claim — the fallback plain send delivers it and
    the claim stands.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_check(registry, fixed=50, claimer_balance=10)

    delivered = _install_lost_reply_target(bot)
    result = await dispatcher.feed_update(bot, _update("/start check_ABCD1234"))
    assert result is not UNHANDLED

    assert "50" in delivered[1], "the receipt never reached the claimer"
    assert not any("ошибка" in text for text in delivered)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None and user.balance == 60  # 10 + 50
        check = (await session.execute(select(Check).where(Check.code == "ABCD1234"))).scalar_one()
        assert check.claims_count == 1


async def test_ordinary_user_claims_with_the_rank_gate_live(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A rank-0 user in a DM must be able to redeem a check.

    Every other case in this file builds ``schemas=[EconomyBase]``, and
    that is precisely why they all passed while the command was dead in
    production: ``CommandAccessMiddleware`` reads its overrides from
    moderation.db, so without ``ModerationBase`` the lookup raises and
    the middleware fails open — the gate under test is never exercised.

    With the table present the gate runs for real, and it used to refuse
    the update: ``check`` was carried over as an alias of the
    developer-only ``create_check`` row (rank 5), and this router is
    private-only, so the middleware's live-Telegram-admin bypass — which
    is group-chats-only — could not rescue it either. The claimer got a
    rank refusal and the wallet stayed empty.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase, ModerationBase],
    )
    await _seed_check(registry, fixed=50, claimer_balance=10)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/check ABCD1234"))
    assert result is not UNHANDLED

    texts = [m["text"] for m in sent]
    assert texts, "the rank gate swallowed the update without answering"
    assert not any("ранг" in text.lower() for text in texts), (
        f"claim refused by the rank gate: {texts[0]!r}"
    )

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 60  # 10 + 50


async def test_create_check_stays_developer_only_with_the_gate_live(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The other half of the split: ``/create_check`` keeps its gate.

    Splitting ``check`` out of the developer row would be a privilege
    escalation if it also loosened the *create* side, so pin that an
    ordinary user still gets nothing but a refusal — and no check is
    minted — with the gate genuinely running.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase, ModerationBase],
    )
    await _seed_wallet(registry, 555, 10_000)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/create_check amount=100 count=1"))

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        assert (await session.execute(select(Check))).scalars().all() == []
    assert not any("Чек создан" in m["text"] for m in sent)


# --------------------------------------------------------------------------
# #1200 — an unrecognised token must refuse, not be swallowed after a debit.
# --------------------------------------------------------------------------


async def test_create_check_misspelt_extra_is_refused_not_silently_dropped(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1200: ``premuim=true`` used to land in ``extras`` under its typo,
    leave ``required_premium`` False, and still mint the check after
    debiting the creator — the #745 bug class ``/promo_create`` already
    fixed at ``handlers/promo.py:172-187``. An unrecognised token is a
    usage refusal now, and nothing moves.
    """
    dev_id = 4242
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], bot_config=_dev_config(dev_id)
    )
    await _seed_wallet(registry, dev_id, 1000)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/create_check fixed 50 3 premuim=true", user_id=dev_id)
    )

    assert result is not UNHANDLED
    assert not any("Чек создан" in (m["text"] or "") for m in sent)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        assert (await session.execute(select(Check))).scalars().all() == []
        creator = await session.get(EconomyUser, dev_id)
        assert creator is not None
        assert creator.balance == 1000


async def test_create_check_surplus_positional_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The legacy syntax has no fourth slot for ``fixed``, so a fourth
    positional is a typo. Without an upper bound the surplus token was
    ignored and the check minted anyway — the same silence as a misspelt
    extra, which is exactly how a misspelt extra now presents.
    """
    dev_id = 4242
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], bot_config=_dev_config(dev_id)
    )
    await _seed_wallet(registry, dev_id, 1000)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/create_check fixed 50 3 7", user_id=dev_id)
    )

    assert result is not UNHANDLED
    assert not any("Чек создан" in (m["text"] or "") for m in sent)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        assert (await session.execute(select(Check))).scalars().all() == []
        creator = await session.get(EconomyUser, dev_id)
        assert creator is not None
        assert creator.balance == 1000


async def test_create_check_every_documented_extra_still_parses(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Negative control for #1200: all four documented keys must keep
    working. ``sub=`` is the one the parse comment used to omit, and a
    whitelist that inherited that omission would silently un-gate the
    subscription requirement — the very failure the ticket is about.
    """
    dev_id = 4242
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], bot_config=_dev_config(dev_id)
    )
    await _seed_wallet(registry, dev_id, 1000)
    capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot,
        _update(
            "/create_check fixed 50 3 lang=ru premium=true sub=true expires=24h",
            user_id=dev_id,
        ),
    )

    assert result is not UNHANDLED
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        check = (await session.execute(select(Check))).scalars().one()
        assert check.required_language == "ru"
        assert check.required_premium == 1
        assert check.required_subscription == 1
        assert check.expires_at is not None


# --------------------------------------------------------------------------
# #1201 / #1202 — the sub-verify claim must commit before it talks to
# Telegram, and an undeliverable receipt must not unwind it.
# --------------------------------------------------------------------------


async def test_sub_verify_commits_the_claim_before_answering_the_callback(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """#1201: the callback handler took no ``checkpoint``, so the claim sat
    uncommitted across up to three Telegram round-trips and the session
    middleware only committed after the handler returned. A failure on the
    very first of those calls therefore rolled the credit back — and the
    comment above it claimed the claim had "already committed atomically".

    ``AnswerCallbackQuery`` failing is the ordinary shape of that (an
    expired query), so it is the cheapest proof the commit now happens
    first.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        economy_config=EconomyConfig(CHECK_SUBSCRIPTION_CHANNEL=_CHANNEL),
    )
    await _seed_sub_check(registry, claimer_balance=10)
    _patch_membership(bot, monkeypatch, subscribed=True, fail_on="callback_answer")

    data = CheckSubVerify(code="SUBC1234").pack()
    await dispatcher.feed_update(bot, make_callback_update(data, user_id=555))

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 60  # 10 + 50, and it survived the failure
        check = (await session.execute(select(Check).where(Check.code == "SUBC1234"))).scalar_one()
        assert check.claims_count == 1


async def test_sub_verify_undeliverable_receipt_keeps_the_claim(
    make_wired: WiredFactory,
    monkeypatch: Any,
) -> None:
    """#1202: the message path states the policy outright — "a claim only
    ever adds coins, so the claimer loses nothing by not seeing the card"
    — and routes its receipt through ``reply_or_send``. The callback path
    used a bare ``target.answer``, so the user saw the success toast and
    then lost the coins to the rollback. The creator DM four lines below
    was already guarded; only the claimer's own receipt was not.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        economy_config=EconomyConfig(CHECK_SUBSCRIPTION_CHANNEL=_CHANNEL),
    )
    await _seed_sub_check(registry, claimer_balance=10)
    sink = _patch_membership(bot, monkeypatch, subscribed=True, fail_on="send")

    data = CheckSubVerify(code="SUBC1234").pack()
    await dispatcher.feed_update(bot, make_callback_update(data, user_id=555))

    assert any(e["kind"] == "text" for e in sink), "the receipt was attempted"
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, 555)
        assert user is not None
        assert user.balance == 60
        check = (await session.execute(select(Check).where(Check.code == "SUBC1234"))).scalar_one()
        assert check.claims_count == 1
