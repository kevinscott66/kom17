"""Unit tests for the ``/topup`` menu + Telegram Stars flow (A1: L-80/L-84).

Pins:

* the command surface (``/topup`` + ``/buy_coins`` + RU ``/пополнить``
  — deliberately NOT ``/buy``, which is the shop);
* the Stars invoice wire contract (``currency="XTR"``, EMPTY
  ``provider_token``, one LabeledPrice whose amount is the star count,
  payload ``stars_{uid}_{stars}_{coins}``);
* payload validation against the server-side pack table (legacy
  trusted callback-carried amounts, bot.py:18070/18248 — we don't);
* pre-checkout approve/refuse;
* successful_payment dispatch → the shared credit pipeline with
  ``provider="stars"`` and ``external_id=telegram_payment_charge_id``;
* degraded rows for unconfigured methods.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command

from telegram_invite_bot.handlers import topup as topup_mod
from telegram_invite_bot.handlers.chat_scope import scoped_worker
from telegram_invite_bot.handlers.topup import (
    STARS_PACKS,
    handle_pre_checkout,
    handle_rollypay_amount,
    handle_stars_pack,
    handle_successful_payment,
    handle_topup_method,
    parse_stars_payload,
)
from telegram_invite_bot.keyboards.builders import (
    TopupMethod,
    TopupRollyPayAmount,
    TopupStarsPack,
)
from telegram_invite_bot.services.payments.base import Provider
from telegram_invite_bot.services.payments.crypto_invoices import (
    CryptoTopupService,
    TopupInvoiceOutcome,
)
from telegram_invite_bot.services.payments.rollypay_invoices import (
    RUB_AMOUNTS,
    RollyPayTopup,
    RollyPayTopupService,
)
from telegram_invite_bot.services.payments_service import CreditOutcome
from telegram_invite_bot.webhook.metrics import PAYMENT_CREDIT_FAILURES


def _crypto_service(token: str | None) -> CryptoTopupService:
    async def _resolver() -> str | None:
        return token

    return CryptoTopupService(_resolver)


def _rollypay_service(api_key: str | None = None) -> RollyPayTopupService:
    return RollyPayTopupService(api_key)


def _settings(
    *,
    yookassa: bool = False,
    stripe: bool = False,
    rollypay: bool = False,
    admin_chat_id: int = 0,
) -> Any:
    return SimpleNamespace(
        payments=SimpleNamespace(
            yookassa_configured=yookassa,
            stripe_configured=stripe,
            rollypay_configured=rollypay,
            rollypay_api_key=SimpleNamespace(get_secret_value=lambda: "rp_key")
            if rollypay
            else None,
        ),
        economy=SimpleNamespace(referral_commission_percent=10, developer_commission_percent=5),
        bot=SimpleNamespace(admin_chat_id=admin_chat_id),
    )


class FakeMessage:
    def __init__(self, *, payload: str | None = None, charge_id: str = "ch_1") -> None:
        self.from_user = SimpleNamespace(id=42)
        self.chat = SimpleNamespace(id=42, type="private")
        self.replies: list[str] = []
        self.successful_payment = (
            SimpleNamespace(
                invoice_payload=payload,
                telegram_payment_charge_id=charge_id,
                total_amount=50,
                currency="XTR",
            )
            if payload is not None
            else None
        )

    async def reply(self, text: str, **_kwargs: Any) -> None:
        self.replies.append(text)


class FakeCallbackMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=42)
        self.message_id = 7
        self.edits: list[tuple[str, Any]] = []

    async def edit_text(self, text: str, reply_markup: Any = None) -> None:
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.message = FakeCallbackMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.invoices: list[dict[str, Any]] = []
        self.sent: list[tuple[int, str]] = []
        self._fail = fail

    async def send_invoice(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("telegram says no")
        self.invoices.append(kwargs)

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _raw_key_t(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render i18n keys raw so assertions don't depend on YAML content."""

    def fake_t(key: str, _lang: str | None = None, **_kwargs: Any) -> str:
        return key

    monkeypatch.setattr(topup_mod, "t", fake_t)


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def test_parse_stars_payload_accepts_every_server_pack() -> None:
    for stars, coins in STARS_PACKS:
        assert parse_stars_payload(f"stars_42_{stars}_{coins}") == (42, stars, coins)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "shop_42_50_550",  # foreign prefix
        "stars_42_50",  # missing part
        "stars_x_50_550",  # non-numeric uid
        "stars_0_50_550",  # uid must be positive
        "stars_42_50_999999",  # not a server-side pack (tampered coins)
        "stars_42_51_550",  # not a server-side pack (tampered stars)
    ],
)
def test_parse_stars_payload_rejects_foreign_or_tampered(payload: str) -> None:
    assert parse_stars_payload(payload) is None


