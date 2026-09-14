"""End-to-end ``/send`` flow: dispatcher → EconomyMiddleware → TransferService.

Stage 17 wires the handler over Stages 14-16 (TransferEffects,
pure tax helpers, TransferService). The matrix here pins:

* Happy path — numeric ``/send <to_id> <amount>`` in a private
  chat seeds the sender wallet, debits gross, credits net to the
  recipient, renders the success receipt.
* VIP discount — sender holding an active global VIP grant pays
  half tax (Stage 14 VipRepo → TransferEffects → service path).
* Outcome coverage — INSUFFICIENT_FUNDS, NO_RECIPIENT_WALLET,
  SELF_TRANSFER each render distinct cards (taxonomy pinning at
  the handler edge).
* Parse-time rejection — missing args / non-numeric / non-positive
  amount render usage or invalid-amount text and never call the
  service.
* Anonymous sender_chat — refused inline.
* Group chats and unsupported forms fall through to legacy.
* Settings wiring (#193) — the tax rate and the treasury id reach
  the service through ``EconomyMiddleware``. Every case below runs
  with ``COINS_TRANSFER_TAX=0.05`` supplied explicitly, because the
  shipped default is ``0`` (legacy bot.py:2550): a bare
  ``make_wired`` would silently give a tax-free transfer and every
  "95 net" assertion in this file would read 100.

Tests use the same WiredFactory + make_message_update helpers as
the /daily e2e tests, so the dispatcher path is the production
path (middleware + DI + service + repo + SQLite).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Update
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from telegram_invite_bot.config.settings import BotConfig, EconomyConfig
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.users import User as UserRow
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


_TAXED = EconomyConfig(COINS_TRANSFER_TAX=0.05)
"""The rate this file's arithmetic is written against.

Kept explicit rather than relying on a default. The shipped default
is ``0`` — legacy's own (bot.py:2550 ``"coins_transfer_tax": 0``,
and the operator's ``settings.json`` carries that 0 too) — so this
constant is also the proof that the value travels
Settings → EconomyMiddleware → TransferConfig at all (#193). Before
that wiring existed the service hard-coded 0.05 internally and
these tests passed no matter what Settings said.
"""


def _update(text: str, *, user_id: int = 555, chat_type: str = "private") -> Update:
    return make_message_update(
        text,
        user_id=user_id,
        first_name="Sam",
        language_code="ru",
        chat_type=chat_type,
    )


async def _seed_wallet(
    registry: Any,
    user_id: int,
    *,
    balance: int = 1_000,
    vip_till: float | None = None,
    language: str = "ru",
) -> None:
    """Direct DB seed — bypasses welcome-credit randomness so the
    test asserts on exact post-transfer numbers."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            EconomyUser(user_id=user_id, balance=balance, language=language, vip_till=vip_till)
        )
        await session.commit()


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    return wallet.balance if wallet is not None else None


