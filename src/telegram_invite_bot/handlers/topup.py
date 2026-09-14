"""``/topup`` — self-service coin top-up menu + Telegram Stars payment
(Cluster A1: L-84 menu, L-80 Stars, L-81 Crypto Pay invoice user-side).

Legacy ran this under ``/buy`` (``cmd_buy``, bot.py:18106) but the new
pipeline's ``/buy`` is the SHOP purchase command, so the coin top-up
surface lives under ``/topup`` with the ``/buy_coins`` alias (plus RU
``/пополнить``). Private-only: balances and payment links are
per-user and have no business in a group — legacy showed the same menu
anywhere, which leaked balance figures into group chats.

Flow map against legacy:

* menu (``cmd_buy`` bot.py:18106-18127 / ``cb_buy_menu`` :18154) →
  :func:`handle_topup_menu`. One button per method: ⭐ Stars,
  💎 Crypto Pay, 🏦 YooKassa, 💳 Stripe. Methods whose secret is
  missing render a 🚫-prefixed row (degraded doctrine — never hidden,
  never crashing; mirrors ``crypto_payment_not_configured``
  bot.py:18279-18285 and the webhook 503 posture).
* ⭐ Stars pack list (``_stars_amount_keyboard`` bot.py:18065-18074,
  packs ``PAYMENT_STARS_PACKS`` bot.py:3181) → :data:`STARS_PACKS`;
  invoice send (``cb_stars_amount`` bot.py:18200-18228) →
  :func:`handle_stars_pack` with ``currency="XTR"`` and an EMPTY
  ``provider_token`` per the Stars contract.
* ``pre_checkout_query`` (bot.py:18231-18235 — always ok=True) →
  :func:`handle_pre_checkout`, which validates the payload against the
  server-side pack table first (a tampered payload is refused instead
  of rubber-stamped).
* ``successful_payment`` (bot.py:18238-18273) →
  :func:`handle_successful_payment`, crediting through the SAME
  :class:`PaymentsService` pipeline as the provider webhooks
  (idempotency on ``telegram_payment_charge_id``, referral + developer
  commissions in the same transaction). Refunds: not required v1.
* 💎 Crypto Pay (``cb_pay_crypto``/``cb_crypto_currency``/
  ``cb_crypto_create_invoice`` bot.py:18276-18372) →
  :func:`handle_crypto_asset` / :func:`handle_crypto_invoice` over
  :class:`CryptoTopupService`; the paid invoice credits via the
  already-ported ``/crypto-webhook``.
* 🏦 YooKassa / 💳 Stripe: v1 renders honest "checkout in the bot is
  not available yet" copy — their WEBHOOK credit side already works if
  an operator creates the payment externally (webhook/payments.py).
* 💳 RollyPay (no legacy counterpart — legacy had no working rouble
  checkout at all) → :func:`handle_rollypay_amount` over
  :class:`RollyPayTopupService`. The first fiat method with a complete
  in-bot round trip: the bot mints the payment, RollyPay's own page
  takes the card / SBP / crypto, and ``/rollypay-webhook`` credits.
  Its rows are quoted at the live USD/RUB the crediting side will use,
  because a rouble button priced off a frozen anchor promises coins the
  webhook then declines to deliver.

Money posture: no handler here writes a wallet directly — the only
credit path is ``PaymentsService.handle_event`` (whose internals carry
the checked-credit contract), so the AST money-guard has nothing to
flag in this module. Callback payloads carry pack/amount *indexes*,
never amounts (see keyboards/builders/topup.py).
"""

from __future__ import annotations

import html
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import (
    TopupBack,
    TopupCryptoAsset,
    TopupCryptoInvoice,
    TopupMethod,
    TopupRollyPayAmount,
    TopupStarsPack,
)
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.language import LanguageMiddleware
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.processed_webhooks_repo import (
    ProcessedWebhooksRepo,
)
from telegram_invite_bot.repositories.referrals_repo import ReferralsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.payments import ParsedEvent, Provider
from telegram_invite_bot.services.payments.crypto_invoices import (
    COINS_PER_USD,
    CRYPTO_USD_AMOUNTS,
    TOPUP_ASSETS,
    CryptoTopupService,
    TopupInvoiceOutcome,
    build_crypto_topup_service,
)
from telegram_invite_bot.services.payments.fx import resolve_usd_to_rub
from telegram_invite_bot.services.payments.rollypay_invoices import (
    RUB_AMOUNTS,
    RollyPayTopupService,
    build_rollypay_topup_service,
    quote_coins,
)
from telegram_invite_bot.services.payments_service import (
    CreditOutcome,
    PaymentsService,
)
from telegram_invite_bot.services.referral_commission_service import (
    CommissionOutcome,
    ReferralCommissionService,
    render_referral_commission_notice,
)
from telegram_invite_bot.utils.aiogram import reply_or_send, require_from_user
from telegram_invite_bot.utils.numbers import format_number
from telegram_invite_bot.webhook.metrics import PAYMENT_CREDIT_FAILURES
from telegram_invite_bot.webhook.payments import alert_stars_refund

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.services.currency_service import CurrencyService

log = logger.bind(component="handlers.topup")

# ⭐ Stars packs: (stars_price, coins_credited). Mirrors legacy
# ``PAYMENT_STARS_PACKS`` (bot.py:3181) verbatim — the 10%-bonus ratio
# (1 star → 11 coins) is a product decision frozen there.
STARS_PACKS: Final[tuple[tuple[int, int], ...]] = (
    (50, 550),
    (100, 1100),
    (250, 2750),
    (500, 5500),
)