def test_stars_packs_mirror_legacy() -> None:
    # bot.py:3181 PAYMENT_STARS_PACKS — frozen product ratio.
    assert STARS_PACKS == ((50, 550), (100, 1100), (250, 2750), (500, 5500))


# ---------------------------------------------------------------------------
# Router surface
# ---------------------------------------------------------------------------


def test_router_registers_topup_aliases_not_buy() -> None:
    # ``registry`` is only read by the middleware, which this test never
    # exercises; ``_settings`` is a duck-typed double.
    #
    # ``scoped_worker``: since #123 ``build_router`` returns the wrapper
    # router pairing the module with its chat-scope refusal twin. Reading
    # the module's own router is what keeps the assertion honest — the
    # refusal twin registers the very same words, so scanning the wrapper
    # would make this test pass on the refusal alone.
    router = scoped_worker(
        topup_mod.build_router(
            registry=cast("Any", None),
            settings=_settings(),
            crypto_topup=_crypto_service(None),
            rollypay_topup=_rollypay_service(),
        )
    )
    tokens: set[str] = set()
    for handler in router.message.handlers:
        for filter_obj in handler.filters or []:
            if isinstance(filter_obj.callback, Command):
                tokens.update(c for c in filter_obj.callback.commands if isinstance(c, str))
    assert tokens == {"topup", "buy_coins", "пополнить"}
    assert "buy" not in tokens  # /buy belongs to the shop


# ---------------------------------------------------------------------------
# Stars invoice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stars_pack_sends_xtr_invoice_with_empty_provider_token() -> None:
    callback: Any = FakeCallback()
    bot = FakeBot()
    await handle_stars_pack(callback, TopupStarsPack(idx=1), bot, "ru")  # type: ignore[arg-type]

    assert len(bot.invoices) == 1
    invoice = bot.invoices[0]
    stars, coins = STARS_PACKS[1]
    assert invoice["currency"] == "XTR"
    assert invoice["provider_token"] == ""  # Stars contract: EMPTY token
    assert invoice["payload"] == f"stars_42_{stars}_{coins}"
    prices = invoice["prices"]
    assert len(prices) == 1
    assert prices[0].amount == stars
    assert callback.answers == [(None, False)]


@pytest.mark.asyncio
async def test_stars_pack_out_of_range_index_refused() -> None:
    callback: Any = FakeCallback()
    bot = FakeBot()
    await handle_stars_pack(callback, TopupStarsPack(idx=99), bot, "ru")  # type: ignore[arg-type]
    assert bot.invoices == []
    assert callback.answers == [("h_topup_bad_request", True)]


@pytest.mark.asyncio
async def test_stars_invoice_send_failure_alerts_not_raises() -> None:
    callback: Any = FakeCallback()
    bot = FakeBot(fail=True)
    await handle_stars_pack(callback, TopupStarsPack(idx=0), bot, "ru")  # type: ignore[arg-type]
    assert callback.answers == [("h_topup_invoice_failed", True)]


# ---------------------------------------------------------------------------
# Pre-checkout
# ---------------------------------------------------------------------------


class FakePreCheckout:
    def __init__(self, payload: str) -> None:
        self.invoice_payload = payload
        self.from_user = SimpleNamespace(id=42)
        self.answered: list[tuple[bool, str | None]] = []

    async def answer(self, ok: bool, error_message: str | None = None) -> None:
        self.answered.append((ok, error_message))