async def _ledger(registry: Any) -> list[Transaction]:
    """Every ledger row this file's transfers wrote, oldest first."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = await session.execute(select(Transaction).order_by(Transaction.id))
        return list(rows.scalars().all())


async def test_send_success_debits_sender_credits_recipient_renders_receipt(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Happy path: 100 sent at default 5% tax = 95 net to recipient,
    5 burned (no admin configured at middleware level). Pins the
    full pipeline end-to-end in one assertion block."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/send 999 100"))
    assert result is not UNHANDLED

    # Two outgoing messages now: the sender's receipt, then the
    # recipient's courtesy DM (#16). Order is receipt-first.
    assert len(sent) == 2
    body = sent[0]["text"]
    assert sent[0]["chat_id"] == 555  # receipt to the sender
    assert "Перевод выполнен" in body
    assert "999" in body  # recipient mention carries the id in its href

    dm = sent[1]
    assert dm["chat_id"] == 999  # DM to the recipient
    assert "перевели" in dm["text"].lower()
    assert "95" in dm["text"]  # net amount received

    assert await _balance(registry, 555) == 400  # -100 gross
    assert await _balance(registry, 999) == 95  # +95 net


async def test_send_survives_a_recipient_who_blocked_the_bot(
    make_wired: WiredFactory,
) -> None:
    """The courtesy DM is best-effort and must stay that way.

    Most recipients of a group ``/send`` have never opened a DM with
    the bot, so the ping is refused outright. The transfer is already
    committed (the handler checkpoints before it renders) and the
    sender has already been shown the receipt —
    letting that refusal out would undo a transfer both parties can
    see. It is caught, and logged rather than dropped in silence.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)

    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    seen: list[int] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        name = type(method).__name__
        if name == "GetMe":
            return TelegramUser(id=1, is_bot=True, first_name="bot")
        if name == "GetChatMember":
            # The recipient isn't in this chat — the handler's own
            # fail-open branch, unrelated to what this test pins.
            raise TelegramBadRequest(method=method, message="Bad Request: user not found")
        seen.append(method.chat_id)
        if method.chat_id == 999:
            raise TelegramForbiddenError(method=method, message="bot can't initiate conversation")
        return Message(
            message_id=1,
            date=datetime(2024, 1, 1, tzinfo=UTC),
            chat=Chat(id=method.chat_id, type="private"),
            from_user=TelegramUser(id=1, is_bot=True, first_name="bot"),
            text=method.text,
        )

    bot.session.make_request = fake_make_request  # type: ignore[method-assign,assignment]

    await dispatcher.feed_update(bot, _update("/send 999 100"))

    assert seen == [555, 999]  # receipt delivered, DM attempted and refused
    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 95


async def test_send_keeps_a_transfer_a_broken_dm_would_undo(make_wired: WiredFactory) -> None:
    """The receipt is a promise, so the transfer must outlive what follows.

    The courtesy DM catches ``TelegramAPIError`` — a recipient who never
    opened a chat with the bot is ordinary. It cannot catch a transport
    failure underneath that layer, and without the handler's checkpoint
    such a failure reached the middleware, which rolled the session back
    *after* the sender had been shown the success card: money on screen
    and nowhere else. The global errors router catches the crash one
    layer further out and paints the generic failure reply, so nothing
    escapes ``feed_update`` — but the rollback has already happened by
    then, which is the window this test guards.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)

    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    seen: list[int] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        name = type(method).__name__
        if name == "GetMe":
            return TelegramUser(id=1, is_bot=True, first_name="bot")
        if name == "GetChatMember":
            raise TelegramBadRequest(method=method, message="Bad Request: user not found")
        seen.append(method.chat_id)
        if method.chat_id == 999:
            # Below aiogram's own error taxonomy, so nothing in the
            # handler catches it — and nothing should.
            raise RuntimeError("synthetic transport failure")
        return Message(
            message_id=1,
            date=datetime(2024, 1, 1, tzinfo=UTC),
            chat=Chat(id=method.chat_id, type="private"),
            from_user=TelegramUser(id=1, is_bot=True, first_name="bot"),
            text=method.text,
        )

    bot.session.make_request = fake_make_request  # type: ignore[method-assign,assignment]

    await dispatcher.feed_update(bot, _update("/send 999 100"))

    assert seen[:2] == [555, 999]  # receipt delivered, then the DM blew up
    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 95


async def test_send_does_not_promise_success_before_the_dm_lookup(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed DB read must not land after the receipt.

    Reading the recipient's language used to sit inside the same
    ``suppress(Exception)`` as the DM send. A failing statement leaves
    the session unusable, so the middleware's commit blew up *after*
    the sender had been shown "Перевод выполнен" — money that moved on
    screen and nowhere else. The read runs before the receipt now, so
    the only card the sender can see is the error one.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    original = EconomyRepo.get
    reads: list[int] = []

    async def failing_get(self: EconomyRepo, user_id: int) -> Any:
        # ``TransferService`` reads the recipient first, to decide the
        # outcome; the handler's own read — the one this test is about —
        # is the second. Failing that one leaves the transfer committed
        # in the session, which is exactly the state the ordering guards.
        if user_id == 999:
            reads.append(user_id)
            if len(reads) > 1:
                raise SQLAlchemyError("recipient wallet read failed")
        return await original(self, user_id)

    monkeypatch.setattr(EconomyRepo, "get", failing_get)

    await dispatcher.feed_update(bot, _update("/send 999 100"))

    assert not any("Перевод выполнен" in m["text"] for m in sent)
    # Read straight off the table — ``EconomyRepo.get`` is patched.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        sender = await session.get(EconomyUser, 555)
        recipient = await session.get(EconomyUser, 999)
        assert sender is not None and sender.balance == 500
        assert recipient is not None and recipient.balance == 0


async def test_send_with_active_vip_halves_tax_end_to_end(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Stage 14 VIP read flowing into TransferEffects → tax math.
    Active vip_till + 5% base + 50% discount = 2% effective →
    100 * 0.025 = 2.5 floored to 2. Sender -100, recipient +98."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    future = datetime(2099, 1, 1, tzinfo=UTC).timestamp()
    await _seed_wallet(registry, 555, balance=500, vip_till=future)
    await _seed_wallet(registry, 999, balance=0)
    capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send 999 100"))

    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 98  # +98 net (100 - 2 tax)


async def test_send_self_transfer_rejected_no_mutation(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """SELF_TRANSFER outcome → "can't gift yourself" card, no debit."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send 555 100"))
    assert "Себе переводить нельзя" in sent[0]["text"]
    assert await _balance(registry, 555) == 500