# Stars invoice payload prefix. Same shape as legacy
# (``stars_{user_id}_{stars}_{coins}``, bot.py:18211) so a payment
# initiated pre-cutover and completed post-cutover still parses.
_STARS_PAYLOAD_PREFIX: Final[str] = "stars_"

# Ledger ``reason`` for Stars credits — byte-identical to legacy
# ``add_coins(..., reason="Покупка за Telegram Stars")`` (bot.py:18260)
# so reason-filtered audit views span the cutover.
_STARS_REASON: Final[str] = "Покупка за Telegram Stars"

# Coin emoji for rendered amounts; matches the pipeline-wide fallback
# (webhook/payments.py ``_COIN_EMOJI``).
_COIN_EMOJI: Final[str] = "🪙"

# One identifier in the owner's uncredited-Stars alert, clamped so a
# hostile-looking charge id cannot push the message past Telegram's
# 4096. Same number and same reason as ``webhook/payments.py``'s
# ``_FACT_MAX_CHARS``.
_ALERT_FACT_MAX_CHARS: Final[int] = 160

# Reason label for an exception that escaped the credit transaction.
# Deliberately the same string the webhook uses for the same event, so
# ``sum by (reason) (tib_payment_credit_failures_total)`` reads across
# providers instead of splitting one fault into two names.
_REASON_PIPELINE_CRASH: Final[str] = "pipeline_crash"

_METHOD_STARS: Final[str] = "stars"
_METHOD_CRYPTO: Final[str] = "crypto"
_METHOD_ROLLYPAY: Final[str] = "rollypay"
_METHOD_YOOKASSA: Final[str] = "yookassa"
_METHOD_STRIPE: Final[str] = "stripe"


def parse_stars_payload(payload: str) -> tuple[int, int, int] | None:
    """``stars_{uid}_{stars}_{coins}`` → ``(uid, stars, coins)`` or None.

    The ``(stars, coins)`` pair must match a server-side pack — we
    minted every payload ourselves, so anything else is tampering or a
    foreign bot's invoice and is refused (unlike legacy, which trusted
    the parsed numbers outright, bot.py:18248-18252).
    """
    if not payload.startswith(_STARS_PAYLOAD_PREFIX):
        return None
    parts = payload.split("_")
    if len(parts) != 4:
        return None
    try:
        uid, stars, coins = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
    if uid <= 0 or (stars, coins) not in STARS_PACKS:
        return None
    return uid, stars, coins


def _method_rows(
    lang: str, *, crypto_ok: bool, rollypay_ok: bool, yookassa_ok: bool, stripe_ok: bool
) -> InlineKeyboardMarkup:
    """One button per *offered* payment method; degraded ones get a 🚫 row.

    Stars need no provider secret, so that row is always live.

    RollyPay comes first because it is the row that actually completes a
    payment here: its hosted page takes card, SBP and crypto, and the
    signed callback credits the coins without the user leaving Telegram.
    It stays clickable even unconfigured — the click explains *why*
    (degraded doctrine: honest copy beats a dead pixel), because it is
    unambiguously part of the offering and a user who tapped yesterday
    deserves an explanation rather than a vanished button.

    Crypto Pay is a second road to the same destination, and that is
    what decides its row. Unconfigured, it is shown degraded only when
    RollyPay cannot take crypto either — then "crypto is down right
    now" is true and worth saying. When RollyPay *is* live the sentence
    becomes false: crypto works, through the row directly above. A
    permanent 🚫 next to a working crypto method does not read as
    honesty, it reads as a menu half made of decoration, and it talks
    users out of a payment they could have made.

    YooKassa and Stripe are hidden rather than degraded when
    unconfigured, for the neighbouring reason. Both onboard only
    registered businesses, so on a deployment with no credentials for
    them the honest answer is not "temporarily unavailable" but "not
    offered here". Configure either and its row comes back on the next
    render; nothing has to be re-enabled by hand.
    """
    unavailable = " " + t("h_topup_unavailable_suffix", lang)
    rows = [
        [
            InlineKeyboardButton(
                text="⭐ " + t("h_topup_btn_stars", lang),
                callback_data=TopupMethod(method=_METHOD_STARS).pack(),
            )
        ],
        [
            InlineKeyboardButton(
                text=(
                    "💳 " + t("h_topup_btn_rollypay", lang) + ("" if rollypay_ok else unavailable)
                ),
                callback_data=TopupMethod(method=_METHOD_ROLLYPAY).pack(),
            )
        ],
    ]
    if crypto_ok or not rollypay_ok:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💎 " + t("h_topup_btn_crypto", lang) + ("" if crypto_ok else unavailable),
                    callback_data=TopupMethod(method=_METHOD_CRYPTO).pack(),
                )
            ]
        )
    if yookassa_ok:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🏦 YooKassa",
                    callback_data=TopupMethod(method=_METHOD_YOOKASSA).pack(),
                )
            ]
        )
    if stripe_ok:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💳 Stripe",
                    callback_data=TopupMethod(method=_METHOD_STRIPE).pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_row(lang: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text="🔙 " + t("h_topup_btn_back", lang),
            callback_data=TopupBack().pack(),
        )
    ]