@pytest.mark.asyncio
async def test_pre_checkout_ok_for_valid_payload() -> None:
    q: Any = FakePreCheckout("stars_42_50_550")
    await handle_pre_checkout(q, "ru")
    assert q.answered == [(True, None)]


@pytest.mark.asyncio
async def test_pre_checkout_refuses_foreign_payload() -> None:
    q: Any = FakePreCheckout("stars_42_50_999999")
    await handle_pre_checkout(q, "ru")
    assert q.answered == [(False, "h_topup_precheckout_bad")]


# ---------------------------------------------------------------------------
# successful_payment dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_reply"),
    [
        (CreditOutcome.CREDITED, "balance_topup_ok"),
        (CreditOutcome.IDEMPOTENT, "payment_coins_already_added"),
        (CreditOutcome.CREDIT_REFUSED, "h_topup_credit_failed"),
    ],
)
async def test_successful_payment_replies_by_outcome(
    monkeypatch: pytest.MonkeyPatch,
    outcome: CreditOutcome,
    expected_reply: str,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_credit(**kwargs: Any) -> tuple[CreditOutcome, None]:
        captured.update(kwargs)
        return outcome, None

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    msg: Any = FakeMessage(payload="stars_42_50_550", charge_id="chg_777")
    await handle_successful_payment(msg, FakeBot(), "ru", None, _settings())  # type: ignore[arg-type]

    assert msg.replies == [expected_reply]
    # provider="stars" idempotency rides telegram_payment_charge_id.
    assert captured["charge_id"] == "chg_777"
    assert captured["user_id"] == 42
    assert captured["coins"] == 550


@pytest.mark.asyncio
async def test_successful_payment_credits_the_payer_not_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#297: a forwarded invoice must not top up somebody else's wallet.

    The payload records who asked for the invoice. An invoice message
    can be forwarded, and whoever receives it can pay it — at which
    point those two people are different, and crediting the payload
    means the person who spent the stars gets nothing while a stranger's
    balance goes up.
    """
    captured: dict[str, Any] = {}

    async def fake_credit(**kwargs: Any) -> tuple[CreditOutcome, None]:
        captured.update(kwargs)
        return CreditOutcome.CREDITED, None

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    # Minted for 42; the stars were actually spent by 99.
    msg: Any = FakeMessage(payload="stars_42_50_550", charge_id="chg_fwd")
    msg.from_user = SimpleNamespace(id=99)
    await handle_successful_payment(msg, FakeBot(), "ru", None, _settings())  # type: ignore[arg-type]

    assert captured["user_id"] == 99
    # The pack is still server-validated against STARS_PACKS, so unlike
    # the id it rides along from the payload unchanged.
    assert captured["coins"] == 550
    assert msg.replies == ["balance_topup_ok"]


@pytest.mark.asyncio
async def test_successful_payment_ignores_foreign_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_credit(**_kwargs: Any) -> tuple[CreditOutcome, None]:
        raise AssertionError("must not credit a foreign payload")

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    msg: Any = FakeMessage(payload="donation_1_2_3")
    await handle_successful_payment(msg, FakeBot(), "ru", None, _settings())  # type: ignore[arg-type]
    assert msg.replies == []


class LostReplyTargetMessage(FakeMessage):
    """A message whose reply target is gone but whose chat still works."""

    def __init__(self, *, chat_gone: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
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


@pytest.mark.asyncio
async def test_successful_payment_receipt_survives_a_lost_reply_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the invoice message must not turn a paid top-up into an error.

    The credit commits in its own transaction — Telegram has already
    taken the stars — so a raise out of this handler cannot undo it. All
    it does is reach ``handlers.errors`` and paint "⚠️ Произошла
    ошибка" over a top-up that worked, which invites the buyer to pay a
    second time.
    """

    async def fake_credit(**_kwargs: Any) -> tuple[CreditOutcome, None]:
        return CreditOutcome.CREDITED, None

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    msg: Any = LostReplyTargetMessage(payload="stars_42_50_550")
    await handle_successful_payment(msg, FakeBot(), "ru", None, _settings())  # type: ignore[arg-type]

    assert msg.replies == []
    assert msg.sends == ["balance_topup_ok"]


@pytest.mark.asyncio
async def test_successful_payment_keeps_the_credit_when_the_chat_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable buyer still keeps the coins Telegram charged for.

    The companion to the test above: here the fallback send fails too,
    so nobody sees the receipt. The handler logs and returns — it must
    not raise, because the money is real and already credited.
    """

    async def fake_credit(**_kwargs: Any) -> tuple[CreditOutcome, None]:
        return CreditOutcome.CREDITED, None

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    msg: Any = LostReplyTargetMessage(payload="stars_42_50_550", chat_gone=True)
    await handle_successful_payment(msg, FakeBot(), "ru", None, _settings())  # type: ignore[arg-type]

    assert msg.replies == []
    assert msg.sends == []


# ---------------------------------------------------------------------------
# Charged but not credited (#161)
# ---------------------------------------------------------------------------
#
# Stars is the one provider with no second chance: a webhook provider's
# failed credit at least leaves a row in somebody's dashboard that a
# reconciliation can find later, while ``successful_payment`` arrives
# exactly once and nothing re-sends it. (This block used to add "and a
# non-2xx, and a redelivery ladder" — false: ``webhook/payments.py``
# answers 200 on CREDIT_REFUSED/INVALID_AMOUNT. #296 gave that path its
# own owner alert rather than leaning on a ladder that is not there.)
# So the tests below pin BOTH halves of the alarm — the counter a
# dashboard can graph, and the DM that names the charge id the owner
# needs to fix it by hand.


def _failure_count(reason: str) -> float:
    counter = PAYMENT_CREDIT_FAILURES.labels(provider=Provider.STARS.value, reason=reason)
    return float(counter._value.get())  # noqa: SLF001


class DeafOwnerBot(FakeBot):
    """A bot whose owner DM always fails (blocked, deleted, wrong id)."""

    async def send_message(self, chat_id: int, text: str) -> None:
        raise TelegramForbiddenError(
            method=SimpleNamespace(),  # type: ignore[arg-type]
            message="Forbidden: bot was blocked by the user",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [CreditOutcome.CREDIT_REFUSED, CreditOutcome.INVALID_AMOUNT])
async def test_terminal_credit_failure_counts_and_alerts_the_owner(
    monkeypatch: pytest.MonkeyPatch, outcome: CreditOutcome
) -> None:
    """Stars taken, coins refused: the owner learns it, with the charge id.

    ``h_topup_credit_failed`` tells the payer something went wrong, which
    is necessary but not sufficient — it is not the payer's job to
    notice that Telegram kept their stars. The counter makes the fault
    graphable next to the webhook providers (same metric, same reason
    vocabulary) and the DM carries the one identifier a manual credit
    needs.
    """

    async def fake_credit(**_kwargs: Any) -> tuple[CreditOutcome, None]:
        return outcome, None

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    before = _failure_count(outcome.value)
    bot = FakeBot()
    msg: Any = FakeMessage(payload="stars_42_50_550", charge_id="chg_777")
    await handle_successful_payment(
        msg,
        bot,
        "ru",
        None,
        _settings(admin_chat_id=99),  # type: ignore[arg-type]
    )

    assert msg.replies == ["h_topup_credit_failed"]
    assert _failure_count(outcome.value) == before + 1
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 99
    assert "chg_777" in text
    assert outcome.value in text


@pytest.mark.asyncio
async def test_credit_crash_is_absorbed_counted_and_alerted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash inside the credit must not surface as the generic error card.

    Re-raising hands the message to ``handlers.errors``, which paints
    "⚠️ Произошла ошибка" — from the payer's seat indistinguishable from
    the top-up never having been taken, which invites paying twice. The
    handler swallows it instead, and pays for that silence by shouting at
    the owner under the same ``pipeline_crash`` reason the webhook uses.
    """

    async def fake_credit(**_kwargs: Any) -> tuple[CreditOutcome, None]:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    before = _failure_count("pipeline_crash")
    bot = FakeBot()
    msg: Any = FakeMessage(payload="stars_42_50_550", charge_id="chg_boom")
    await handle_successful_payment(
        msg,
        bot,
        "ru",
        None,
        _settings(admin_chat_id=99),  # type: ignore[arg-type]
    )

    assert msg.replies == ["h_topup_credit_failed"]
    assert _failure_count("pipeline_crash") == before + 1
    assert bot.sent and "chg_boom" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_uncredited_stars_are_counted_even_without_an_owner_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``ADMIN_CHAT_ID`` costs the DM, never the metric."""

    async def fake_credit(**_kwargs: Any) -> tuple[CreditOutcome, None]:
        return CreditOutcome.CREDIT_REFUSED, None

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    before = _failure_count("credit_refused")
    bot = FakeBot()
    msg: Any = FakeMessage(payload="stars_42_50_550")
    await handle_successful_payment(msg, bot, "ru", None, _settings())  # type: ignore[arg-type]

    assert _failure_count("credit_refused") == before + 1
    assert bot.sent == []
    assert msg.replies == ["h_topup_credit_failed"]


@pytest.mark.asyncio
async def test_a_failed_owner_alert_does_not_swallow_the_payer_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alert is a courtesy: if the DM dies, the handler carries on.

    Letting a ``TelegramForbiddenError`` out of the alert would drop the
    payer's own message and hand the update to ``handlers.errors`` —
    trading one lost signal for two.
    """

    async def fake_credit(**_kwargs: Any) -> tuple[CreditOutcome, None]:
        return CreditOutcome.CREDIT_REFUSED, None

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    before = _failure_count("credit_refused")
    msg: Any = FakeMessage(payload="stars_42_50_550")
    await handle_successful_payment(
        msg,
        DeafOwnerBot(),
        "ru",
        None,
        _settings(admin_chat_id=99),  # type: ignore[arg-type]
    )

    assert _failure_count("credit_refused") == before + 1
    assert msg.replies == ["h_topup_credit_failed"]


@pytest.mark.asyncio
async def test_owner_alert_clamps_a_hostile_charge_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long charge id must not push the alert past Telegram's 4096.

    ``telegram_payment_charge_id`` is Telegram's own string, so this is
    belt-and-braces rather than a live threat — but an alert that 400s is
    an alert nobody reads, and that is exactly the failure this whole
    path exists to prevent.
    """

    async def fake_credit(**_kwargs: Any) -> tuple[CreditOutcome, None]:
        return CreditOutcome.CREDIT_REFUSED, None

    monkeypatch.setattr(topup_mod, "credit_stars_payment", fake_credit)
    bot = FakeBot()
    msg: Any = FakeMessage(payload="stars_42_50_550", charge_id="A" * 5000)
    await handle_successful_payment(
        msg,
        bot,
        "ru",
        None,
        _settings(admin_chat_id=99),  # type: ignore[arg-type]
    )

    assert len(bot.sent[0][1]) < 4096


# ---------------------------------------------------------------------------
# Degraded methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crypto_method_degrades_without_token() -> None:
    callback: Any = FakeCallback()
    await handle_topup_method(
        callback,
        TopupMethod(method="crypto"),
        "ru",
        _crypto_service(None),
        _rollypay_service(),
        _settings(),
    )
    assert callback.message.edits[0][0] == "h_topup_crypto_not_configured"


@pytest.mark.asyncio
async def test_crypto_method_opens_asset_screen_with_token() -> None:
    callback: Any = FakeCallback()
    await handle_topup_method(
        callback,
        TopupMethod(method="crypto"),
        "ru",
        _crypto_service("tok"),
        _rollypay_service(),
        _settings(),
    )
    assert callback.message.edits[0][0] == "h_topup_crypto_title"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["yookassa", "stripe"])
async def test_card_methods_render_unavailable_when_unconfigured(method: str) -> None:
    callback: Any = FakeCallback()
    await handle_topup_method(
        callback,
        TopupMethod(method=method),
        "ru",
        _crypto_service(None),
        _rollypay_service(),
        _settings(),
    )
    assert callback.message.edits[0][0] == "h_topup_method_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "kwargs"),
    [("yookassa", {"yookassa": True}), ("stripe", {"stripe": True})],
)
async def test_card_methods_render_honest_copy_when_configured(
    method: str, kwargs: dict[str, bool]
) -> None:
    # v1: no in-bot checkout — but the webhook credit side IS live, so
    # the configured state renders the "external payments credit
    # automatically" copy rather than a fake checkout button.
    callback: Any = FakeCallback()
    await handle_topup_method(
        callback,
        TopupMethod(method=method),
        "ru",
        _crypto_service(None),
        _rollypay_service(),
        _settings(**kwargs),
    )
    assert callback.message.edits[0][0] == "h_topup_method_external"


@pytest.mark.asyncio
async def test_unknown_method_alerts() -> None:
    callback: Any = FakeCallback()
    await handle_topup_method(
        callback,
        TopupMethod(method="paypal"),
        "ru",
        _crypto_service(None),
        _rollypay_service(),
        _settings(),
    )
    assert callback.answers == [("h_topup_bad_request", True)]


# ---------------------------------------------------------------------------
# RollyPay checkout
# ---------------------------------------------------------------------------


class FakeRollyPay:
    """Stand-in for :class:`RollyPayTopupService` on the handler path."""

    def __init__(self, result: RollyPayTopup, *, ok: bool = True) -> None:
        self._result = result
        self._ok = ok
        self.calls: list[dict[str, Any]] = []

    def available(self) -> bool:
        return self._ok

    async def create_payment(self, **kwargs: Any) -> RollyPayTopup:
        self.calls.append(kwargs)
        return self._result


def _created(pay_url: str = "https://pay.rollypay.test/p/abc") -> RollyPayTopup:
    return RollyPayTopup(outcome=TopupInvoiceOutcome.CREATED, pay_url=pay_url, payment_id="pay_1")


@pytest.mark.asyncio
async def test_rollypay_method_degrades_without_key() -> None:
    callback: Any = FakeCallback()
    await handle_topup_method(
        callback,
        TopupMethod(method="rollypay"),
        "ru",
        _crypto_service(None),
        _rollypay_service(None),
        _settings(),
    )
    assert callback.message.edits[0][0] == "h_topup_rollypay_not_configured"


@pytest.mark.asyncio
async def test_rollypay_method_opens_amount_screen_with_key() -> None:
    callback: Any = FakeCallback()
    await handle_topup_method(
        callback,
        TopupMethod(method="rollypay"),
        "ru",
        _crypto_service(None),
        _rollypay_service("rp_key"),
        _settings(),
    )
    text, keyboard = callback.message.edits[0]
    assert text == "h_topup_rollypay_title"
    # One row per server-side amount + the back row; every amount row
    # carries an INDEX, never a rouble figure (money never rides the wire).
    assert len(keyboard.inline_keyboard) == len(RUB_AMOUNTS) + 1
    packed = [row[0].callback_data for row in keyboard.inline_keyboard[:-1]]
    assert packed == [TopupRollyPayAmount(amount=i).pack() for i in range(len(RUB_AMOUNTS))]


@pytest.mark.asyncio
async def test_rollypay_amount_screen_quotes_at_the_live_fix() -> None:
    """The button must move with the fix, not with the offline anchor.

    A row frozen at 90 ₽/$ while the desk sits at 180 advertises twice
    the coins the webhook will credit — the exact drift ``rates`` exists
    to prevent, so pin it here rather than trusting the call chain.
    """

    class FakeFx:
        async def usd_to_rub(self) -> float:
            return 180.0

    callback: Any = FakeCallback()
    await handle_topup_method(
        callback,
        TopupMethod(method="rollypay"),
        "ru",
        _crypto_service(None),
        _rollypay_service("rp_key"),
        _settings(),
        FakeFx(),  # type: ignore[arg-type]
    )
    keyboard = callback.message.edits[0][1]
    # 100 ₽ at 180 ₽/$ and 900 coins/$ → 500 coins, half the anchor quote.
    assert "500" in keyboard.inline_keyboard[0][0].text


@pytest.mark.asyncio
async def test_rollypay_amount_renders_pay_button() -> None:
    callback: Any = FakeCallback()
    service = FakeRollyPay(_created())
    await handle_rollypay_amount(
        callback,
        TopupRollyPayAmount(amount=0),
        "ru",
        service,  # type: ignore[arg-type]
    )
    assert service.calls == [
        {
            "user_id": 42,
            "amount_rub": RUB_AMOUNTS[0],
            # Localized, and carried to the provider's page (and from
            # there the payer's statement) rather than left to the
            # service's Russian default.
            "description": "h_topup_rollypay_description",
        }
    ]
    text, keyboard = callback.message.edits[0]
    assert text == "h_topup_rollypay_created"
    assert keyboard.inline_keyboard[0][0].url == "https://pay.rollypay.test/p/abc"


@pytest.mark.asyncio
@pytest.mark.parametrize("idx", [-1, len(RUB_AMOUNTS), 99])
async def test_rollypay_amount_out_of_range_index_refused(idx: int) -> None:
    callback: Any = FakeCallback()
    service = FakeRollyPay(_created())
    await handle_rollypay_amount(
        callback,
        TopupRollyPayAmount(amount=idx),
        "ru",
        service,  # type: ignore[arg-type]
    )
    assert service.calls == []  # no payment minted from a tampered index
    assert callback.answers == [("h_topup_bad_request", True)]


@pytest.mark.asyncio
async def test_rollypay_amount_renders_degraded_copy_on_stale_card() -> None:
    # The key can be pulled between rendering the amount card and the
    # click — that path must say why, not fail generically.
    callback: Any = FakeCallback()
    service = FakeRollyPay(RollyPayTopup(outcome=TopupInvoiceOutcome.NOT_CONFIGURED))
    await handle_rollypay_amount(
        callback,
        TopupRollyPayAmount(amount=1),
        "ru",
        service,  # type: ignore[arg-type]
    )
    assert callback.message.edits[0][0] == "h_topup_rollypay_not_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        RollyPayTopup(outcome=TopupInvoiceOutcome.FAILED),
        # CREATED without a pay_url is a provider contract violation —
        # must not render a card with a dead button.
        RollyPayTopup(outcome=TopupInvoiceOutcome.CREATED, pay_url=None),
    ],
)
async def test_rollypay_amount_alerts_on_failure(result: RollyPayTopup) -> None:
    callback: Any = FakeCallback()
    await handle_rollypay_amount(
        callback,
        TopupRollyPayAmount(amount=0),
        "ru",
        FakeRollyPay(result),  # type: ignore[arg-type]
    )
    assert callback.message.edits == []
    assert callback.answers == [("h_topup_invoice_failed", True)]


@pytest.mark.asyncio
async def test_menu_row_availability_follows_the_service() -> None:
    for ok in (True, False):
        text, keyboard = await topup_mod._menu_payload(
            "ru",
            balance=0,
            crypto_topup=_crypto_service(None),
            rollypay_topup=_rollypay_service("rp_key" if ok else None),
            settings=_settings(),
        )
        row = next(
            r
            for r in keyboard.inline_keyboard
            if r[0].callback_data == TopupMethod(method="rollypay").pack()
        )
        # Degraded rows stay clickable — the click explains why; the
        # unavailable suffix is the only difference. (``t`` is stubbed
        # to echo keys, so the suffix shows up as its key here.)
        assert ("h_topup_unavailable_suffix" in row[0].text) is not ok


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["yookassa", "stripe"])
async def test_business_only_methods_are_absent_until_configured(method: str) -> None:
    """The other half of the doctrine: not offered ≠ temporarily down.

    YooKassa and Stripe onboard registered businesses only, so a
    deployment without their credentials is not "down" — it does not
    offer them. A permanent 🚫 row would train users to read half the
    menu as decoration, so the row is absent entirely and returns the
    moment credentials appear.
    """
    for configured in (False, True):
        _, keyboard = await topup_mod._menu_payload(
            "ru",
            balance=0,
            crypto_topup=_crypto_service(None),
            rollypay_topup=_rollypay_service(),
            settings=_settings(**{method: configured}),
        )
        packed = TopupMethod(method=method).pack()
        present = any(r[0].callback_data == packed for r in keyboard.inline_keyboard)
        assert present is configured
        # When it IS shown it is a live row, never a degraded one.
        if present:
            row = next(r for r in keyboard.inline_keyboard if r[0].callback_data == packed)
            assert "h_topup_unavailable_suffix" not in row[0].text


@pytest.mark.asyncio
async def test_crypto_row_hides_when_rollypay_already_takes_crypto() -> None:
    """#145: no permanent 🚫 next to a crypto method that works.

    RollyPay's hosted page accepts crypto alongside card and SBP, so on
    a deployment with RollyPay live and no Crypto Pay token the sentence
    "crypto is unavailable" is simply false — and a false 🚫 costs more
    than a missing row, because it talks a user out of a payment they
    could have made from the button directly above.
    """
    _, keyboard = await topup_mod._menu_payload(
        "ru",
        balance=0,
        crypto_topup=_crypto_service(None),
        rollypay_topup=_rollypay_service("rp_key"),
        settings=_settings(),
    )
    packed = TopupMethod(method="crypto").pack()
    assert not any(r[0].callback_data == packed for r in keyboard.inline_keyboard)


@pytest.mark.asyncio
async def test_crypto_row_still_degrades_when_no_provider_takes_crypto() -> None:
    """The other half: with RollyPay down too, "crypto is down" is true.

    Then the degraded row is the honest one — it is the only thing that
    tells a user who paid in crypto last week that the method exists at
    all and is coming back.
    """
    _, keyboard = await topup_mod._menu_payload(
        "ru",
        balance=0,
        crypto_topup=_crypto_service(None),
        rollypay_topup=_rollypay_service(None),
        settings=_settings(),
    )
    packed = TopupMethod(method="crypto").pack()
    row = next(r for r in keyboard.inline_keyboard if r[0].callback_data == packed)
    assert "h_topup_unavailable_suffix" in row[0].text


@pytest.mark.asyncio
async def test_configured_crypto_pay_keeps_its_own_row() -> None:
    """A configured Crypto Pay is a second road, not a duplicate.

    Paying in @CryptoBot directly is a different flow from paying on
    RollyPay's page, so once the token exists the row comes back live
    regardless of what RollyPay is doing.
    """
    _, keyboard = await topup_mod._menu_payload(
        "ru",
        balance=0,
        crypto_topup=_crypto_service("cp_token"),
        rollypay_topup=_rollypay_service("rp_key"),
        settings=_settings(),
    )
    packed = TopupMethod(method="crypto").pack()
    row = next(r for r in keyboard.inline_keyboard if r[0].callback_data == packed)
    assert "h_topup_unavailable_suffix" not in row[0].text


@pytest.mark.asyncio
async def test_the_row_that_completes_a_payment_comes_first() -> None:
    """Order the menu by what the button *does*.

    Stars stay on top (no provider, always live); RollyPay follows,
    because it is the only row here that opens a payment page instead of
    explaining something.
    """
    _, keyboard = await topup_mod._menu_payload(
        "ru",
        balance=0,
        crypto_topup=_crypto_service("cp_token"),
        rollypay_topup=_rollypay_service("rp_key"),
        settings=_settings(),
    )
    order = [r[0].callback_data for r in keyboard.inline_keyboard]
    assert order[0] == TopupMethod(method="stars").pack()
    assert order[1] == TopupMethod(method="rollypay").pack()