async def test_send_to_unknown_recipient_renders_recipient_missing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """NO_RECIPIENT_WALLET → user-friendly "ask them to /start"
    card. Sender wallet auto-created by the handler but balance
    untouched (welcome credit is the only delta)."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send 999 100"))
    assert "Получатель не найден" in sent[0]["text"]


async def test_send_insufficient_funds_renders_specific_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Sender has 50, tries to send 100 → INSUFFICIENT_FUNDS card,
    no mutation. Pins the Python pre-check + the SQL guard's
    collapse to the same outcome."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=50)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send 999 100"))
    assert "Недостаточно средств" in sent[0]["text"]
    assert await _balance(registry, 555) == 50
    assert await _balance(registry, 999) == 0


async def test_send_missing_args_renders_usage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bare ``/send`` or single arg → usage card. Parse-time
    rejection, no service call."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send"))
    assert "Использование" in sent[0]["text"]


async def test_send_non_numeric_amount_rejected_at_parser(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/send 999 abc`` → invalid-amount card. Parser catches
    before any DB I/O — the service-level INVALID_AMOUNT branch
    is the defensive floor, not the primary surface."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send 999 abc"))
    assert "положительным" in sent[0]["text"]


async def test_send_zero_amount_rejected_at_parser(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send 999 0"))
    assert "положительным" in sent[0]["text"]


async def test_send_in_group_chat_numeric_form_owned(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-015: ``/send <id> <amount>`` in a supergroup is now owned by
    the new pipeline (router filter dropped). The transfer must
    succeed end-to-end — proves group calls reach the same service
    flow as private."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/send 999 100", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert "Перевод выполнен" in sent[0]["text"]
    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 95


async def _seed_user(
    registry: Any,
    *,
    user_id: int,
    username: str | None,
) -> None:
    """Seed ``users.users`` directly — bypasses any future touch-flow
    side effects so the username resolution path is exercised in
    isolation."""
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(UserRow(user_id=user_id, username=username, first_name="X"))
        await session.commit()


async def test_send_at_username_resolves_and_transfers(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Stage 18 happy path: ``/send @alice 100`` → UsersRepo lookup
    resolves to user_id 999 → numeric transfer pipeline. Pins the
    new SessionMiddleware → UsersRepo → handler wiring end-to-end
    (no fakes between the dispatcher and the service write)."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_user(registry, user_id=999, username="alice")
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send @alice 100"))

    assert "Перевод выполнен" in sent[0]["text"]
    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 95


async def test_send_at_username_unknown_renders_recipient_missing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``@unknown`` → users.username lookup returns None → same
    NO_RECIPIENT_WALLET copy as the numeric-id-not-in-wallets case.
    Sender balance untouched (no debit), no wallet auto-creation
    for the would-be recipient (resolution failed before service)."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send @unknown 100"))

    assert "Получатель не найден" in sent[0]["text"]
    assert await _balance(registry, 555) == 500
    # Recipient never seen → still no economy row for them.
    assert await _balance(registry, 999) is None


async def test_send_at_username_case_insensitive_match(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/send @ALICE 100`` resolves a user stored as ``alice``.
    Pins the legacy ``lower(username) = ?`` semantics — Telegram
    treats usernames case-insensitively at the protocol level, and
    the strangler port must preserve that or break a fraction of
    real /send calls in production."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_user(registry, user_id=999, username="alice")
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send @ALICE 100"))

    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 95


async def test_send_at_own_username_hits_self_transfer(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/send @my_handle 100`` where ``my_handle`` resolves to the
    sender → SELF_TRANSFER, not NO_RECIPIENT. Self-check must run
    AFTER username resolution: legacy semantics are "can't gift
    yourself regardless of how you addressed yourself"."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_user(registry, user_id=555, username="sam")
    await _seed_wallet(registry, 555, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/send @sam 100"))

    assert "Себе переводить нельзя" in sent[0]["text"]
    assert await _balance(registry, 555) == 500


async def test_send_rate_limit_blocks_the_seventh_rapid_call(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 19 token-bucket gate: with the legacy posture (burst 6,
    one slot back every 10s — bot.py:2552) seven rapid /send calls
    produce six successful transfers and one cool-down reply. The
    seventh call must NOT mutate the wallet — that's the whole reason
    the gate exists BEFORE EconomyMiddleware in the router stack.

    Time is frozen via ``time.monotonic`` monkeypatch (no freezegun
    dependency) so the test runs in milliseconds without flakiness
    on slow CI. Same shape as the throttling middleware's unit
    tests.
    """
    import telegram_invite_bot.middlewares._bucket_rate_limit as rl_mod  # patch the shared clock

    clock = {"t": 1000.0}
    monkeypatch.setattr(rl_mod, "monotonic", lambda: clock["t"])

    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    # Balance covers all seven debits if they all went through — so
    # the post-test 400 assertion proves the seventh was BLOCKED, not
    # silently failed at the service layer for some other reason.
    await _seed_wallet(registry, 555, balance=1_000)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    for _ in range(7):
        await dispatcher.feed_update(bot, _update("/send 999 100"))

    # Six success receipts + six recipient DMs (#16) + one cool-down.
    success_count = sum("Перевод выполнен" in s["text"] for s in sent)
    cooldown_count = sum("Слишком часто" in s["text"] for s in sent)
    assert success_count == 6
    assert cooldown_count == 1
    # The cool-down is always the LAST message (the bucket starts full →
    # first six admit + DM, seventh rejects with no DM after it).
    assert "Слишком часто" in sent[-1]["text"]

    # Wallet shows 6 × -100 (gross), NOT 7 × -100. The seventh
    # transfer never reached the service.
    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 570  # 6 × +95 net