def _stars_keyboard(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"⭐ {stars} Stars → {coins} {_COIN_EMOJI}",
                callback_data=TopupStarsPack(idx=i).pack(),
            )
        ]
        for i, (stars, coins) in enumerate(STARS_PACKS)
    ]
    rows.append(_back_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _crypto_assets_keyboard(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=asset, callback_data=TopupCryptoAsset(asset=asset).pack())]
        for asset in TOPUP_ASSETS
    ]
    rows.append(_back_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _crypto_amounts_keyboard(asset: str, lang: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"${usd} → {usd * COINS_PER_USD} {_COIN_EMOJI}",
                callback_data=TopupCryptoInvoice(asset=asset, amount=i).pack(),
            )
        ]
        for i, usd in enumerate(CRYPTO_USD_AMOUNTS)
    ]
    rows.append(_back_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _rollypay_amounts_keyboard(lang: str, usd_to_rub: float) -> InlineKeyboardMarkup:
    """Rouble amount rows, priced at the fix that will do the crediting.

    The coin figures are a quote, not a promise — the webhook recomputes
    them from the amount RollyPay reports as paid (R-FIX-003 posture).
    Quoting at the *live* fix rather than the offline anchor is what
    keeps the two numbers within rounding of each other: a button frozen
    at 90 ₽/$ while the fix sits at 100 would advertise 5 000 🪙 and
    deliver 4 500, and being 10 % short of the button is how a top-up
    becomes a support ticket.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{format_number(rub)} ₽ → "
                    f"{format_number(quote_coins(rub, usd_to_rub))} {_COIN_EMOJI}"
                ),
                callback_data=TopupRollyPayAmount(amount=i).pack(),
            )
        ]
        for i, rub in enumerate(RUB_AMOUNTS)
    ]
    rows.append(_back_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _menu_payload(
    lang: str,
    *,
    balance: int,
    crypto_topup: CryptoTopupService,
    rollypay_topup: RollyPayTopupService,
    settings: Settings,
) -> tuple[str, InlineKeyboardMarkup]:
    """Menu text + keyboard, with live per-method availability."""
    text = t(
        "h_topup_title",
        lang,
        balance=format_number(balance),
        rate=COINS_PER_USD,
        sign=_COIN_EMOJI,
    )
    keyboard = _method_rows(
        lang,
        crypto_ok=await crypto_topup.available(),
        rollypay_ok=rollypay_topup.available(),
        yookassa_ok=settings.payments.yookassa_configured,
        stripe_ok=settings.payments.stripe_configured,
    )
    return text, keyboard


async def _edit_or_answer(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None
) -> None:
    """Edit the menu card in place; swallow the benign edit races.

    Legacy wrapped every ``edit_message_text`` in try/except with a
    ``send_message`` fallback (bot.py:18172-18179); here a failed edit
    (deleted card, identical content) degrades to answering the
    callback only — the user re-runs /topup, nothing is stuck.
    """
    msg = callback.message
    if msg is not None and not isinstance(msg, InaccessibleMessage):
        try:
            await msg.edit_text(text, reply_markup=keyboard)
        except (TelegramBadRequest, TelegramForbiddenError):
            log.bind(chat_id=msg.chat.id, message_id=msg.message_id).debug(
                "topup card edit swallowed"
            )
    await callback.answer()


async def handle_topup_menu(
    message: Message,
    lang: str,
    economy_repo: EconomyRepo,
    crypto_topup: CryptoTopupService,
    rollypay_topup: RollyPayTopupService,
    settings: Settings,
) -> None:
    """``/topup`` — render the method menu with the live balance."""
    tg_user = require_from_user(message)
    wallet = await economy_repo.get(tg_user.id)
    balance = wallet.balance if wallet is not None else 0
    text, keyboard = await _menu_payload(
        lang,
        balance=balance,
        crypto_topup=crypto_topup,
        rollypay_topup=rollypay_topup,
        settings=settings,
    )
    await message.reply(text, reply_markup=keyboard)
    log.bind(uid=tg_user.id).info("/topup menu rendered")


async def handle_topup_back(
    callback: CallbackQuery,
    lang: str,
    economy_repo: EconomyRepo,
    crypto_topup: CryptoTopupService,
    rollypay_topup: RollyPayTopupService,
    settings: Settings,
) -> None:
    """🔙 — back to the method menu (legacy ``cb_buy_menu`` bot.py:18154)."""
    wallet = await economy_repo.get(callback.from_user.id)
    balance = wallet.balance if wallet is not None else 0
    text, keyboard = await _menu_payload(
        lang,
        balance=balance,
        crypto_topup=crypto_topup,
        rollypay_topup=rollypay_topup,
        settings=settings,
    )
    await _edit_or_answer(callback, text, keyboard)


async def handle_topup_method(
    callback: CallbackQuery,
    callback_data: TopupMethod,
    lang: str,
    crypto_topup: CryptoTopupService,
    rollypay_topup: RollyPayTopupService,
    settings: Settings,
    currency_service: CurrencyService | None = None,
) -> None:
    """Route a method click to its sub-screen or its degraded copy."""
    method = callback_data.method
    if method == _METHOD_STARS:
        await _edit_or_answer(callback, t("h_topup_stars_title", lang), _stars_keyboard(lang))
        return
    if method == _METHOD_CRYPTO:
        if not await crypto_topup.available():
            # Degraded doctrine (legacy bot.py:18279-18285): localized
            # copy + a back row, never a crash.
            await _edit_or_answer(
                callback,
                t("h_topup_crypto_not_configured", lang),
                InlineKeyboardMarkup(inline_keyboard=[_back_row(lang)]),
            )
            return
        await _edit_or_answer(
            callback, t("h_topup_crypto_title", lang), _crypto_assets_keyboard(lang)
        )
        return
    if method == _METHOD_ROLLYPAY:
        if not rollypay_topup.available():
            await _edit_or_answer(
                callback,
                t("h_topup_rollypay_not_configured", lang),
                InlineKeyboardMarkup(inline_keyboard=[_back_row(lang)]),
            )
            return
        await _edit_or_answer(
            callback,
            t("h_topup_rollypay_title", lang),
            _rollypay_amounts_keyboard(lang, await resolve_usd_to_rub(currency_service)),
        )
        return
    if method in (_METHOD_YOOKASSA, _METHOD_STRIPE):
        configured = (
            settings.payments.yookassa_configured
            if method == _METHOD_YOOKASSA
            else settings.payments.stripe_configured
        )
        name = "YooKassa" if method == _METHOD_YOOKASSA else "Stripe"
        # v1: no in-bot checkout for either — honest copy. When the
        # provider is configured the webhook credit side IS live, so
        # the copy says "operator-issued payments credit automatically";
        # unconfigured gets the plain "unavailable" line.
        key = "h_topup_method_external" if configured else "h_topup_method_unavailable"
        await _edit_or_answer(
            callback,
            t(key, lang, method=name),
            InlineKeyboardMarkup(inline_keyboard=[_back_row(lang)]),
        )
        return
    # Unknown method string (stale button / tampered payload).
    await callback.answer(t("h_topup_bad_request", lang), show_alert=True)


async def handle_stars_pack(
    callback: CallbackQuery,
    callback_data: TopupStarsPack,
    bot: Bot,
    lang: str,
) -> None:
    """⭐ pack click → ``send_invoice`` with ``currency="XTR"``.

    Stars contract (aiogram 3 / Bot API): ``provider_token`` is the
    EMPTY string, ``currency="XTR"``, exactly one :class:`LabeledPrice`
    whose ``amount`` is the star count (no decimal scaling). Legacy
    parity: bot.py:18213-18223.
    """
    if not 0 <= callback_data.idx < len(STARS_PACKS):
        await callback.answer(t("h_topup_bad_request", lang), show_alert=True)
        return
    stars, coins = STARS_PACKS[callback_data.idx]
    user_id = callback.from_user.id
    msg = callback.message
    chat_id = msg.chat.id if msg is not None else user_id
    payload = f"{_STARS_PAYLOAD_PREFIX}{user_id}_{stars}_{coins}"
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title=t("h_topup_stars_invoice_title", lang),
            description=t(
                "h_topup_stars_invoice_desc",
                lang,
                coins=format_number(coins),
                sign=_COIN_EMOJI,
            ),
            payload=payload,
            provider_token="",  # empty for Telegram Stars
            currency="XTR",
            prices=[LabeledPrice(label=f"{coins} {_COIN_EMOJI}", amount=stars)],
            start_parameter="buy_stars",
        )
    except Exception as exc:  # noqa: BLE001 — Telegram-side failure must not crash
        log.bind(uid=user_id, stars=stars).warning("stars send_invoice failed: {e}", e=exc)
        await callback.answer(t("h_topup_invoice_failed", lang), show_alert=True)
        return
    await callback.answer()
    log.bind(uid=user_id, stars=stars, coins=coins).info("stars invoice sent")


async def handle_pre_checkout(
    pre_checkout: PreCheckoutQuery,
    lang: str,
) -> None:
    """Approve a Stars pre-checkout after validating OUR payload.

    Legacy answered ``ok=True`` unconditionally (bot.py:18231-18235);
    we validate the payload against the server-side pack table first —
    an invoice we did not mint (or a tampered pack) is refused, which
    refunds nothing because nothing was charged yet.
    """
    if parse_stars_payload(pre_checkout.invoice_payload) is None:
        await pre_checkout.answer(ok=False, error_message=t("h_topup_precheckout_bad", lang))
        log.bind(uid=pre_checkout.from_user.id).warning("pre_checkout refused: unknown payload")
        return
    await pre_checkout.answer(ok=True)


async def credit_stars_payment(
    *,
    registry: EngineRegistry,
    settings: Settings,
    user_id: int,
    coins: int,
    charge_id: str,
    stars_charged: int | None = None,
    stars_currency: str | None = None,
) -> tuple[CreditOutcome, tuple[int, int] | None]:
    """Credit one Stars payment through the shared payments pipeline.

    Mirrors ``webhook/payments.py:_credit_event``: one ``economy``
    transaction wraps wallet credit + idempotency row + referral and
    developer commissions (legacy ``apply_purchase_commissions`` runs
    on the Stars path too, bot.py:18261). Returns the outcome plus
    ``(referrer_id, commission)`` when a kickback landed, so the caller
    can send the inviter's courtesy DM post-commit.

    ``stars_charged`` / ``stars_currency`` are the #239 audit trail
    and are deliberately taken from ``successful_payment`` rather
    than from ``coins``: the two are meant to agree, and the whole
    point of writing the charge down is to be able to notice when
    they don't. They are optional because the credit does not need
    them — a caller that has only a coin count still gets a correct
    top-up, with an empty audit cell.
    """
    event = ParsedEvent(
        provider=Provider.STARS,
        external_id=charge_id,
        user_id=user_id,
        coins=coins,
        reason=_STARS_REASON,
        fiat_amount=None if stars_charged is None else Decimal(stars_charged),
        # XTR is not fiat, and Telegram states it on every
        # ``successful_payment``; we record what was sent rather than
        # assuming the constant, so a future non-Stars invoice
        # reaching this path is visible instead of mislabelled.
        fiat_currency=None if stars_charged is None else (stars_currency or "XTR"),
        fx_rate=None,
    )
    async with registry.session(DBName.ECONOMY)() as session, session.begin():
        economy_repo = EconomyRepo(session)
        transactions_repo = TransactionsRepo(session)
        service = PaymentsService(
            economy=EconomyService(economy_repo, transactions_repo),
            idempotency=ProcessedWebhooksRepo(session),
            bot=None,  # the handler replies in-chat itself, post-commit
            referral_commission=ReferralCommissionService(
                economy_repo,
                transactions_repo,
                ReferralsRepo(session),
                percent=settings.economy.referral_commission_percent,
                developer_percent=settings.economy.developer_commission_percent,
                developer_id=settings.bot.admin_chat_id,
            ),
            # R15: the session the commission's SAVEPOINT is taken on —
            # the same one this transaction is open against.
            session=session,
        )
        outcome = await service.handle_event(event)
    kickback: tuple[int, int] | None = None
    commissions = service.last_commissions
    if (
        outcome is CreditOutcome.CREDITED
        and commissions is not None
        and commissions.referral.outcome is CommissionOutcome.CREDITED
        and commissions.referral.referrer_id is not None
    ):
        kickback = (commissions.referral.referrer_id, commissions.referral.commission)
    return outcome, kickback


async def _notify_referrer(
    *,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    referrer_id: int,
    commission: int,
) -> None:
    """Best-effort inviter kickback DM (legacy bot.py:9870-9876)."""
    lang = "ru"
    try:
        async with registry.session(DBName.USERS)() as session:
            lang = await UserSettingsRepo(session).get_language(referrer_id) or "ru"
    except Exception as exc:  # noqa: BLE001 — courtesy DM, log-only
        log.bind(uid=referrer_id).warning("referrer lang lookup failed: {e}", e=exc)
    try:
        await bot.send_message(
            referrer_id,
            render_referral_commission_notice(
                lang,
                commission=commission,
                percent=settings.economy.referral_commission_percent,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — courtesy DM, log-only
        log.bind(uid=referrer_id).warning("referrer kickback DM failed: {e}", e=exc)


async def _alert_stars_uncredited(
    *,
    bot: Bot,
    settings: Settings,
    user_id: int,
    stars: int,
    coins: int,
    charge_id: str,
    reason: str,
) -> None:
    """Count a charged-but-uncredited Stars payment and tell the owner.

    Stars is the leg with no safety net underneath it:
    ``successful_payment`` arrives exactly once, nothing re-delivers it,
    and there is no external ledger to walk — if this moment passes
    unnoticed the only remaining record that someone paid is the user's
    memory of paying. The webhook legs at least leave a row in a
    provider dashboard that a reconciliation can find later.

    That is the whole of the difference. This docstring used to claim
    the webhook legs also got a non-2xx and an ``ERROR`` line on this
    outcome and could therefore stay quiet; both halves were false
    (``webhook/payments.py`` answers 200 here and logged nothing), and
    the claim was reading as a design decision rather than the gap it
    was. #296 gave that path its own alert — see
    ``webhook/payments.py::_alert_credit_refused``.

    The counter is kept for parity (same metric, same ``reason``
    vocabulary, so one query spans every provider) and a DM sits on top.
    What the DM has to carry is the charge id: that is the value the
    owner needs to confirm the charge in Telegram's own payment record
    before crediting by hand.

    Best-effort after the counter, in that order and for the usual
    reason — the counter is the durable half of the signal, the DM the
    convenient one, and a Telegram outage must not swallow both.
    """
    PAYMENT_CREDIT_FAILURES.labels(provider=Provider.STARS.value, reason=reason).inc()
    log.bind(
        provider=Provider.STARS.value,
        uid=user_id,
        stars=stars,
        coins=coins,
        charge_id=charge_id,
        reason=reason,
    ).error("stars_paid_but_uncredited")
    admin_id = settings.bot.admin_chat_id
    if not admin_id:
        return
    try:
        # ``charge_id`` is Telegram-controlled text going into an
        # HTML-parse-mode message: one "<" in it would make Telegram
        # reject the whole alert, which is the one message here that
        # cannot be allowed to go missing.
        await bot.send_message(
            admin_id,
            "🚨 <b>Telegram Stars: оплата прошла, монеты НЕ зачислены</b>\n\n"
            f"Пользователь: <code>{user_id}</code>\n"
            f"Списано: <b>{stars}</b> ⭐ → <b>{coins}</b> {_COIN_EMOJI}\n"
            f"Charge ID: <code>{html.escape(_clamp_fact(charge_id))}</code>\n"
            f"Причина: <code>{html.escape(_clamp_fact(reason))}</code>\n\n"
            "Звёзды у вас, монет у человека нет. Повторной доставки не будет — "
            "Telegram присылает successful_payment один раз. Начислите вручную "
            "или верните звёзды.",
        )
    except Exception as exc:  # noqa: BLE001 — courtesy alert, log-only
        log.warning("stars uncredited alert DM failed: {e}", e=exc)


def _clamp_fact(value: str) -> str:
    """One identifier, short enough that the alert cannot break 4096."""
    if len(value) <= _ALERT_FACT_MAX_CHARS:
        return value
    return value[: _ALERT_FACT_MAX_CHARS - 1] + "…"


async def handle_successful_payment(
    message: Message,
    bot: Bot,
    lang: str,
    registry: EngineRegistry,
    settings: Settings,
) -> None:
    """``successful_payment`` (Stars) — credit + receipt.

    Idempotent on ``telegram_payment_charge_id`` through the shared
    ``processed_webhooks`` table: a redelivered update replies the
    legacy "already credited" line (bot.py:18255-18258) and never
    double-credits. Payloads that are not ours (other invoice flows a
    future feature might add) are ignored silently, as legacy did
    (bot.py:18246-18247).

    #297: the coins go to whoever Telegram says paid, NOT to the user id
    carried in the payload. An invoice message can be forwarded, and the
    person who receives it can pay it — so the payload's id is "who
    asked for this invoice", which stops being the same person the
    moment the message leaves its original chat. Crediting the payload
    made a forwarded invoice a working way to have a stranger top up
    someone else's wallet.

    ``stars``/``coins`` from the payload stay trusted, and for a reason
    that does not extend to the id: ``parse_stars_payload`` checks that
    pair against the server-side :data:`STARS_PACKS` table, so a tampered
    payload is refused before it gets here. Nothing validates the id
    against anything, because there is nothing to validate it against.
    """
    sp = message.successful_payment
    if sp is None:
        return
    parsed = parse_stars_payload(sp.invoice_payload or "")
    if parsed is None:
        log.bind(payload=sp.invoice_payload).info(
            "successful_payment with foreign payload — ignored"
        )
        return
    payload_uid, stars, coins = parsed
    payer = message.from_user
    user_id = payer.id if payer is not None else payload_uid
    if user_id != payload_uid:
        # Not refused: the payer's stars are already gone, and telling
        # them "this invoice was not yours" would leave them charged and
        # empty-handed for someone else's benefit. Crediting the payer
        # puts the money and the coins in the same pair of hands, which
        # is the outcome both of them would have chosen. Logged because
        # it should be rare enough that a run of them means something.
        log.bind(
            payer=user_id,
            payload_uid=payload_uid,
            charge=sp.telegram_payment_charge_id,
        ).warning("stars invoice paid by someone other than its addressee — crediting the payer")
    charge_id = sp.telegram_payment_charge_id
    try:
        outcome, kickback = await credit_stars_payment(
            registry=registry,
            settings=settings,
            user_id=user_id,
            coins=coins,
            charge_id=charge_id,
            # #239: the server-authoritative pair. ``stars`` from the
            # payload is set at checkout and never re-signed, which is
            # exactly why it is not what gets written down.
            stars_charged=sp.total_amount,
            stars_currency=sp.currency,
        )
    except Exception:
        # The credit transaction rolled back, so no coins and no
        # idempotency row: the charge stands alone. Re-raising would
        # hand this to ``handlers.errors``, which paints the generic
        # "⚠️ Произошла ошибка" — indistinguishable, to the payer, from
        # the top-up not having been taken at all. Instead: log with the
        # traceback, count it under the same ``pipeline_crash`` label
        # the webhook uses, alert the owner with the charge id, and tell
        # the payer the specific thing that happened.
        log.opt(exception=True).error("stars credit pipeline crashed")
        await _alert_stars_uncredited(
            bot=bot,
            settings=settings,
            user_id=user_id,
            stars=stars,
            coins=coins,
            charge_id=charge_id,
            reason=_REASON_PIPELINE_CRASH,
        )
        await reply_or_send(message, t("h_topup_credit_failed", lang))
        return
    # Every reply below goes through ``reply_or_send``: the credit is
    # already committed in its own transaction (Telegram has charged
    # the stars — it must stand whether or not we can talk to the
    # user), so a raise here would only reach ``handlers.errors`` and
    # paint "⚠️ Произошла ошибка" over a top-up that actually
    # succeeded. A buyer told that is a buyer who pays twice. The
    # helper also survives the reply target vanishing, which a raw
    # ``reply`` does not.
    if outcome is CreditOutcome.CREDITED:
        if not await reply_or_send(
            message, t("balance_topup_ok", lang, coins_amt=coins, sign=_COIN_EMOJI)
        ):
            log.bind(uid=user_id, coins=coins).warning("stars receipt undeliverable")
        if kickback is not None:
            await _notify_referrer(
                bot=bot,
                registry=registry,
                settings=settings,
                referrer_id=kickback[0],
                commission=kickback[1],
            )
    elif outcome is CreditOutcome.IDEMPOTENT:
        await reply_or_send(message, t("payment_coins_already_added", lang))
    else:
        # CREDIT_REFUSED / INVALID_AMOUNT: the stars are charged and the
        # credit resolved to a terminal refusal. Telling the payer is
        # necessary but not sufficient — someone has to be told who owes
        # what, and it cannot be the payer's job to notice.
        await _alert_stars_uncredited(
            bot=bot,
            settings=settings,
            user_id=user_id,
            stars=stars,
            coins=coins,
            charge_id=charge_id,
            reason=outcome.value,
        )
        await reply_or_send(message, t("h_topup_credit_failed", lang))
    log.bind(uid=user_id, stars=stars, coins=coins, outcome=outcome.value).info(
        "stars payment processed"
    )


async def handle_refunded_payment(
    message: Message,
    registry: EngineRegistry,
    settings: Settings,
    bot: Bot,
) -> None:
    """``refunded_payment`` (Stars) — stamp the credit, alert the owner.

    Until #1987 nothing in the bot listened for this update at all. A
    Stars refund therefore left the coins on the payer's balance AND
    left ``processed_webhooks.reversed_at`` NULL — which is the column
    ``TransactionsRepo.lifetime_deposits`` subtracts, so refunded money
    kept counting as a deposit toward the withdrawal gate. Refund,
    withdraw, repeat: the money came back out of the owner's pocket
    twice.

    Telegram reuses the ``telegram_payment_charge_id`` of the original
    ``successful_payment`` here, and that is what
    ``credit_stars_payment`` stored as ``external_id``, so resolving
    the credit is a primary-key read (see ``_REVERSAL_ID_MATCHES_CREDIT``
    in :mod:`telegram_invite_bot.webhook.payments`).

    Deliberately silent toward the payer: they asked Telegram for the
    refund and Telegram already told them it went through. The coins
    are NOT debited automatically — same non-decision the webhook
    reversals make, and for the same reason: a forced debit can drive
    a balance negative or collide with money already spent, so it
    stays the owner's call.
    """
    refund = message.refunded_payment
    if refund is None:  # pragma: no cover - the filter guarantees it
        return
    await alert_stars_refund(bot=bot, settings=settings, engines=registry, refund=refund)
    log.bind(charge_id=refund.telegram_payment_charge_id, stars=refund.total_amount).warning(
        "stars payment refunded"
    )


async def handle_crypto_asset(
    callback: CallbackQuery,
    callback_data: TopupCryptoAsset,
    lang: str,
) -> None:
    """Asset chosen → USD amount keyboard (legacy bot.py:18327-18347)."""
    if callback_data.asset not in TOPUP_ASSETS:
        await callback.answer(t("h_topup_bad_request", lang), show_alert=True)
        return
    await _edit_or_answer(
        callback,
        t("h_topup_crypto_amount_title", lang, asset=callback_data.asset),
        _crypto_amounts_keyboard(callback_data.asset, lang),
    )


async def handle_crypto_invoice(
    callback: CallbackQuery,
    callback_data: TopupCryptoInvoice,
    lang: str,
    crypto_topup: CryptoTopupService,
) -> None:
    """Create the Crypto Pay invoice and render the pay-URL button.

    Legacy ``cb_crypto_create_invoice`` (bot.py:18350-18372). The paid
    invoice flows back through ``/crypto-webhook`` → the same
    PaymentsService pipeline; this handler only mints the link.
    """
    if callback_data.asset not in TOPUP_ASSETS or not 0 <= callback_data.amount < len(
        CRYPTO_USD_AMOUNTS
    ):
        await callback.answer(t("h_topup_bad_request", lang), show_alert=True)
        return
    usd = CRYPTO_USD_AMOUNTS[callback_data.amount]
    coins = usd * COINS_PER_USD
    user_id = callback.from_user.id
    invoice = await crypto_topup.create_invoice(
        user_id=user_id,
        asset=callback_data.asset,
        amount_usd=usd,
        coins=coins,
    )
    if invoice.outcome is TopupInvoiceOutcome.NOT_CONFIGURED:
        await _edit_or_answer(
            callback,
            t("h_topup_crypto_not_configured", lang),
            InlineKeyboardMarkup(inline_keyboard=[_back_row(lang)]),
        )
        return
    if invoice.outcome is not TopupInvoiceOutcome.CREATED or invoice.pay_url is None:
        await callback.answer(t("h_topup_invoice_failed", lang), show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("h_topup_btn_pay", lang), url=invoice.pay_url)],
            _back_row(lang),
        ]
    )
    await _edit_or_answer(
        callback,
        t(
            "h_topup_crypto_invoice",
            lang,
            asset=callback_data.asset,
            usd=usd,
            coins=format_number(coins),
            sign=_COIN_EMOJI,
        ),
        keyboard,
    )
    log.bind(uid=user_id, asset=callback_data.asset, usd=usd).info("crypto top-up invoice rendered")


async def handle_rollypay_amount(
    callback: CallbackQuery,
    callback_data: TopupRollyPayAmount,
    lang: str,
    rollypay_topup: RollyPayTopupService,
    currency_service: CurrencyService | None = None,
) -> None:
    """Rouble amount click → a RollyPay payment page + the pay button.

    The twin of :func:`handle_crypto_invoice`, and deliberately built to
    the same shape: this handler only *mints the link*. Nothing about
    the wallet happens here — the paid callback comes back through
    ``/rollypay-webhook``, which verifies the HMAC, recomputes the coins
    from the paid amount and credits through the shared
    :class:`PaymentsService` pipeline. So a user who closes this card,
    pays an hour later and never returns to Telegram still gets credited.

    ``NOT_CONFIGURED`` is reachable even though the menu gated on
    availability one screen back: nothing stops a user pressing a button
    on a card rendered before the key was pulled. It renders the same
    honest copy the menu would, rather than the generic failure.
    """
    if not 0 <= callback_data.amount < len(RUB_AMOUNTS):
        await callback.answer(t("h_topup_bad_request", lang), show_alert=True)
        return
    rub = RUB_AMOUNTS[callback_data.amount]
    user_id = callback.from_user.id
    payment = await rollypay_topup.create_payment(
        user_id=user_id,
        amount_rub=rub,
        # Localized, and deliberately the raw integer rather than
        # ``format_number``: this string leaves Telegram for the
        # provider's page and, from there, the payer's bank statement,
        # where a thin-space thousands separator is a mojibake risk for
        # no reader benefit.
        description=t("h_topup_rollypay_description", lang, rub=rub),
    )
    if payment.outcome is TopupInvoiceOutcome.NOT_CONFIGURED:
        await _edit_or_answer(
            callback,
            t("h_topup_rollypay_not_configured", lang),
            InlineKeyboardMarkup(inline_keyboard=[_back_row(lang)]),
        )
        return
    if payment.outcome is not TopupInvoiceOutcome.CREATED or payment.pay_url is None:
        await callback.answer(t("h_topup_invoice_failed", lang), show_alert=True)
        return
    coins = quote_coins(rub, await resolve_usd_to_rub(currency_service))
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("h_topup_btn_pay", lang), url=payment.pay_url)],
            _back_row(lang),
        ]
    )
    await _edit_or_answer(
        callback,
        t(
            "h_topup_rollypay_created",
            lang,
            rub=format_number(rub),
            coins=format_number(coins),
            sign=_COIN_EMOJI,
        ),
        keyboard,
    )
    log.bind(uid=user_id, rub=rub, payment_id=payment.payment_id).info(
        "rollypay top-up payment rendered"
    )


def build_router(
    registry: EngineRegistry,
    settings: Settings,
    *,
    crypto_topup: CryptoTopupService | None = None,
    rollypay_topup: RollyPayTopupService | None = None,
    currency_service: CurrencyService | None = None,
) -> Router:
    """Private-only top-up router (L-84/L-80/L-81 + RollyPay).

    ``crypto_topup`` is injectable for tests; production default wires
    the runtime-secret-aware resolver (T-027). EconomyMiddleware rides
    the message + callback chains for the balance line on the menu;
    the Stars credit path opens its own economy transaction instead
    (one transaction wrapping credit + idempotency + commissions, same
    shape as ``webhook/payments.py:_credit_event``).

    ``currency_service`` is the SAME instance ``/rate`` and the profile
    card read (see ``routers/main_router``) — sharing it means the
    rouble figures on this menu come out of the same warm hourly cache
    the rest of the bot quotes from, so /topup and /rate cannot show two
    different USD/RUB in the same minute. Optional because the RollyPay
    rows degrade to the offline anchor without it rather than refusing
    to render.
    """
    router = Router(name="topup")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)
    # #1608: the callback side is scoped too, not just the message
    # side. Every keyboard this router renders comes out of a
    # private-only message handler, and no legacy surface emits the
    # ``tpb``/``tpm``/``tps``/``tpca``/``tpci``/``tpr`` prefixes
    # (checked against bot.py), so a click arriving from a group
    # cannot be a real user flow. The per-handler guards would still
    # stop it; the point of the filter is that the scope must not
    # rest on them alone.
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    service = crypto_topup or build_crypto_topup_service(registry=registry, settings=settings)
    rollypay = rollypay_topup or build_rollypay_topup_service(settings=settings)

    router.message.middleware(EconomyMiddleware(registry))
    router.callback_query.middleware(EconomyMiddleware(registry))
    # ``lang`` for pre-checkout answers: the root LanguageMiddleware is
    # mounted on message/callback_query only, and pre_checkout_query
    # updates bypass it — mount the same middleware here so the refusal
    # copy is localized without touching tg_user.language_code.
    router.pre_checkout_query.middleware(LanguageMiddleware(registry))

    async def _menu(message: Message, lang: str, economy_repo: EconomyRepo) -> None:
        await handle_topup_menu(message, lang, economy_repo, service, rollypay, settings)

    async def _back(callback: CallbackQuery, lang: str, economy_repo: EconomyRepo) -> None:
        await handle_topup_back(callback, lang, economy_repo, service, rollypay, settings)

    async def _method(callback: CallbackQuery, callback_data: TopupMethod, lang: str) -> None:
        await handle_topup_method(
            callback, callback_data, lang, service, rollypay, settings, currency_service
        )

    async def _crypto_invoice(
        callback: CallbackQuery, callback_data: TopupCryptoInvoice, lang: str
    ) -> None:
        await handle_crypto_invoice(callback, callback_data, lang, service)

    async def _rollypay_amount(
        callback: CallbackQuery, callback_data: TopupRollyPayAmount, lang: str
    ) -> None:
        await handle_rollypay_amount(callback, callback_data, lang, rollypay, currency_service)

    async def _paid(message: Message, bot: Bot, lang: str) -> None:
        await handle_successful_payment(message, bot, lang, registry, settings)

    async def _refunded(message: Message, bot: Bot) -> None:
        await handle_refunded_payment(message, registry, settings, bot)

    router.message.register(
        _menu,
        Command("topup", "buy_coins", "пополнить", ignore_case=True),
        F.from_user,
    )
    router.message.register(_paid, F.successful_payment)
    router.message.register(_refunded, F.refunded_payment)
    router.pre_checkout_query.register(handle_pre_checkout)
    router.callback_query.register(_back, TopupBack.filter())
    router.callback_query.register(_method, TopupMethod.filter())
    router.callback_query.register(handle_stars_pack, TopupStarsPack.filter())
    router.callback_query.register(handle_crypto_asset, TopupCryptoAsset.filter())
    router.callback_query.register(_crypto_invoice, TopupCryptoInvoice.filter())
    router.callback_query.register(_rollypay_amount, TopupRollyPayAmount.filter())
    return with_chat_type_refusal(router, scope="private")