async def test_send_rate_limit_recovers_after_refill_window(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After exhausting the bucket, advancing the clock by 10s (one
    refill period at the legacy 6/min rate) re-admits exactly one
    transfer. Pins that the gate is a TOKEN BUCKET (continuous
    refill) and not a fixed-window counter (which would only
    re-admit at a clock-aligned reset boundary)."""
    import telegram_invite_bot.middlewares._bucket_rate_limit as rl_mod  # patch the shared clock

    clock = {"t": 2000.0}
    monkeypatch.setattr(rl_mod, "monotonic", lambda: clock["t"])

    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=1_000)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    # Burn the burst.
    for _ in range(6):
        await dispatcher.feed_update(bot, _update("/send 999 10"))
    # Seventh at t=2000 → rejected.
    await dispatcher.feed_update(bot, _update("/send 999 10"))
    assert "Слишком часто" in sent[-1]["text"]

    # Advance one refill period; next call admits again. The admitted
    # transfer emits a receipt followed by the recipient DM (#16), so the
    # receipt is the second-to-last message, not the very last.
    clock["t"] += 10.0
    await dispatcher.feed_update(bot, _update("/send 999 10"))
    assert any("Перевод выполнен" in s["text"] for s in sent[-2:])


# ---------------------------------------------------------------------------
# T-015 — group form: reply-to-message recipient
# ---------------------------------------------------------------------------


def _reply_update(
    text: str,
    *,
    reply_to_user_id: int,
    user_id: int = 555,
    chat_type: str = "supergroup",
    chat_id: int = -100777,
) -> Update:
    """Group-default update with a ``reply_to_message`` envelope.

    The reply form is the legacy primary group use site (typing
    ``/send 100`` while replying to a member's message); the helper
    keeps the test body free of per-call envelope bookkeeping.
    """
    return make_message_update(
        text,
        user_id=user_id,
        first_name="Sam",
        language_code="ru",
        chat_type=chat_type,
        chat_id=chat_id,
        reply_to_user_id=reply_to_user_id,
    )


async def test_send_reply_form_transfers_to_reply_target(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-015 happy path: ``/send 100`` while replying to user 999's
    message → recipient resolved from ``reply_to_message.from_user.id``,
    transfer goes through TransferService unchanged."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _reply_update("/send 100", reply_to_user_id=999))

    assert "Перевод выполнен" in sent[0]["text"]
    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 95


async def test_send_reply_to_self_hits_self_transfer(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Replying to one's own message → SELF_TRANSFER, not a silent
    pass-through. Pins the rule that self-check runs at the service
    floor regardless of recipient resolution path."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _reply_update("/send 100", reply_to_user_id=555))

    assert "Себе переводить нельзя" in sent[0]["text"]
    assert await _balance(registry, 555) == 500


async def test_send_reply_to_bot_falls_back_to_arg_parse(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Replying to a bot message must NOT silently retarget the
    transfer to the bot — legacy refuses bot transfers, and the
    handler skips the reply arm when ``reply_to_message.from_user.is_bot``
    is true. With only ``/send 100`` (no explicit recipient) the
    handler falls back to the usage card, NOT to a bot debit.

    Built by post-mutating the model_dump path: aiogram doesn't expose
    an ``is_bot=True`` shortcut on the test helper, so we set it
    directly on the validated payload.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    sent = capture_outgoing(bot)

    update = make_message_update(
        "/send 100",
        user_id=555,
        first_name="Sam",
        language_code="ru",
        chat_type="supergroup",
        chat_id=-100777,
        reply_to_user_id=12345,
        reply_to_is_bot=True,
    )
    await dispatcher.feed_update(bot, update)

    # With reply skipped + only one positional arg, parser returns
    # None and the handler emits the usage card. NOT a 100-coin
    # debit to user 12345.
    assert "Использование" in sent[0]["text"]
    assert await _balance(registry, 555) == 500
    assert await _balance(registry, 12345) is None


async def test_send_reply_form_works_in_private_chat(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Reply form isn't group-exclusive: a private chat replying to a
    forwarded message (or any inline thread) gets the same shortcut.
    Rare, but the parser doesn't gate on chat type and that consistency
    matters for power users."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, _reply_update("/send 50", reply_to_user_id=999, chat_type="private", chat_id=555)
    )

    assert "Перевод выполнен" in sent[0]["text"]
    assert await _balance(registry, 555) == 450


async def test_send_explicit_id_takes_priority_over_reply(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """If the user typed BOTH a numeric recipient AND replied to a
    message, the explicit argument wins. The parser only chooses the
    reply arm when the FIRST token parses as an amount — a two-token
    ``<id> <amount>`` form is unambiguous and overrides."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 777, balance=0)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _reply_update("/send 777 100", reply_to_user_id=999))

    assert "Перевод выполнен" in sent[0]["text"]
    # Explicit 777 got credited; reply target 999 did NOT.
    assert await _balance(registry, 777) == 95
    assert await _balance(registry, 999) == 0


# R-FIX-004 — bot recipient is refused on every resolution path
# -------------------------------------------------------------
# Legacy ``bot.py`` kept a global ``BOT_USER_IDS`` blocklist that
# guarded every /send target. The strangler port lost that gate
# on the numeric-id and @username branches — only the reply arm
# checked ``reply_to_message.from_user.is_bot``. A group admin
# could advertise a bot's numeric id (or its @handle) as a "prize
# wallet" and victims would burn coins into a wallet they can
# never spend from. Three tests pin the gate now lives on all
# three paths.
#
# Telegram's ``getChat`` payload does not expose ``is_bot`` (it's
# a property of ``User``, not ``Chat``). The handler uses the
# platform invariant that bot usernames must end in ``"bot"``,
# so the test stub returns a ``Chat`` with ``username="evilbot"``
# for the bot-target id and the default ``"human"`` for everyone
# else. The reply path is exercised separately via the existing
# ``reply_to_is_bot=True`` envelope field.


def _bot_aware_capture(
    monkeypatch: pytest.MonkeyPatch,
    bot: Any,
    sink: list[dict[str, Any]],
    bot_target_id: int,
    *,
    self_bot_id: int = 0,
    chat_member_raises: bool = False,
) -> None:
    """Like ``capture_outgoing`` but overlays a ``GetChatMember`` stub
    that flags ``bot_target_id`` as a bot via the authoritative
    ``ChatMember.user.is_bot`` field.

    Defined inline instead of as a conftest fixture: it's R-FIX-004
    specific, and the conftest helper deliberately stays bot-free
    so the other 20 send tests don't accidentally trip the gate.

    R-FIX-004-fp: switched from ``GetChat`` + ``username.endswith("bot")``
    (which mis-classified humans whose handle merely ends in those
    three characters, e.g. ``MyCoolRobot``) to ``GetChatMember`` so
    only real bots are rejected.

    ``self_bot_id`` lets a test pin the bot's own id for the
    self-id short-circuit.

    ``chat_member_raises=True`` simulates a TelegramAPIError (target
    not in this chat) — used to pin that the handler fails open
    rather than mis-detecting via a heuristic.
    """
    from aiogram.exceptions import TelegramBadRequest as _TBR
    from aiogram.types import Chat as _Chat
    from aiogram.types import ChatMemberMember as _CMM
    from aiogram.types import Message as _Msg
    from aiogram.types import User as _User

    def _synth(chat_id: int, text: str | None) -> _Msg:
        return _Msg(
            message_id=1,
            date=datetime(2024, 1, 1, tzinfo=UTC),
            chat=_Chat(id=chat_id, type="private"),
            from_user=_User(id=0, is_bot=True, first_name="bot"),
            text=text,
        )

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ASYNC109, ARG001
    ) -> Any:
        name = type(method).__name__
        if name == "SendMessage":
            sink.append({"kind": "text", "chat_id": method.chat_id, "text": method.text})
            return _synth(method.chat_id, method.text)
        if name == "SendPhoto":
            cap = getattr(method, "caption", None)
            sink.append({"kind": "photo", "chat_id": method.chat_id, "caption": cap})
            return _synth(method.chat_id, cap or "ok")
        if name == "GetMe":
            return _User(id=self_bot_id, is_bot=True, first_name="bot")
        if name == "GetChatMember":
            if chat_member_raises:
                raise _TBR(method=method, message="user not found")
            uid = int(method.user_id)
            is_bot = uid == int(bot_target_id)
            user = _User(
                id=uid,
                is_bot=is_bot,
                first_name="EvilBot" if is_bot else "X",
                username="evilbot" if is_bot else None,
            )
            return _CMM(user=user)
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


@pytest.mark.asyncio
async def test_send_to_bot_via_numeric_id_rejected(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/send <bot_id> 100`` → recipient classifies as a bot via the
    ``getChat`` probe → handler refuses, sender balance untouched.
    Pins R-FIX-004 on the numeric-id path."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    sent: list[dict[str, Any]] = []
    _bot_aware_capture(monkeypatch, bot, sent, bot_target_id=999)

    await dispatcher.feed_update(bot, _update("/send 999 100"))

    assert len(sent) == 1
    assert "Переводы ботам запрещены" in sent[0]["text"]
    assert await _balance(registry, 555) == 500
    # No wallet was created for the bot — the refusal happens before
    # the service touches the economy DB.
    assert await _balance(registry, 999) is None


@pytest.mark.asyncio
async def test_send_to_bot_via_at_username_rejected(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/send @evilbot 100`` → UsersRepo resolves to a numeric id →
    ``getChat`` probe flags it as a bot → handler refuses on the
    @username path. Same gate, different resolver."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_user(registry, user_id=999, username="evilbot")
    await _seed_wallet(registry, 555, balance=500)
    sent: list[dict[str, Any]] = []
    _bot_aware_capture(monkeypatch, bot, sent, bot_target_id=999)

    await dispatcher.feed_update(bot, _update("/send @evilbot 100"))

    assert len(sent) == 1
    assert "Переводы ботам запрещены" in sent[0]["text"]
    assert await _balance(registry, 555) == 500
    assert await _balance(registry, 999) is None


@pytest.mark.asyncio
async def test_send_reply_to_bot_message_does_not_credit_bot(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The reply path already filtered ``reply_to_message.from_user.is_bot``
    before R-FIX-004 — pin that the legacy gate still holds after the
    new ``getChat`` plumbing. ``/send 100`` while replying to a bot
    message falls through to the parser usage card; no debit, no
    bot-wallet crediting."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    sent = capture_outgoing(bot)

    update = make_message_update(
        "/send 100",
        user_id=555,
        first_name="Sam",
        language_code="ru",
        chat_type="supergroup",
        chat_id=-100777,
        reply_to_user_id=12345,
        reply_to_is_bot=True,
    )
    await dispatcher.feed_update(bot, update)

    # Reply skipped by ``from_user.is_bot`` filter → only one positional
    # arg → usage card. NOT a 100-coin debit into bot 12345.
    assert "Использование" in sent[0]["text"]
    assert await _balance(registry, 555) == 500
    assert await _balance(registry, 12345) is None


# ---------------------------------------------------------------------------
# R-FIX-004-fp: false-positive on humans whose handle ends in "bot".
# Iteration 1 used ``username.endswith("bot")`` which mis-classified
# real users like ``MyCoolRobot`` / ``Doctorbot`` / ``hellochatbot`` as
# bots (Python's ``endswith`` is a literal-suffix check —
# ``"robot".endswith("bot") is True``). The follow-up replaces the
# heuristic with the authoritative ``ChatMember.user.is_bot`` field,
# which only flags bots that Telegram itself flagged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_human_with_bot_suffix_username_is_accepted(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/send 999 100`` where user 999 is a HUMAN with username
    ``MyCoolRobot`` — the old endswith heuristic rejected; the new
    ``is_bot`` check accepts. Recipient is credited."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent: list[dict[str, Any]] = []
    # bot_target_id=-1 → no user matches → every GetChatMember returns
    # a human ChatMemberMember. The "MyCoolRobot" handle is irrelevant
    # because the new code reads ``is_bot``, not ``username``.
    _bot_aware_capture(monkeypatch, bot, sent, bot_target_id=-1)

    await dispatcher.feed_update(bot, _update("/send 999 100"))

    # Transfer landed (success card, not the bot-recipient refusal).
    assert any("Перевод выполнен" in m["text"] for m in sent)
    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 95


@pytest.mark.asyncio
async def test_send_to_actual_bot_in_same_chat_rejected(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/send 999 100`` where user 999 is flagged by Telegram as a
    bot via ``GetChatMember`` — refused. Pins the authoritative
    is_bot path."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    sent: list[dict[str, Any]] = []
    _bot_aware_capture(monkeypatch, bot, sent, bot_target_id=999)

    await dispatcher.feed_update(bot, _update("/send 999 100"))

    assert len(sent) == 1
    assert "Переводы ботам запрещены" in sent[0]["text"]
    assert await _balance(registry, 555) == 500
    assert await _balance(registry, 999) is None


@pytest.mark.asyncio
async def test_send_to_self_bot_id_rejected(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/send <this_bot_id> 100`` — short-circuits on
    ``to_id == bot.me().id`` BEFORE the GetChatMember probe. Pins the
    self-id refusal path."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    sent: list[dict[str, Any]] = []
    # The bot's id is 7777; the target is also 7777. We set
    # ``bot_target_id=-1`` so GetChatMember would say "not a bot",
    # but the self-id short-circuit refuses before that probe runs.
    _bot_aware_capture(monkeypatch, bot, sent, bot_target_id=-1, self_bot_id=7777)

    await dispatcher.feed_update(bot, _update("/send 7777 100"))

    assert len(sent) == 1
    assert "Переводы ботам запрещены" in sent[0]["text"]
    assert await _balance(registry, 555) == 500


@pytest.mark.asyncio
async def test_send_chat_member_raises_falls_open(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GetChatMember`` raises (target not in this chat) — handler
    falls open and lets the transfer proceed. Pins the documented
    limitation that cross-chat targets cannot be is_bot-checked
    (no Chat-shaped is_bot field exists in the Telegram API)."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], economy_config=_TAXED
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent: list[dict[str, Any]] = []
    _bot_aware_capture(monkeypatch, bot, sent, bot_target_id=-1, chat_member_raises=True)

    await dispatcher.feed_update(bot, _update("/send 999 100"))

    assert any("Перевод выполнен" in m["text"] for m in sent)
    assert await _balance(registry, 555) == 400
    assert await _balance(registry, 999) == 95


# --- #193: the Settings → middleware → service wiring itself ----------
#
# Every case above supplies ``_TAXED`` explicitly, so together they
# prove the rate *travels*. The two below pin the other two halves of
# the ticket: what a stock install does (nothing — legacy charged no
# tax), and where the cut lands once an operator turns it on.


async def test_send_with_default_settings_charges_no_tax(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No ``COINS_TRANSFER_TAX`` set → the recipient gets the full 100.

    Legacy's own default is zero — ``"coins_transfer_tax": 0`` at
    bot.py:2550, and the operator's live ``settings.json`` carries that
    0 as well — so a stock install must move coins one-for-one. The
    port used to hard-code ``0.05`` inside :class:`TransferConfig`
    under a docstring claiming 0.05 *was* the legacy default, which
    charged every sender a 5% cut legacy never charged. Asserting the
    absence of the ``type='tax'`` row (not just the balances) is what
    makes this a parity test rather than an arithmetic one.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/send 999 100"))
    assert result is not UNHANDLED
    assert "Перевод выполнен" in sent[0]["text"]

    assert await _balance(registry, 555) == 400  # -100, nothing skimmed
    assert await _balance(registry, 999) == 100  # +100, the full amount

    rows = await _ledger(registry)
    assert [r.type for r in rows] == ["transfer"]
    assert rows[0].amount == 100


async def test_send_tax_lands_in_the_admin_treasury(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """With a tax configured, the cut is banked — not burned.

    This is the ticket's headline defect. ``EconomyMiddleware`` was the
    only place in ``src/`` that builds a :class:`TransferService`, and
    it passed no config at all, so ``admin_user_id`` was permanently
    ``None``; transfer_service.py then skipped the treasury credit and
    wrote the row as ``transfer_tax_burned``. Every taxed coin was
    destroyed. Legacy credits ``ADMIN_CHAT_ID`` (bot.py:10270-10273).

    The treasury wallet is seeded at 0 rather than left to
    ``get_or_create`` so the assertion is ``== 5`` and not
    ``== WELCOME_BALANCE + 5``.
    """
    treasury = 7
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        economy_config=_TAXED,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), ADMIN_CHAT_ID=treasury),
    )
    await _seed_wallet(registry, 555, balance=500)
    await _seed_wallet(registry, 999, balance=0)
    await _seed_wallet(registry, treasury, balance=0)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/send 999 100"))
    assert result is not UNHANDLED
    assert "Перевод выполнен" in sent[0]["text"]

    assert await _balance(registry, 555) == 400  # -100 gross
    assert await _balance(registry, 999) == 95  # +95 net
    assert await _balance(registry, treasury) == 5  # +5 banked, not burned

    rows = await _ledger(registry)
    assert [r.type for r in rows] == ["transfer", "tax"]
    tax_row = rows[1]
    assert tax_row.reason == "transfer_tax"  # NOT "transfer_tax_burned"
    assert tax_row.from_id == 555
    assert tax_row.to_id == treasury
    assert tax_row.amount == 5
