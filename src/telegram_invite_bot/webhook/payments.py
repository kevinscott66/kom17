"""Provider payment webhooks (Crypto Pay, YooKassa, Stripe, RollyPay).

T-025 port. Routes ``POST /crypto-webhook``, ``POST /yookassa-webhook``,
``POST /stripe-webhook`` and ``POST /rollypay-webhook``. Each route:

1. Looks up its adapter via :class:`PaymentsConfig` — if the
   corresponding secret is missing, the route returns ``503`` with a
   single log line ("degraded mode, not configured") and no side
   effects. Legacy answered 403 on a missing secret only on the
   Crypto Pay route; its YooKassa and Stripe routes credited the caller
   *without any verification at all* when their secrets were unset
   (``webhook_server.py:166``, ``:193``). So the change here is not a
   status-code tweak — every route now answers 503 and credits nothing.
   503 rather than 403 because a missing secret is an operator problem,
   not a caller problem; see ADR 0014.
2. Calls ``adapter.verify_signature(headers, body)``. False → the
   provider's expected 4xx (403 for Crypto/YooKassa, 400 for Stripe
   per Stripe's own spec, which retries on 5xx but NOT on 4xx).
3. Calls ``adapter.parse_event(body)``. None → 200 OK ack (event was
   well-formed but not credit-relevant, e.g. ``invoice_expired``).
4. Opens an ``economy`` session via :class:`EngineRegistry`, wraps
   the credit + idempotency-row in one transaction, then commits.
5. After commit, fires a best-effort confirmation DM (legacy parity).

If step 4 raises, the answer depends on whether the provider asking
again could help: a transient DB fault gets a 503 so the redelivery
re-runs the credit, anything else gets a 200 so a poison event doesn't
loop. See :func:`_pipeline_failure_is_retryable`.

The router is built from the running :class:`Application` instance
(engines, bot, settings, payments config) through
``request.app.state.application``. Tests inject their own
application; the wiring is deliberately not module-global so a single
process can host multiple routers in parallel.

T-025 replaces the previous ``LegacyBotProxy`` bridge entirely. No
more lazy import of ``bot.py`` for payments.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from aiogram import Bot
from aiogram.types import RefundedPayment
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

from telegram_invite_bot.cms.client_ip import client_key
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.processed_webhooks_repo import (
    CreditRecord,
    ProcessedWebhooksRepo,
)
from telegram_invite_bot.repositories.referrals_repo import ReferralsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.payments import (
    CryptoAdapter,
    ParsedEvent,
    Provider,
    RollyPayAdapter,
    StripeAdapter,
    UncreditedCause,
    UncreditedPayment,
    YooKassaAdapter,
)
from telegram_invite_bot.services.payments.fx import resolve_usd_to_rub
from telegram_invite_bot.services.payments.rollypay import (
    REVERSAL_EVENTS as ROLLYPAY_REVERSAL_EVENTS,
)
from telegram_invite_bot.services.payments.secret_resolver import resolve_crypto_token
from telegram_invite_bot.services.payments.stripe import (
    REVERSAL_EVENTS as STRIPE_REVERSAL_EVENTS,
)
from telegram_invite_bot.services.payments.yookassa import (
    REVERSAL_EVENTS as YOOKASSA_REVERSAL_EVENTS,
)
from telegram_invite_bot.services.payments.yookassa import (
    ReverifyUnavailable,
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
from telegram_invite_bot.utils.http_body import (
    PAYMENT_WEBHOOK_MAX_BYTES,
    declared_body_too_large,
    read_body_capped,
)
from telegram_invite_bot.webhook.metrics import (
    PAYMENT_CREDIT_FAILURES,
    PAYMENT_DM_FAILURES,
    PAYMENT_REVERSALS,
    PAYMENT_UNCREDITED,
)
from telegram_invite_bot.webhook.reverify_throttle import ReverifyThrottle

if TYPE_CHECKING:
    from telegram_invite_bot.app import Application
    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.services.currency_service import CurrencyService


class ReversalContext(Protocol):
    """The three things the reversal path needs from the running bot.

    :func:`_alert_reversal` and :func:`_resolve_reversal_credit` used to
    take the whole :class:`~telegram_invite_bot.app.Application`, which
    tied them to the webhook routes: a message handler has a ``Bot``, a
    ``Settings`` and an ``EngineRegistry``, but it cannot build an
    ``Application`` (that needs a DI container and a ``Dispatcher``).
    Telegram Stars refunds arrive as an ordinary update rather than as a
    webhook POST, so #1987 needed exactly that — hence the narrowing.

    Read-only members on purpose: ``Application`` satisfies this with
    plain dataclass attributes, and so does the small frozen adapter a
    handler passes (:class:`_HandlerReversalContext`).
    """

    @property
    def settings(self) -> Settings: ...

    @property
    def bot(self) -> Bot: ...

    @property
    def engines(self) -> EngineRegistry: ...


log = logger.bind(component="payments")

# Coin emoji rendered into ``balance_topup_ok`` at the ``{sign}``
# placeholder. Matches legacy's ``COM_EMOJI`` fallback (🪙) so the
# DM text is byte-identical across the cutover. A future
# config-driven coin emoji would land in PaymentsConfig.
_COIN_EMOJI = "🪙"

#: Ceiling on the YooKassa merchant-API reverify (#228). The SDK
#: is synchronous ``requests`` with its own retry budget, so an
#: unreachable provider can hold a delivery for far longer than a
#: webhook has any business taking. Eight seconds is comfortably
#: above a healthy round trip.
#:
#: #1610: this used to end "and well below YooKassa's own
#: redelivery interval, so a timeout costs a retry, not a payment".
#: Both halves were wrong. YooKassa publishes no redelivery
#: interval for API v3 at all — only a 24-hour window — and, more
#: to the point, a timeout costs a retry only because the route
#: now answers a code the provider redelivers on. It used to
#: answer 200, which is the acknowledgement that ENDS delivery.
_YOOKASSA_REVERIFY_TIMEOUT_S: Final[float] = 8.0

#: Width of the pool that reverify runs in (#1439). A pool of its own,
#: not the loop's default executor: the route is unauthenticated —
#: YooKassa signs nothing, so ``verify_signature`` there is a
#: shop-credentials shape check and a flood costs an attacker nothing —
#: and the default executor is where every image the bot draws is
#: rendered (the profile card, ``/stats``, ``/chatstats``, the guide
#: site). Sharing it let one POST route decide whether those rendered.
#:
#: Four is a ceiling on the damage, not a throughput target: a reverify
#: is a single merchant-API round trip on a payment rate measured in
#: units per minute. Deliveries past it queue, and the timeout above
#: cancels them out of that queue — a queued call whose deadline
#: passed is never handed a thread at all, so the flood cannot buy
#: more than four.
_REVERIFY_WORKERS: Final[int] = 4

#: The limiter in front of that pool (:mod:`webhook.reverify_throttle`).
#: Module-global for the same reason the pool is: one process, one
#: bucket, however many Applications a test session builds. Cheap
#: enough to exist unconditionally — two floats and an empty map — so
#: unlike the pool it is not built lazily.
_REVERIFY_THROTTLE: ReverifyThrottle = ReverifyThrottle()

#: Built on first use, dropped by :func:`shutdown_reverify_pool`.
#: Module-global rather than an :class:`Application` attribute for the
#: same reason the /broadcast fan-out is one (``handlers/broadcast.py``,
#: :func:`~telegram_invite_bot.handlers.broadcast.cancel_inflight`): a
#: pool of OS threads is worth having exactly one of, and one test
#: process builds many applications.
_REVERIFY_POOL: ThreadPoolExecutor | None = None


def _reverify_pool() -> ThreadPoolExecutor:
    """The pool the blocking YooKassa reverify runs in.

    Lazy, so an operator with no YooKassa credentials never pays for
    threads that cannot be used. Unlocked, because both callers are the
    single event loop: the route that submits, and the teardown below.
    """
    global _REVERIFY_POOL  # noqa: PLW0603 — process-wide pool, see above
    if _REVERIFY_POOL is None:
        _REVERIFY_POOL = ThreadPoolExecutor(
            max_workers=_REVERIFY_WORKERS, thread_name_prefix="yookassa-reverify"
        )
    return _REVERIFY_POOL


def reset_reverify_throttle() -> None:
    """Forget the rate-limiter's state along with the application.

    Its buckets describe traffic aimed at one running deployment, and a
    process hosts exactly one of those — except in the test suite,
    where it hosts hundreds, and a bucket drained by one test would be
    a limit the next test never asked for. Called beside
    :func:`shutdown_reverify_pool` for the same reason and at the same
    moment; separate from it because dropping OS threads and forgetting
    a few floats are not the same operation.
    """
    global _REVERIFY_THROTTLE  # noqa: PLW0603 — process-wide limiter, see above
    _REVERIFY_THROTTLE = ReverifyThrottle()


def shutdown_reverify_pool() -> None:
    """Drop the pool, discarding whatever never got a thread.

    ``wait=False`` because a reverify already inside the SDK's blocking
    ``requests`` call cannot be interrupted, and shutdown must not hang
    on the provider whose slowness is the reason this pool exists.
    Those threads are joined at interpreter exit by
    :class:`ThreadPoolExecutor`'s own ``atexit`` hook — the same deal
    the default executor offers today, and the reason the timeout
    above, not this function, is what bounds a stuck delivery.

    The global is cleared rather than kept: a shut-down executor
    refuses new work forever, and one process hosts many applications
    in the test suite. The next submission builds a fresh pool.
    """
    global _REVERIFY_POOL  # noqa: PLW0603 — process-wide pool, see above
    pool, _REVERIFY_POOL = _REVERIFY_POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


#: Shape a Crypto Pay signature must have before the route will spend
#: anything on it (#1470). The expected value is a SHA-256 HMAC
#: rendered by ``hexdigest()`` (``services/payments/crypto.py``, see
#: :meth:`CryptoAdapter.verify_signature`), so it is always exactly 64
#: hex characters. ``compare_digest`` is exact, so nothing outside this
#: shape could ever match — refusing it early is the same verdict,
#: reached without touching the database. Both casings are accepted:
#: only the lowercase form can actually match today, and the extra
#: tolerance costs nothing while surviving a change on the other side.
_CRYPTO_SIGNATURE_SHAPE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-fA-F]{64}\Z")


async def _credit_event(application: Application, event: ParsedEvent) -> CreditOutcome:
    """Run the service inside a fresh economy-session transaction.

    The economy session and the users session are independent: the
    credit + idempotency row land in one transaction on
    ``economy.db``, then a separate short-lived users-session opens
    to read the user's language for the confirmation DM. Coupling
    the two would force a multi-DB transaction which SQLite does not
    support natively.

    The DM is best-effort and runs AFTER the economy commit — a DM
    failure (blocked user, network blip) must not roll back the
    wallet write. The sender is :func:`_send_topup_dm` below, which
    swallows everything it raises; the ``users.db`` session it runs
    inside does not, which is why the call site wraps it too (see the
    #1184 note there). ``PaymentsService.notify_user`` used to be the
    sender and is still where the same contract is documented, but it
    is unreachable from here: this route constructs the service with
    ``bot=None`` precisely so the DM happens out here, post-commit.
    """
    engines = application.engines
    bot = application.bot

    async with (
        engines.session(DBName.ECONOMY)() as economy_session,
        economy_session.begin(),
    ):
        economy_repo = EconomyRepo(economy_session)
        transactions_repo = TransactionsRepo(economy_session)
        economy_service = EconomyService(economy_repo, transactions_repo)
        payments_service = PaymentsService(
            economy=economy_service,
            idempotency=ProcessedWebhooksRepo(economy_session),
            bot=None,  # DM happens post-commit, in a separate users-session below
            # L-22/L-32 + #75: same-session referral kickback AND developer
            # commission — both cuts commit atomically with the buyer's
            # top-up credit (legacy apply_purchase_commissions runs both,
            # bot.py:9900-9903, on every top-up path).
            referral_commission=ReferralCommissionService(
                economy_repo,
                transactions_repo,
                ReferralsRepo(economy_session),
                percent=application.settings.economy.referral_commission_percent,
                # #75: the developer cut. The field is a plain
                # ``EconomyConfig.developer_commission_percent`` (default
                # 5 — the legacy default at bot.py:3173); recipient is
                # ADMIN_CHAT_ID (bot.py:9888).
                developer_percent=application.settings.economy.developer_commission_percent,
                developer_id=application.settings.bot.admin_chat_id,
            ),
            # R15: the session the commission's SAVEPOINT is taken on —
            # the same one this transaction is open against.
            session=economy_session,
        )
        outcome = await payments_service.handle_event(event)

    # A verified event that resolved to a terminal non-credit outcome
    # (the economy layer refused the write, or our amount validator
    # rejected it) is acked 200 but the money never landed — make that
    # observable. CREDITED and
    # IDEMPOTENT are the healthy outcomes and are NOT counted here.
    #
    # #296: the counter used to be the whole of it. A Prometheus series
    # is not a person, and this branch is the one where a real payment
    # has already been banked — so the owner is told as well, exactly
    # like the Stars leg does for the same two outcomes.
    if outcome in (CreditOutcome.CREDIT_REFUSED, CreditOutcome.INVALID_AMOUNT):
        PAYMENT_CREDIT_FAILURES.labels(provider=event.provider.value, reason=outcome.value).inc()
        await _alert_credit_refused(application, event, outcome)

    # #226: the payment is genuine and the signature checked out, but a
    # reversal for this id got here first. Refusing the credit is the
    # right call — the merchant already sent the money back — yet it
    # must never be a silent one: if the refund is later cancelled the
    # buyer is short their coins with nothing in the log to explain it.
    if outcome is CreditOutcome.REVERSED:
        await _alert_reversed_payment(application, event)

    if outcome is CreditOutcome.CREDITED:
        # Open a brief users-session for the language lookup. The DM
        # path is intentionally NOT routed through PaymentsService
        # again — that service is anchored to an economy-session for
        # the credit pipeline, and reconstructing it just to call
        # one method would force fake economy/idempotency
        # dependencies for the users-DB scope. Direct DM here is the
        # smaller surface.
        # #1184: the guard is around the OPEN, not just the send.
        # ``_send_topup_dm`` swallows everything it raises, but the
        # ``async with`` above it does not: a locked ``users.db`` here
        # raises AFTER the economy transaction committed, the route
        # reads that as a transient pipeline fault and answers 503, and
        # the provider's redelivery is swallowed by the idempotency row
        # it just wrote. The buyer keeps the coins and never sees a
        # receipt — which is precisely a missed receipt, so it is
        # counted as one.
        try:
            async with engines.session(DBName.USERS)() as users_session:
                await _send_topup_dm(
                    bot=bot,
                    user_settings=UserSettingsRepo(users_session),
                    event=event,
                )
        except Exception as exc:  # noqa: BLE001 — post-commit, best-effort
            _emit_dm_failure(event, exc)
        # #75: legacy DMs the inviter their kickback receipt right after
        # crediting it (bot.py:9870-9876). Best-effort, post-commit —
        # by the time we're here the kickback either landed in the same
        # transaction as the top-up or didn't happen at all. The
        # developer half sends NO DM (legacy only logs, bot.py:9895).
        commissions = payments_service.last_commissions
        if (
            commissions is not None
            and commissions.referral.outcome is CommissionOutcome.CREDITED
            and commissions.referral.referrer_id is not None
        ):
            # Same #1184 guard, log-only: per the note on
            # ``_send_referral_commission_dm``, PAYMENT_DM_FAILURES
            # tracks missed BUYER receipts (an SLO signal), and a
            # missed inviter courtesy note is not one.
            try:
                async with engines.session(DBName.USERS)() as users_session:
                    await _send_referral_commission_dm(
                        bot=bot,
                        user_settings=UserSettingsRepo(users_session),
                        referrer_id=commissions.referral.referrer_id,
                        commission=commissions.referral.commission,
                        percent=application.settings.economy.referral_commission_percent,
                    )
            except Exception as exc:  # noqa: BLE001 — post-commit, best-effort
                log.warning("referral commission DM session failed: {e}", e=exc)
    return outcome


# DB faults the provider's own retry ladder can fix by simply asking
# again later. ``OperationalError`` is what SQLAlchemy wraps SQLite's
# "database is locked" (SQLITE_BUSY survives ``busy_timeout`` under
# real contention — the credit path takes the write lock while other
# handlers hold it) and "disk I/O error" in; ``InterfaceError`` covers
# a connection dropped underneath us. Both are "try again in a
# minute", not "this event is poison".
_RETRYABLE_DB_ERRORS = (OperationalError, InterfaceError)


def _pipeline_failure_is_retryable(exc: BaseException, *, route: str, provider: str) -> bool:
    """Count + log a crashed credit pipeline; ``True`` ⇒ ask for a retry.

    Every route used to ack 200 on ANY exception, with the rationale
    "a poison invoice must not loop". That is right for a
    deterministic fault — a malformed event or a bug in our code will
    fail identically on every redelivery, so retries only burn the
    provider's ladder. It is wrong for a transient one: the
    transaction rolled back, nothing was credited, and 200 tells the
    provider to forget a payment the customer actually made. The user
    is out real money and the only trace is a log line somebody has to
    notice.

    So we split the two. Transient DB faults get a 5xx and ride the
    provider's retry ladder (Crypto Pay, YooKassa, Stripe and RollyPay
    all redeliver on non-2xx); everything else keeps the old 200. A
    retry is safe by construction: the credit and its
    ``processed_webhooks`` row commit together, so a rolled-back
    attempt leaves no trace and the redelivery re-runs the whole
    pipeline from the idempotency gate. If the first attempt DID
    commit, the redelivery finds the row and returns ``IDEMPOTENT``.
    """
    if isinstance(exc, _RETRYABLE_DB_ERRORS):
        PAYMENT_CREDIT_FAILURES.labels(provider=provider, reason="db_unavailable").inc()
        log.opt(exception=exc).error(
            "{route}: credit pipeline hit a transient DB fault — asking for a retry",
            route=route,
        )
        return True
    PAYMENT_CREDIT_FAILURES.labels(provider=provider, reason="pipeline_crash").inc()
    log.opt(exception=exc).error("{route}: credit pipeline crashed", route=route)
    return False


async def _send_referral_commission_dm(
    *,
    bot: Bot,
    user_settings: UserSettingsRepo,
    referrer_id: int,
    commission: int,
    percent: int,
) -> None:
    """Best-effort kickback receipt to the inviter. Swallows all exceptions.

    Legacy parity (bot.py:9870-9876): resolve the REFERRER's language
    (not the buyer's), render the one-line "+N coins (P%)" notice and
    DM it; any failure (blocked bot, lang-lookup blip) is logged and
    swallowed — the credit already committed and must stay committed.
    No ``PAYMENT_DM_FAILURES`` bump: that counter tracks missed BUYER
    receipts (an SLO signal); a missed inviter courtesy note is log-only.
    """
    lang = "ru"
    try:
        lang = await user_settings.get_language(referrer_id) or "ru"
    except Exception as exc:
        log.warning(
            "referral-dm: lang lookup failed uid={uid}: {exc}",
            uid=referrer_id,
            exc=exc,
        )
    try:
        text = render_referral_commission_notice(lang, commission=commission, percent=percent)
        await bot.send_message(referrer_id, text)
    except Exception as exc:
        log.warning("referral-dm: send failed uid={uid}: {exc}", uid=referrer_id, exc=exc)


def _emit_dm_failure(event: ParsedEvent, exc: BaseException) -> None:
    """Count + log one missed buyer receipt.

    Extracted rather than repeated. The comment on the call site below
    is the reason: distinguishing forbidden from transient happens
    downstream of the log via the ``exc_type`` label, which only works
    while there is exactly one place the label is produced. #1184 added
    a second failure point — the users-session open, one line before
    the send — and two copies of this block would have drifted.
    """
    PAYMENT_DM_FAILURES.labels(provider=event.provider.value).inc()
    log.bind(
        provider=event.provider.value,
        external_id=event.external_id,
        user_id=event.user_id,
        exc_type=type(exc).__name__,
        exc_msg=str(exc),
    ).error("payment_dm_failed")


async def _send_topup_dm(
    *,
    bot: Bot,
    user_settings: UserSettingsRepo,
    event: ParsedEvent,
) -> None:
    """Best-effort confirmation DM. Swallows all exceptions.

    HTML parse mode is the new pipeline's global default (see
    ``di/providers.py:AppProvider.bot``). The i18n key
    ``balance_topup_ok`` is the same template legacy renders, so a
    user sees byte-identical text across the cutover.

    M-E-5: a DM failure here means the wallet was already credited
    (the economy session committed before this function runs) but the
    user never saw the receipt. We surface that explicitly via:

    * A structured ``payment_dm_failed`` log line carrying provider,
      external_id, user_id, exception class + message — enough for
      grep-based reconstruction of "which receipts did the user miss"
      without having to cross-reference balance changes against
      Telegram outbound logs.
    * A ``tib_payment_dm_failures_total{provider}`` counter for SLO
      alerting; the counter is per-provider so a flaky single
      integration doesn't drown the others out.

    Three exception classes get the structured-failure treatment:
    ``TelegramForbiddenError`` (user blocked the bot — common, not a
    bug), ``TelegramAPIError`` (any other Telegram-side failure —
    rate-limit, network blip), and the bare ``Exception`` tail (for
    anything else like a transient ``users.db`` connection error
    inside the lang lookup that we don't want to crash the webhook).
    All three keep the same byte-for-byte return contract: the DM
    failure must not raise out of this function or the webhook would
    flip from 200 to 500 and the provider would retry a credit that
    already landed.
    """
    lang = "ru"
    try:
        lang = await user_settings.get_language(event.user_id) or "ru"
    except Exception as exc:
        log.warning(
            "payments-dm: lang lookup failed uid={uid}: {exc}",
            uid=event.user_id,
            exc=exc,
        )
    try:
        text = t("balance_topup_ok", lang, coins_amt=event.coins, sign=_COIN_EMOJI)
        await bot.send_message(event.user_id, text)
    except Exception as exc:
        # Single arm: every failure class (TelegramForbiddenError when
        # the user blocked the bot, TelegramAPIError on transient
        # network/upstream issues, anything else from the t() lookup or
        # serializer) produces the same structured log + counter bump.
        # Distinguishing forbidden vs. transient happens downstream of
        # the log via the ``exc_type`` label, not here — one arm avoids
        # drift between two emission paths. ``Exception`` already
        # subsumes the Telegram subclasses, so we don't list them
        # redundantly.
        _emit_dm_failure(event, exc)


def _oversized(request: Request) -> bool:
    """Whether this callback declares more body than any of them sends.

    Checked before anything else in every route — before the secret is
    resolved (the Crypto Pay one reads it from the database), before the
    body is materialised, before the signature is computed over it. A
    caller who can reach these public URLs must not be able to pick how
    much memory the process spends deciding they are unauthorised.

    Memory is all this bounds. It says nothing about I/O: a request
    declaring two bytes passes. The Crypto Pay route therefore also
    refuses a missing signature header before it resolves its token out
    of the database.
    """
    return declared_body_too_large(request, max_bytes=PAYMENT_WEBHOOK_MAX_BYTES)


def build_router(currency_service: CurrencyService | None = None) -> APIRouter:
    """Construct the payments APIRouter.

    Returns a fresh router each call so ``create_app`` can mount a
    clean instance per FastAPI factory invocation — matters for tests
    that build multiple apps in one process.

    ``currency_service`` supplies the live USD/RUB that prices a rouble
    top-up (R11). Optional and defaulted to ``None`` on purpose: a
    router built without one prices at the offline anchor, which is
    byte-identical to the pre-R11 behaviour, so no caller that doesn't
    care about FX has to grow a dependency — or make a network call —
    to keep working. ``create_app`` wires the real one.
    """

    router = APIRouter(tags=["payments"])

    @router.post("/crypto-webhook")
    async def crypto_webhook(request: Request) -> Response:
        if _oversized(request):
            log.warning("crypto-webhook: oversized body refused unread")
            return JSONResponse(
                {"ok": False, "error": "payload_too_large"},
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        application: Application = request.app.state.application
        body = await read_body_capped(request, max_bytes=PAYMENT_WEBHOOK_MAX_BYTES)
        if body is None:
            log.warning("crypto-webhook: oversized chunked body abandoned mid-read")
            return JSONResponse(
                {"ok": False, "error": "payload_too_large"},
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        # Refuse a caller whose signature cannot possibly verify BEFORE
        # resolving the token. Resolving opens an ``economy.db`` session
        # and issues a SELECT against ``runtime_secrets`` — on the same
        # event loop, pool and SQLite file the live RollyPay credit path
        # uses. Both gates above bound only MEMORY, so an unsigned POST
        # used to sail through and drive that query; anyone who can reach
        # this public URL could aim unbounded database work at the money
        # path from outside, on a small host with no rate limit.
        # Legacy read the token from the process environment
        # (``bot.py:3182``) and touched no database before the HMAC —
        # T-027 moved it into the DB without putting a gate in front. The
        # sibling RollyPay route states the rule this restores, in the
        # comment above its own ``verify_signature`` call: prove the
        # caller first, spend resources second.
        #
        # #1470: the first version of this gate checked only that the
        # header was PRESENT, which one arbitrary byte satisfies — the
        # SELECT was still one forged request away. The check is on the
        # SHAPE now, and it is exact rather than heuristic: only a
        # 64-character hex string can survive ``compare_digest`` against
        # a ``hexdigest()``, so a 403 here is the same verdict the full
        # check would reach, minus the database.
        #
        # Not solved with a cached resolver, which is the other obvious
        # answer: the whole point of T-027 is that a token set from the
        # in-bot panel takes effect on the NEXT request, and a TTL would
        # trade that away to buy what one regex already buys.
        #
        # Both casings are looked up because ``request.headers``
        # lowercases on lookup while the adapter accepts either.
        signature_header = (
            request.headers.get("crypto-pay-api-signature")
            or request.headers.get("Crypto-Pay-API-Signature")
            or ""
        ).strip()
        if not _CRYPTO_SIGNATURE_SHAPE.match(signature_header):
            log.warning("crypto-webhook: unusable signature header — refused unresolved")
            return JSONResponse(
                {"ok": False, "error": "invalid_signature"},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        # T-027: resolve the EFFECTIVE token at call time — the
        # ``economy.runtime_secrets`` override (set via the in-bot
        # ``/set_crypto_token`` panel) wins over the ``.env`` value, so
        # a token set without a redeploy feeds the inbound signature
        # check here AND the outbound client consistently. ``None`` ⇒
        # neither source set ⇒ degraded 503 (same posture as before).
        token = await resolve_crypto_token(
            registry=application.engines, settings=application.settings
        )
        if token is None:
            log.warning("crypto-webhook: CRYPTO_PAY_TOKEN not configured — 503")
            return JSONResponse(
                {"ok": False, "error": "not_configured"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        adapter = CryptoAdapter(token)
        # ``dict(request.headers)`` lowercases keys; the adapter
        # accepts both casings, so the lookup works either way.
        if not adapter.verify_signature(dict(request.headers), body):
            log.warning("crypto-webhook: invalid signature")
            return JSONResponse(
                {"ok": False, "error": "invalid_signature"},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        event = adapter.parse_event(body)
        if event is None:
            # Non-credit event (e.g. invoice_expired) or malformed
            # payload — 200 ack so the provider stops retrying.
            #
            # #1183: the ack was the whole of it. The signature already
            # proved the body came from Crypto Pay, so a refusal on a
            # body that reports a PAID invoice is a payer who is out
            # real crypto with no coins and nobody told. Same alert,
            # same counter and same asymmetry as the RollyPay route
            # below — the predicate stays permissive on the update type
            # on purpose.
            if CryptoAdapter.describes_paid_money(body):
                await _alert_uncredited(
                    application, Provider.CRYPTO, _crypto_uncredited_facts(body)
                )
            return JSONResponse({"ok": True})
        try:
            outcome = await _credit_event(application, event)
        except Exception as exc:
            if _pipeline_failure_is_retryable(
                exc, route="crypto-webhook", provider=event.provider.value
            ):
                return JSONResponse(
                    {"ok": False, "error": "temporary_failure"},
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            # Crypto Pay retries on non-2xx — return 200 so a poison
            # invoice doesn't loop. Operator finds it in the log.
            return JSONResponse({"ok": True})
        log.info("crypto-webhook: outcome={outcome}", outcome=outcome.value)
        return JSONResponse({"ok": True})

    @router.post("/yookassa-webhook")
    async def yookassa_webhook(request: Request) -> Response:
        if _oversized(request):
            log.warning("yookassa-webhook: oversized body refused unread")
            return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        application = request.app.state.application
        cfg = application.settings.payments
        if not cfg.yookassa_configured:
            log.warning("yookassa-webhook: YOOKASSA_SHOP_ID/SECRET_KEY not configured — 503")
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        # Before the body is read, let alone reverified: the whole
        # point is to make a flood cheap for us and pointless for the
        # sender. 429 rather than 403 — this is not a claim that the
        # caller is forged, it is a claim that we will not call the
        # merchant API this often, and YooKassa redelivers a non-200
        # for 24 hours either way.
        if not _REVERIFY_THROTTLE.admit(client_key(request), now=time.monotonic()):
            PAYMENT_CREDIT_FAILURES.labels(
                provider=Provider.YOOKASSA.value, reason="rate_limited"
            ).inc()
            log.warning("yookassa-webhook: rate limited — 429, asking for redelivery")
            return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)
        assert cfg.yookassa_shop_id is not None  # noqa: S101
        assert cfg.yookassa_secret is not None  # noqa: S101
        shop_id = cfg.yookassa_shop_id
        secret = cfg.yookassa_secret.get_secret_value()
        body = await read_body_capped(request, max_bytes=PAYMENT_WEBHOOK_MAX_BYTES)
        if body is None:
            log.warning("yookassa-webhook: oversized chunked body abandoned mid-read")
            return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        # Gate first, FX second — see the note on the RollyPay route.
        # This gate is far weaker (YooKassa doesn't sign webhooks; the
        # real authentication is the reverify inside ``parse_event``),
        # so the ordering buys less here. It costs nothing and keeps
        # both fiat routes reading the same way.
        if not YooKassaAdapter(shop_id, secret).verify_signature(dict(request.headers), body):
            # Shop-credentials gate (defense in depth — the 503 above
            # should have caught the missing-secret case already).
            log.warning("yookassa-webhook: shop credentials gate failed")
            return Response(status_code=status.HTTP_403_FORBIDDEN)
        adapter = YooKassaAdapter(
            shop_id,
            secret,
            # R11: roubles are priced through the dollar anchor the
            # withdraw desk pays out at. Resolved here, in async land,
            # because the adapter API is sync by design.
            usd_to_rub=await resolve_usd_to_rub(currency_service),
        )
        try:
            # #228: ``parse_event`` reverifies against the merchant
            # API over blocking HTTPS. Called inline it parks the
            # single event loop for the SDK's whole timeout-and-retry
            # budget, so one POST to a public, unsigned URL freezes
            # every other handler the bot has — commands, captchas,
            # the other payment routes. Off-loop, with a ceiling.
            #
            # Honest about what the ceiling does not buy: the timeout
            # cannot cancel a call that already reached a thread, so a
            # flood still saturates the pool below. What survives is
            # the *loop* — queued deliveries answer uncredited and ask
            # to be redelivered.
            #
            # #1469: "and the rest of the bot keeps serving" is what
            # this used to claim, and it was not true while the call
            # went to the loop's DEFAULT executor. That one is shared,
            # and its other users are every image the bot draws — the
            # profile card (``handlers/profile.py``), ``/stats``,
            # ``/chatstats`` and the guide site's renderer. A flood on
            # this unauthenticated route saturated it and those stopped
            # rendering.
            #
            # #1439 gives the reverify a pool of its own, so the only
            # thing a flood can starve is the next reverify. What is
            # still true: the timeout cannot cancel a call that has
            # already reached a thread. What it *can* do is cancel one
            # that is still queued, which past four concurrent
            # deliveries is all of them — they answer uncredited and
            # ask to be redelivered, without ever touching the
            # merchant API.
            event = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    _reverify_pool(), adapter.parse_event, body
                ),
                _YOOKASSA_REVERIFY_TIMEOUT_S,
            )
        except TimeoutError:
            # 503, not 200 (#1610). This branch used to answer 200 on
            # the reasoning that "YooKassa redelivers either way, and
            # a 5xx on a route anyone can POST to is a free error
            # budget". The first clause is false and it carried the
            # second: YooKassa's documented contract is that HTTP 200
            # acknowledges a notification and stops its delivery, and
            # that anything else keeps it being redelivered for 24
            # hours from the event. Answering 200 here therefore did
            # not defer the credit — it discarded a real payment, with
            # a log line as the only trace and no documented way to
            # ask for the notification again.
            #
            # It is the same call ``_pipeline_failure_is_retryable``
            # already makes one branch down, for the same reason: we
            # did not decide anything, nothing was written, so the
            # honest answer is "ask me again". The error budget is a
            # real cost and it is the smaller one; bounding the flood
            # that can provoke this is #1439.
            PAYMENT_CREDIT_FAILURES.labels(
                provider=Provider.YOOKASSA.value, reason="reverify_timeout"
            ).inc()
            log.warning(
                "yookassa-webhook: reverify timed out after {s}s — "
                "503 without credit, asking for redelivery",
                s=_YOOKASSA_REVERIFY_TIMEOUT_S,
            )
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        except ReverifyUnavailable as exc:
            # #2010. The same answer as the timeout above, for the
            # same reason and against the same mistake: the reverify
            # did not finish, so we decided nothing and wrote nothing.
            #
            # This branch is younger than the one above only because
            # the outcome used to be invisible here. ``parse_event``
            # spelled a reverify that raised — dead socket, merchant
            # API 5xx, rejected credentials, or the ``ImportError``
            # from an optional SDK that is not installed on prod —
            # ``None``, and ``None`` on this route is acknowledged
            # with 200 as "not money". YooKassa stops redelivering
            # after a 200, so a payer could be charged, credited
            # nothing, and leave no trace but a WARNING.
            PAYMENT_CREDIT_FAILURES.labels(
                provider=Provider.YOOKASSA.value, reason="reverify_unavailable"
            ).inc()
            log.warning(
                "yookassa-webhook: reverify could not complete ({exc}) — "
                "503 without credit, asking for redelivery",
                exc=exc,
            )
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        if isinstance(event, UncreditedPayment):
            # #1643. The payer paid and got nothing. Until this branch
            # existed the only trace was a WARNING, because the parser
            # spelled this outcome ``None`` — the same ``None`` it uses
            # for "not a payment at all", which is why the route below
            # could not tell them apart and the comment there said the
            # money gap stayed open.
            #
            # Nothing here is read off the body. Every field comes from
            # the object ``Payment.find_one`` returned, so the card is
            # backed by the merchant API even though the callback that
            # provoked it was anonymous — which is exactly the
            # distinction that kept a body-shaped predicate out of this
            # route (#1611).
            #
            # ``rationed``: a payer can arrange one stuck payment (a
            # sub-one-coin top-up is the cheapest) and then replay its
            # notification body in a loop. The reverify answers
            # "succeeded" every time, so without a ceiling the owner's
            # phone rings for the price of a single small payment. Same
            # remedy and same reasoning as #188 took on the refund card.
            await _alert_uncredited(
                application,
                Provider.YOOKASSA,
                _ReversalFacts(
                    payment_id=event.external_id or "?",
                    # The payer, not an order id: YooKassa has no
                    # merchant-side order and what the owner needs in
                    # order to credit by hand is the user the parser
                    # could not use. When that is the field that was
                    # missing it renders as "?", which is the diagnosis
                    # — the same pattern ``_crypto_uncredited_facts``
                    # documents.
                    ref_label="Пользователь",
                    ref=event.payer or "?",
                    amount=event.amount,
                ),
                cause=event.cause,
                rationed=True,
            )
            return Response(status_code=status.HTTP_200_OK)
        if event is None:
            # Non-credit event, malformed body, OR reverify miss.
            # Legacy returns 200 for all three; we match exactly so
            # YooKassa doesn't retry a benign or spoofed event forever.
            #
            # #140: one of those "non-credit events" is a refund, and
            # a refund against a top-up is money leaving the merchant
            # account for coins that are already in circulation — the
            # one case here the owner has to hear about. Until now the
            # rouble provider with the *higher* chargeback exposure was
            # the silent one: RollyPay alerted, YooKassa did not.
            event_type = YooKassaAdapter.classify(body)
            if event_type in YOOKASSA_REVERSAL_EVENTS:
                await _alert_reversal(
                    application,
                    Provider.YOOKASSA,
                    event_type,
                    _yookassa_reversal_facts(body),
                    # #188: ``verify_signature`` above is a credentials
                    # gate, not a signature check — YooKassa signs
                    # nothing. Everything below this line came from an
                    # anonymous POST to a public URL until the provider
                    # itself confirms otherwise.
                    authenticated=False,
                )
            # Still no ``elif …describes_paid_money(body)`` arm here,
            # unlike the Stripe and RollyPay branches below, and still
            # for the #1611 reasons: those two routes reach that
            # predicate with an HMAC behind them, this one has only a
            # credentials gate. The reasoning in full sits where the
            # predicate would have been, in
            # ``services/payments/yookassa.py``.
            #
            # What changed is that the arm is no longer needed. Every
            # refusal it was wanted for is now answered above by the
            # ``UncreditedPayment`` branch, off the reverified payment
            # rather than off the body — #1643, which this comment used
            # to name as an open gap. What reaches this line is what
            # the predicate could never have identified anyway: a body
            # that describes no money we can confirm.
            return Response(status_code=status.HTTP_200_OK)
        try:
            outcome = await _credit_event(application, event)
        except Exception as exc:
            if _pipeline_failure_is_retryable(
                exc, route="yookassa-webhook", provider=event.provider.value
            ):
                return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
            return Response(status_code=status.HTTP_200_OK)
        log.info("yookassa-webhook: outcome={outcome}", outcome=outcome.value)
        return Response(status_code=status.HTTP_200_OK)

    @router.post("/stripe-webhook")
    async def stripe_webhook(request: Request) -> Response:
        if _oversized(request):
            log.warning("stripe-webhook: oversized body refused unread")
            return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        application = request.app.state.application
        cfg = application.settings.payments
        if not cfg.stripe_configured:
            log.warning("stripe-webhook: STRIPE_WEBHOOK_SECRET not configured — 503")
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        assert cfg.stripe_webhook_secret is not None  # noqa: S101
        adapter = StripeAdapter(cfg.stripe_webhook_secret.get_secret_value())
        body = await read_body_capped(request, max_bytes=PAYMENT_WEBHOOK_MAX_BYTES)
        if body is None:
            log.warning("stripe-webhook: oversized chunked body abandoned mid-read")
            return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        if not adapter.verify_signature(dict(request.headers), body):
            # Stripe wants 400 (NOT 403/5xx) on signature failure:
            # the dashboard surfaces 4xx as a delivery error and
            # Stripe does NOT retry on 4xx — so a poison/forged
            # event stops after the first 400 instead of looping.
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
        event = adapter.parse_event(body)
        if event is None:
            # Non-checkout-completed event or malformed metadata.
            # 200 ack; Stripe stops retrying.
            #
            # #141: except when it is a refund or a dispute. Stripe is
            # the provider where a chargeback is most likely to arrive
            # months later, against coins long since spent — the owner
            # hears about it here or at a reconciliation.
            event_type = StripeAdapter.classify(body)
            if event_type in STRIPE_REVERSAL_EVENTS:
                await _alert_reversal(
                    application,
                    Provider.STRIPE,
                    event_type,
                    _stripe_reversal_facts(body),
                )
            elif StripeAdapter.describes_paid_money(body):
                # #1527: and the third case — the session cleared and
                # the parser still refused it. On this provider the
                # reachable refusals are a settlement in something
                # other than USD and a session carrying no
                # ``metadata.user_id``; both leave the money on the
                # merchant account with the payer's balance untouched,
                # which until now only a WARNING in the journal said
                # out loud.
                #
                # ``elif``, not a second ``if``. The two event sets
                # are disjoint by name, but this predicate keys on
                # the *object* rather than the type, so a reversal
                # event delivered with a session in ``data.object``
                # satisfies both — and two alerts for one event is
                # how an owner learns to stop reading them. The
                # reversal card wins because it is the one that
                # names what happened.
                await _alert_uncredited(
                    application,
                    Provider.STRIPE,
                    _stripe_uncredited_facts(body),
                )
            return Response(status_code=status.HTTP_200_OK)
        try:
            outcome = await _credit_event(application, event)
        except Exception as exc:
            if _pipeline_failure_is_retryable(
                exc, route="stripe-webhook", provider=event.provider.value
            ):
                # Stripe redelivers on 5xx (and only on 5xx — the
                # signature branch above deliberately answers 400 to
                # stop a forged event dead).
                return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
            return Response(status_code=status.HTTP_200_OK)
        log.info("stripe-webhook: outcome={outcome}", outcome=outcome.value)
        return Response(status_code=status.HTTP_200_OK)

    @router.post("/rollypay-webhook")
    async def rollypay_webhook(request: Request) -> Response:
        if _oversized(request):
            log.warning("rollypay-webhook: oversized body refused unread")
            return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        application = request.app.state.application
        cfg = application.settings.payments
        if cfg.rollypay_signing_secret is None:
            log.warning("rollypay-webhook: ROLLYPAY_SIGNING_SECRET not configured — 503")
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        secret = cfg.rollypay_signing_secret.get_secret_value()
        body = await read_body_capped(request, max_bytes=PAYMENT_WEBHOOK_MAX_BYTES)
        if body is None:
            log.warning("rollypay-webhook: oversized chunked body abandoned mid-read")
            return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        # The HMAC check needs the signing secret and nothing else. The
        # rate is what *pricing* needs, and pricing is downstream of
        # proving the caller is RollyPay — so it is resolved after the
        # gate, not before it. Resolving first would let anyone who can
        # reach this public URL drive lookups against a metered FX key,
        # and make a forged request that is about to be rejected wait
        # out the full FX timeout before it gets its 403.
        if not RollyPayAdapter(secret).verify_signature(dict(request.headers), body):
            # A real HMAC failure, unlike the YooKassa route's
            # credentials gate. 403 and no database contact.
            log.warning("rollypay-webhook: signature verification failed")
            return Response(status_code=status.HTTP_403_FORBIDDEN)
        adapter = RollyPayAdapter(
            secret,
            # Same rouble pricing as the YooKassa leg — both fiat
            # providers go through the dollar anchor so they cannot
            # drift into two different prices for one coin.
            usd_to_rub=await resolve_usd_to_rub(currency_service),
        )
        event = adapter.parse_event(body)
        if event is None:
            # Signature was good, so whatever this is, it came from
            # RollyPay. Two very different cases hide behind None: a
            # benign lifecycle event (created/expired/canceled), and a
            # reversal — money leaving the merchant account for a top-up
            # that already minted coins. Only the second one is news.
            event_type = adapter.classify(body)
            if event_type in ROLLYPAY_REVERSAL_EVENTS:
                await _alert_reversal(
                    application,
                    Provider.ROLLYPAY,
                    event_type,
                    _rollypay_reversal_facts(body),
                )
            elif RollyPayAdapter.describes_paid_money(body):
                # #144: and the third case — the payment cleared and the
                # parser still refused it. That refusal is right (a sum
                # we cannot price must not be guessed at), but it leaves
                # someone paid and empty-handed, which until now only a
                # WARNING in the journal said out loud. RollyPay is the
                # provider where this is most reachable: its page takes
                # crypto as well as card and SBP, and a crypto leg is
                # the likeliest thing to report a currency the RUB gate
                # refuses.
                await _alert_uncredited(
                    application,
                    Provider.ROLLYPAY,
                    _rollypay_reversal_facts(body),
                )
            return Response(status_code=status.HTTP_200_OK)
        try:
            outcome = await _credit_event(application, event)
        except Exception as exc:
            if _pipeline_failure_is_retryable(
                exc, route="rollypay-webhook", provider=event.provider.value
            ):
                # Spend the 8-attempt ladder on exactly the case it
                # exists for: the DB was busy, try us again.
                return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
            # 200 so RollyPay stops its 8-attempt retry ladder on a
            # poison event; the counter and the log carry it instead.
            return Response(status_code=status.HTTP_200_OK)
        log.info("rollypay-webhook: outcome={outcome}", outcome=outcome.value)
        return Response(status_code=status.HTTP_200_OK)

    return router


#: How each provider is named to the owner. Their own spelling, so a
#: line in the alert can be matched against a line in their dashboard
#: without a translation step.
_PROVIDER_TITLE: Final[dict[Provider, str]] = {
    Provider.ROLLYPAY: "RollyPay",
    Provider.YOOKASSA: "ЮKassa",
    Provider.STRIPE: "Stripe",
    Provider.CRYPTO: "Crypto Pay",
    Provider.STARS: "Telegram Stars",
}


#: Why a *verified* payment on this provider gets refused a credit —
#: the shortlist the owner reads before opening the journal. Per
#: provider because the parsers refuse for different reasons: the
#: RollyPay list is its three (settlement not in RUB, no ``user_id`` in
#: metadata, under one coin), and pasting those beside a Crypto Pay
#: invoice would send the owner looking for a rouble field that route
#: never had. A provider absent from this table simply gets no
#: shortlist; the sentence still reads.
#:
#: YooKassa is absent deliberately (#1643) and not by oversight. On
#: that route the parser hands the exact refusal up as an
#: :class:`UncreditedCause`, so the alert prints the cause that
#: actually fired; a "чаще всего" shortlist printed beside it would be
#: three guesses next to one fact. See :data:`_UNCREDITED_VERDICT`.
_UNCREDITED_CAUSES: Final[dict[Provider, str]] = {
    Provider.ROLLYPAY: "расчёт не в рублях, нет user_id в metadata или сумма меньше одной монеты",
    Provider.CRYPTO: "нет user_id в payload, сумма не читается или меньше одной монеты",
    Provider.STRIPE: "расчёт не в долларах, нет user_id в metadata или сумма меньше одной монеты",
}


#: The ``log.bind`` component prefix each route's refusals are written
#: under, so "точная причина — в журнале" names a string that actually
#: greps. Defaults to the provider's own value, which is what every
#: current prefix is built from anyway.
_UNCREDITED_JOURNAL: Final[dict[Provider, str]] = {
    Provider.ROLLYPAY: "rollypay",
    Provider.CRYPTO: "crypto",
    Provider.STRIPE: "stripe",
    Provider.YOOKASSA: "payments.yookassa",
}


#: What the owner is told about the *authenticity* of the payment. The
#: default sentence is the one three of the four routes earn: their
#: callback carried an HMAC. YooKassa signs nothing (see the module
#: docstring of ``services/payments/yookassa.py``), so telling its
#: owner "подпись верна" would be a false claim printed beside a real
#: payment id — the objection that kept the whole alert off this route
#: until #1643. What that route can honestly say is stronger anyway:
#: the payment was confirmed by asking ЮKassa itself.
_UNCREDITED_PROOF_DEFAULT: Final[str] = "Подпись верна, платёж настоящий"
_UNCREDITED_PROOF: Final[dict[Provider, str]] = {
    Provider.YOOKASSA: "Платёж подтверждён повторным запросом к API ЮKassa — он настоящий",
}


#: The exact refusal, in the owner's words. Keyed by the machine value
#: the adapter produced (#1643): the parser knows which gate fired, the
#: router owns how it reads, and keeping the prose here is what lets
#: ``services/payments/`` stay the pure value-producer its package
#: docstring promises.
#:
#: Each line says what to do next, because the answers differ: a
#: settlement in the wrong currency is a checkout misconfiguration, a
#: missing ``user_id`` is a hand-credit, and an amount under one coin
#: is a refund. A cause absent from this table falls back to the
#: shortlist wording, so a new member cannot silence the alert.
_UNCREDITED_VERDICT: Final[dict[UncreditedCause, str]] = {
    UncreditedCause.BAD_METADATA: "metadata платежа не читается",
    UncreditedCause.NO_USER_ID: "в metadata платежа нет user_id — некому зачислять",
    UncreditedCause.NOT_RUB: "расчёт не в рублях",
    UncreditedCause.BAD_AMOUNT: "сумма платежа не читается",
    UncreditedCause.BELOW_ONE_COIN: "сумма меньше одной монеты",
}


#: How much of one identifier reaches the alert. Ids are short by every
#: provider's own convention; the ceiling exists for the body that does
#: not follow it. Telegram refuses a message over 4096 characters, and a
#: refused message means the owner hears nothing at all about a reversal
#: — so a truncated id beats a swallowed alert. Escaping can multiply a
#: character sixfold (``&amp;#x27;``), which five clamped fields still
#: clear by an order of magnitude.
_FACT_MAX_CHARS: Final[int] = 96


def _clamp(value: str) -> str:
    """One identifier, short enough that five of them cannot break 4096."""
    if len(value) <= _FACT_MAX_CHARS:
        return value
    return value[: _FACT_MAX_CHARS - 1] + "…"


@dataclass(frozen=True, slots=True)
class _ReversalFacts:
    """The identifiers an owner needs to find one reversal again.

    Every field defaults to the "we could not read it" placeholder: the
    alert must go out even when the body is a shape we have never seen,
    because a reversal we cannot parse is not less urgent than one we
    can. The second identifier is labelled per provider — RollyPay
    keys reversals by the merchant's order id, YooKassa by a refund id
    of its own — since a line reading "Заказ" against a refund id would
    send the owner looking in the wrong column of the wrong dashboard.
    """

    payment_id: str = "?"
    ref_label: str = "Заказ"
    ref: str = "?"
    amount: str = ""


def _reversal_body(body: bytes) -> dict[str, object]:
    """The body as a dict, or empty — a parse failure is not fatal here.

    The alert that follows still fires with placeholder fields, and
    that alert is the one an operator must not miss.
    """
    try:
        data = json.loads(body)
    except Exception as exc:  # noqa: BLE001 — the alert must survive a weird body
        log.debug("reversal body not parseable: {e}", e=exc)
        return {}
    return data if isinstance(data, dict) else {}


def _rollypay_reversal_facts(body: bytes) -> _ReversalFacts:
    """RollyPay puts everything at the top level of the callback."""
    data = _reversal_body(body)
    return _ReversalFacts(
        payment_id=str(data.get("payment_id") or "?"),
        ref_label="Заказ",
        ref=str(data.get("order_id") or "?"),
        amount=f"{data.get('amount')} {data.get('currency') or ''}".strip(),
    )


def _crypto_uncredited_facts(body: bytes) -> _ReversalFacts:
    """Crypto Pay nests the invoice one level down, under ``payload``.

    The second identifier is the payer, not an order: Crypto Pay has no
    merchant-side order id, and what the owner needs in order to credit
    by hand is exactly the user id the parser could not use. When that
    is the field that was missing it renders as ``?``, which is itself
    the diagnosis.
    """
    raw = _reversal_body(body).get("payload")
    payload: dict[str, object] = raw if isinstance(raw, dict) else {}
    return _ReversalFacts(
        payment_id=str(payload.get("invoice_id") or "?"),
        ref_label="Пользователь",
        ref=str(payload.get("payload") or "?"),
        amount=f"{payload.get('amount')} {payload.get('asset') or ''}".strip(),
    )


def _yookassa_reversal_facts(body: bytes) -> _ReversalFacts:
    """YooKassa wraps a *refund* object, not the payment.

    ``object.id`` is the refund's own id and ``object.payment_id``
    points back at the top-up that minted the coins — which is the one
    the owner needs to match against a user. Reading them the other way
    round would name an id that appears nowhere in the payment history.
    """
    obj_any = _reversal_body(body).get("object")
    obj: dict[str, object] = obj_any if isinstance(obj_any, dict) else {}
    amount_any = obj.get("amount")
    amount: dict[str, object] = amount_any if isinstance(amount_any, dict) else {}
    return _ReversalFacts(
        payment_id=str(obj.get("payment_id") or "?"),
        ref_label="Возврат",
        ref=str(obj.get("id") or "?"),
        amount=f"{amount.get('value')} {amount.get('currency') or ''}".strip() if amount else "",
    )


def _stripe_reversal_facts(body: bytes) -> _ReversalFacts:
    """Stripe wraps either a Charge (refund) or a Dispute in ``data``.

    Both shapes point back at the charge that was settled, under
    different names — ``payment_intent`` on the charge, ``charge`` on
    the dispute — and both carry the disputed/refunded sum in minor
    units, which is why the amount is divided rather than printed.
    """
    data_any = _reversal_body(body).get("data")
    data: dict[str, object] = data_any if isinstance(data_any, dict) else {}
    obj_any = data.get("object")
    obj: dict[str, object] = obj_any if isinstance(obj_any, dict) else {}
    payment_id = str(obj.get("payment_intent") or obj.get("charge") or "?")
    minor = obj.get("amount_refunded")
    if minor is None:
        minor = obj.get("amount")
    currency = str(obj.get("currency") or "").upper()
    amount = ""
    if isinstance(minor, int) and not isinstance(minor, bool):
        # Minor units are an integer by Stripe's own schema; anything
        # else is a shape we do not recognise, and a wrong number in a
        # money alert is worse than no number.
        amount = f"{minor / 100:.2f} {currency}".strip()
    return _ReversalFacts(
        payment_id=payment_id,
        ref_label="Событие",
        ref=str(obj.get("id") or "?"),
        amount=amount,
    )


def _stripe_uncredited_facts(body: bytes) -> _ReversalFacts:
    """Stripe wraps a Checkout Session here, not a charge.

    :func:`_stripe_reversal_facts` reads the same ``data.object`` and
    still cannot be reused: it looks for ``payment_intent`` /
    ``charge`` and for ``amount_refunded``, and a checkout session
    carries none of the three. Every field would render ``?`` on the
    one alert whose whole job is to name a payment.

    The first identifier is the session id, because that is what the
    Stripe dashboard lists and what a credit would have been recorded
    under. The second is the payer, as on the Crypto Pay route and
    for the same reason: what the owner needs in order to credit by
    hand is exactly the user id the parser could not use, and when
    that is the field that was missing it renders as ``?``, which is
    itself the diagnosis.
    """
    data_any = _reversal_body(body).get("data")
    data: dict[str, object] = data_any if isinstance(data_any, dict) else {}
    obj_any = data.get("object")
    sess: dict[str, object] = obj_any if isinstance(obj_any, dict) else {}
    meta_any = sess.get("metadata")
    meta: dict[str, object] = meta_any if isinstance(meta_any, dict) else {}
    minor = sess.get("amount_total")
    currency = str(sess.get("currency") or "").upper()
    amount = ""
    if isinstance(minor, int) and not isinstance(minor, bool):
        # Minor units by Stripe's own schema — divided for the same
        # reason :func:`_stripe_reversal_facts` divides, and refused
        # on any other type for the same reason too: a wrong number
        # in a money alert is worse than no number.
        amount = f"{minor / 100:.2f} {currency}".strip()
    return _ReversalFacts(
        payment_id=str(sess.get("id") or "?"),
        ref_label="Пользователь",
        ref=str(meta.get("user_id") or "?"),
        amount=amount,
    )


# Providers whose reversal event carries the SAME identifier their
# success event was credited under, so ``processed_webhooks`` resolves
# on a primary-key read. Stripe is deliberately absent: it credits by
# checkout ``session_id`` and reverses by ``payment_intent`` / ``charge``
# (see :func:`_stripe_reversal_facts`), two ids that share no column.
# Listing it here would make the alert claim "no such credit" when the
# truth is "we looked in the wrong place", and the owner acts on that
# sentence with real money.
#
# #1530: what reaches this set is narrower than the membership looks,
# and the difference is worth knowing before anyone reasons from it.
# There are four ``_alert_reversal`` call sites — the YooKassa, Stripe
# and RollyPay webhook routes, plus :func:`alert_stars_refund`, which
# is a Telegram update rather than a POST (#1987) — and two readers of
# this set: the tombstone arm and the wording arm, both inside
# :func:`_resolve_reversal_credit` / :func:`_reversal_user_lines`.
# (Line numbers used to be quoted here and had all four drifted by the
# time anyone read them; names move with the code, numbers do not.)
#   * ROLLYPAY is the only member that can reach both. Its call site
#     alerts with the default ``authenticated=True``, which becomes
#     ``stamp``, which is what the tombstone is gated on.
#   * YOOKASSA reaches the wording arm only. Its one call site passes
#     ``authenticated=False`` — YooKassa signs nothing — so ``stamp``
#     is false and the tombstone arm is unreachable for it, by the
#     #188 rule spelled out in :func:`_resolve_reversal_credit`. Its
#     membership therefore decides a sentence, never a row.
#   * CRYPTO reaches neither, today: Crypto Pay has no reversal path
#     at all (``services/payments/crypto.py`` defines no
#     ``REVERSAL_EVENTS`` and ``/crypto-webhook`` never calls
#     :func:`_alert_reversal`). It is kept rather than dropped
#     because the answer is right in advance — Crypto Pay reverses an
#     invoice under the invoice id it credited under — and because
#     dropping it would leave a future reversal arm silently printing
#     the "reverses under a different identifier" wording, which is a
#     wrong sentence about money and exactly what the Stripe
#     paragraph above exists to prevent.
#   * STARS (#1987) reaches both arms, like ROLLYPAY. Telegram
#     reuses one ``telegram_payment_charge_id`` across the
#     ``successful_payment`` and the ``refunded_payment`` for the same
#     purchase — it is the argument ``refundStarPayment`` itself takes
#     — and ``credit_stars_payment`` writes that id as
#     ``external_id``, so the lookup here is a primary-key hit. The
#     update is delivered over the bot's own authenticated channel, so
#     its call site passes ``authenticated=True`` and the stamp lands.
_REVERSAL_ID_MATCHES_CREDIT: Final[frozenset[Provider]] = frozenset(
    {Provider.ROLLYPAY, Provider.YOOKASSA, Provider.CRYPTO, Provider.STARS}
)

_UTC_STAMP: Final[str] = "%d.%m.%Y %H:%M UTC"

#: Appended to a reversal alert the bot could not authenticate (#188).
#: YooKassa sends its notifications unsigned, so this card may equally
#: be a real refund or a stranger's POST to a public URL. It is shown
#: rather than swallowed because a missed chargeback costs more than a
#: false one — but nothing was written down on its word.
_UNVERIFIED_NOTE: Final[str] = (
    "\n⚠️ <b>Событие не подтверждено.</b> YooKassa не подписывает "
    "уведомления, поэтому отправителем мог быть кто угодно. Сверьтесь с "
    "дашбордом провайдера, прежде чем действовать; в базе бота по этому "
    "событию ничего не отмечено.\n"
)

#: How many unverifiable reversal cards the owner is asked to read
#: in one hour (#228). ``/yookassa-webhook`` raises one for any
#: anonymous POST that parses as a refund, because YooKassa signs
#: nothing. Uncapped, a stranger with a loop turns the owner's DM
#: thread into a denial-of-attention channel — and the card that
#: matters, a real chargeback, arrives buried inside the flood.
#: Twelve is above every legitimate burst this route has ever
#: produced and far below the volume that makes the thread useless.
_UNVERIFIED_ALERT_BUDGET: Final[int] = 12
_UNVERIFIED_ALERT_WINDOW_S: Final[float] = 3600.0

#: ``time.monotonic()`` marks of the unverifiable alerts already
#: sent. In memory on purpose: the webhook routes are served by a
#: single worker, and a budget that outlived a restart would keep
#: the owner silenced across the very deploy that stops the flood.
_unverified_alert_marks: list[float] = []


def _unverified_alert_allowed() -> bool:
    """Take one slot of the unverifiable-alert budget.

    Returns ``False`` once :data:`_UNVERIFIED_ALERT_BUDGET` cards
    have gone out inside the trailing window. Only the DM is
    rationed: :data:`PAYMENT_REVERSALS` is bumped before this is
    ever consulted, so a flood stays fully visible in Prometheus
    while the owner stops being paged for it.

    Deliberately not deduplicated by payment id — that was the
    first idea and it is bug #188 again: two genuine deliveries of
    the same refund must still produce two cards, because the
    second may be the one the owner is awake for.
    """
    now = time.monotonic()
    cutoff = now - _UNVERIFIED_ALERT_WINDOW_S
    # Marks are appended in order, so trimming the expired head is
    # the whole eviction — the tail is by definition still fresh.
    while _unverified_alert_marks and _unverified_alert_marks[0] <= cutoff:
        del _unverified_alert_marks[0]
    if len(_unverified_alert_marks) >= _UNVERIFIED_ALERT_BUDGET:
        return False
    _unverified_alert_marks.append(now)
    return True


def reset_unverified_alert_budget() -> None:
    """Drop the unverifiable-alert budget (tests only).

    The marks are process-global, so a test that deliberately
    exhausts the budget would otherwise silence every test after it
    in the same process — including ones that pass in isolation.
    """
    _unverified_alert_marks.clear()


#: The same ration, for the paid-but-uncredited card on an unsigned
#: route (#1643). Separate list on purpose: one shared budget would let
#: a flood of one kind silence the other, and these two cards are the
#: owner's only signals for two different ways of losing money.
#:
#: The flood this bounds is cheap but not free — unlike the refund
#: card, which any anonymous POST can provoke, this one is only reached
#: after ЮKassa itself confirms a real payment. So the attacker must
#: first arrange one genuinely stuck payment (a sub-one-coin top-up is
#: the cheapest) and can then replay its notification body without
#: limit. Six an hour is well above the rate at which real checkouts
#: break and far below the volume that makes the DM thread useless.
_UNCREDITED_ALERT_BUDGET: Final[int] = 6
_UNCREDITED_ALERT_WINDOW_S: Final[float] = 3600.0
_uncredited_alert_marks: list[float] = []


def _uncredited_alert_allowed() -> bool:
    """Take one slot of the paid-but-uncredited budget.

    Deliberately not deduplicated by payment id, for the reason
    :func:`_unverified_alert_allowed` gives: a redelivery of the same
    stuck payment must still produce a second card, because the second
    may be the one the owner is awake for. :data:`PAYMENT_UNCREDITED`
    is bumped before this is consulted, so a flood stays fully visible
    in Prometheus while the owner stops being paged for it.
    """
    now = time.monotonic()
    cutoff = now - _UNCREDITED_ALERT_WINDOW_S
    while _uncredited_alert_marks and _uncredited_alert_marks[0] <= cutoff:
        del _uncredited_alert_marks[0]
    if len(_uncredited_alert_marks) >= _UNCREDITED_ALERT_BUDGET:
        return False
    _uncredited_alert_marks.append(now)
    return True


def reset_uncredited_alert_budget() -> None:
    """Drop the paid-but-uncredited budget (tests only)."""
    _uncredited_alert_marks.clear()


async def _resolve_reversal_credit(
    context: ReversalContext,
    provider: Provider,
    event_type: str,
    payment_id: str,
    *,
    stamp: bool = True,
) -> CreditRecord | None:
    """Find the credit this reversal cancels and stamp it as reversed.

    Returns the row as it was BEFORE the stamp, so the caller can tell
    a first notice from a redelivery by reading
    :attr:`CreditRecord.reversed_at`.

    Every failure is swallowed into ``None``. This runs on a route that
    has already decided to answer 200; letting a locked database or a
    schema older than migration ``0014`` raise here would suppress the
    alert entirely — and an alert naming an opaque payment id is still
    vastly better than no alert. ``None`` therefore means "could not
    resolve", never "definitely not credited", and the message the
    caller composes says exactly that.

    ``stamp=False`` reads the credit but leaves it untouched. #188: the
    stamp is a permanent claim about money, and one provider — YooKassa
    — reaches this function from a body nobody authenticated. Reading
    is harmless there; writing is not, because a single anonymous POST
    would set ``reversed_at`` on a real credit forever and every later
    alert about the genuine chargeback would then read as "already
    noted, probably a redelivery" — the owner's only chargeback signal,
    poisoned by the cheapest possible request.

    #226: when there is no credit to stamp, the reversal may simply
    have overtaken its own payment — the provider retries ``paid`` for
    the better part of an hour, and a refund issued inside that hour
    can land first. A tombstone row is written so the retry that
    follows finds the idempotency gate shut instead of crediting money
    the merchant has already returned. It is filed only when BOTH the
    event was authenticated (``stamp``, for exactly the #188 reason
    above — an anonymous POST must not be able to block a stranger's
    future top-up) and the provider reverses under the same id it
    credited under (:data:`_REVERSAL_ID_MATCHES_CREDIT`); for Stripe
    the two ids never match, so a tombstone there would bar an
    unrelated payment. The value returned is still the pre-stamp
    state — a synthetic all-zero record whose ``reversed_at`` is
    ``None`` — so the caller words a first notice as a first notice.
    """
    if not payment_id or payment_id == "?":
        return None
    try:
        async with (
            context.engines.session(DBName.ECONOMY)() as session,
            session.begin(),
        ):
            repo = ProcessedWebhooksRepo(session)
            if stamp:
                # #1440. The read below and the write that may follow
                # it are one decision, and this connection is in no
                # transaction until something write-headed runs. Taking
                # the lock first is what stops a concurrent ``paid``
                # delivery from committing its credit row between them
                # and turning the tombstone INSERT into a swallowed
                # IntegrityError — a lost ``reversed_at`` is invisible
                # until the payout desk pays out against it.
                #
                # Skipped on the ``stamp=False`` path deliberately:
                # that path is reached from a body nobody authenticated
                # (#188), and an anonymous POST must not be able to
                # make every other economy writer wait on a lock.
                await repo.lock_writer()
            record = await repo.get(provider=provider.value, external_id=payment_id)
            tombstone = record is None and stamp and provider in _REVERSAL_ID_MATCHES_CREDIT
            if record is None and not tombstone:
                return None
            if stamp:
                now = datetime.now(UTC).replace(tzinfo=None)
                await repo.mark_reversed(
                    provider=provider.value,
                    external_id=payment_id,
                    event=event_type,
                    reversed_at=now,
                    tombstone=tombstone,
                )
                if record is None:
                    record = CreditRecord(
                        provider=provider.value,
                        external_id=payment_id,
                        user_id=0,
                        credited_amount=0,
                        processed_at=now,
                        reversed_at=None,
                        reversed_event=None,
                    )
            return record
    except IntegrityError as exc:
        # #1440. With the writer lock above this should be
        # unreachable. If it fires, either the lock was not taken or a
        # second writer reached this table by a route that does not
        # take it — and a reversal was lost either way, which is a
        # fact worth an ERROR rather than one warning among many.
        log.error(
            "reversal credit stamp collided: {e}",
            e=exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — the alert outranks the lookup
        log.warning(
            "reversal credit lookup failed: {e}",
            e=exc,
        )
        return None


def _reversal_user_lines(
    provider: Provider, record: CreditRecord | None, commission_percent: int
) -> str:
    """The "who paid this" block of the reversal alert.

    Split out because it has four shapes and the alert body is already
    dense: resolved, resolved-and-we-knew-already, tombstoned, and
    unresolved. The unresolved wording distinguishes the two honest
    reasons — Stripe's id mismatch versus a payment this bot never
    credited — because they call for different next steps in the
    dashboard.

    #226: a tombstone must not be rendered with the credited shape.
    Its ``user_id`` and ``credited_amount`` are both zero by
    construction, and "Пользователь: 0 / Было начислено: 0 🪙" reads as
    a bug rather than as what it is — a reversal that arrived before
    its payment, now on record so the late ``paid`` cannot pay out.

    #1272: ``credited_amount`` is NOT the coin loss. The same top-up
    also minted the referral kickback and the developer cut
    (payments_service.py:324-333), and a reversal deliberately claws
    back neither, so the real hole is up to ``commission_percent``
    larger than the printed figure. The exact amounts are
    unrecoverable here — ``PaymentsService.last_commissions`` is
    per-event and already reset (payments_service.py:208), and
    ``CreditRecord`` carries no commission fields — so the line states
    the configured percents as an UPPER bound: the referral half is
    skipped when the buyer has no inviter (``NO_REFERRER``) and is
    zero when the programme is off (``DISABLED``).
    """
    if record is not None:
        lines = (
            (
                "Пользователь: <i>не определён</i> — начисления по этому "
                "идентификатору у бота нет: возврат опередил сам платёж. "
                "Бот записал возврат заранее, поэтому запоздавшее "
                "«оплачено» монет уже не выдаст. Если возврат отменят, "
                "начислите вручную.\n"
            )
            if record.is_tombstone
            else (
                f"Пользователь: <code>{record.user_id}</code>\n"
                f"Было начислено: <b>{record.credited_amount} {_COIN_EMOJI}</b> "
                f"({record.processed_at.strftime(_UTC_STAMP)})\n"
            )
        )
        if not record.is_tombstone and commission_percent > 0:
            lines += (
                f"Сверх этого выпущены комиссии — до {commission_percent}% "
                "суммы (реферальная и разработчику). Возврат их не "
                "отменяет, так что реальная потеря больше напечатанной.\n"
            )
        if record.reversed_at is not None:
            lines += (
                "⏱ Возврат по этому платежу уже отмечался "
                f"{record.reversed_at.strftime(_UTC_STAMP)} "
                f"(<code>{html.escape(_clamp(record.reversed_event or '?'))}</code>) "
                "— похоже на повторную доставку того же события.\n"
            )
        return lines
    if provider in _REVERSAL_ID_MATCHES_CREDIT:
        return (
            "Пользователь: <i>не определён</i> — начисления по этому "
            "идентификатору у бота нет. Скорее всего монеты и не выдавались, "
            "но проверьте вручную.\n"
        )
    return (
        "Пользователь: <i>не определён</i> — этот провайдер присылает "
        "возврат с другим идентификатором, чем зачисление. Найдите платёж "
        "в дашборде и сверьте с пополнениями вручную.\n"
    )


async def _alert_reversal(
    context: ReversalContext,
    provider: Provider,
    event_type: str,
    facts: _ReversalFacts,
    *,
    authenticated: bool = True,
) -> None:
    """Count a chargeback/refund and tell the owner about it.

    Best-effort throughout: this runs on a webhook that has already
    decided to answer 200, and a failure to deliver the DM must not turn
    that into a retry loop. The counter is bumped first for exactly that
    reason — it is the durable half of the signal, the DM is the
    convenient half.

    No wallet is touched. See :data:`PAYMENT_REVERSALS` for why the
    debit is a deliberate non-decision here.

    #174: before composing the message the credit is looked up by the
    provider's payment id and stamped reversed. The bot has always
    known which user this money belonged to — ``processed_webhooks``
    stores exactly that — and an alert that names an id but not a
    person leaves the owner to grep the dashboard for the one fact the
    decision hinges on.

    #188: ``authenticated`` says whether the body that triggered this
    was proven to come from the provider. Two routes prove it with an
    HMAC; the YooKassa one cannot, because YooKassa does not sign its
    notifications at all. An unauthenticated reversal is still told to
    the owner — suppressing it outright would let a real chargeback
    pass in silence, which is the failure this whole path exists to
    prevent — but it writes nothing and says on the card that it is
    unverified, so the owner checks the dashboard before believing it.

    #228: with one qualification. Because anyone can raise that card,
    the unauthenticated half is rationed by
    :func:`_unverified_alert_allowed`; past the hourly budget the DM
    is dropped and only the counter and a WARNING record the event.
    Authenticated reversals are never rationed — nothing an outsider
    sends can consume their slots, so there is no budget to exhaust.
    """
    PAYMENT_REVERSALS.labels(provider=provider.value, event=event_type).inc()
    if not authenticated and not _unverified_alert_allowed():
        # Everything past this point — a read of economy.db and a
        # Telegram send — is work an anonymous POST asked for. The
        # counter above already recorded that it happened.
        log.warning(
            "reversal alert suppressed: unverified budget spent "
            "provider={p} event={e} payment_id={pid}",
            p=provider.value,
            e=event_type,
            pid=facts.payment_id,
        )
        return
    title = _PROVIDER_TITLE.get(provider, provider.value)
    # #1272: an upper bound on what was minted on top of the credit.
    # Both cuts ride the same top-up transaction; neither is reversed.
    economy = context.settings.economy
    commission_percent = economy.referral_commission_percent + economy.developer_commission_percent
    record = await _resolve_reversal_credit(
        context, provider, event_type, facts.payment_id, stamp=authenticated
    )
    log.bind(
        provider=provider.value,
        event=event_type,
        payment_id=facts.payment_id,
        order_id=facts.ref,
        amount=facts.amount,
        user_id=record.user_id if record else None,
        credited_amount=record.credited_amount if record else None,
    ).error("payment_reversal")
    admin_id = context.settings.bot.admin_chat_id
    if not admin_id:
        return
    try:
        # Everything interpolated below is provider-controlled text
        # landing in an HTML-parse-mode message: an id containing "<"
        # would make Telegram reject the whole alert, which is exactly
        # the message we cannot afford to lose.
        await context.bot.send_message(
            admin_id,
            f"⚠️ <b>{html.escape(title)}: возврат средств</b>\n\n"
            f"Событие: <code>{html.escape(_clamp(event_type))}</code>\n"
            f"Платёж: <code>{html.escape(_clamp(facts.payment_id))}</code>\n"
            f"{html.escape(facts.ref_label)}: "
            f"<code>{html.escape(_clamp(facts.ref))}</code>\n"
            f"Сумма: <b>{html.escape(_clamp(facts.amount)) or '?'}</b>\n\n"
            f"{_reversal_user_lines(provider, record, commission_percent)}"
            f"{'' if authenticated else _UNVERIFIED_NOTE}"
            "\nМонеты НЕ списаны автоматически — "
            "проверьте баланс пользователя и решите, списывать ли.",
        )
    except Exception as exc:  # noqa: BLE001 — courtesy alert, log-only
        log.warning("reversal alert DM failed: {e}", e=exc)


@dataclass(frozen=True, slots=True)
class _HandlerReversalContext:
    """A :class:`ReversalContext` assembled from what a handler holds.

    Frozen because nothing here should be rebound mid-alert; the frozen
    dataclass's read-only attributes are what let it satisfy the
    protocol's read-only properties under mypy.
    """

    settings: Settings
    bot: Bot
    engines: EngineRegistry


async def alert_stars_refund(
    *,
    bot: Bot,
    settings: Settings,
    engines: EngineRegistry,
    refund: RefundedPayment,
) -> None:
    """Stamp a refunded Stars credit and tell the owner (#1987).

    The public door into the reversal machinery for the one provider
    that does not arrive as a webhook POST: Telegram delivers a Stars
    refund as an ordinary ``message.refunded_payment`` update, so
    ``handlers/topup.py`` calls this instead of a route calling
    :func:`_alert_reversal` directly.

    ``authenticated=True`` — and it is not a shrug. The update came
    over the same channel as every other update this bot acts on, and
    that channel is authenticated by the bot token; there is no weaker
    claim being made here than the HMAC routes make. So the stamp
    lands, which is the half that matters: ``reversed_at`` is what
    :meth:`~telegram_invite_bot.repositories.transactions_repo.TransactionsRepo.lifetime_deposits`
    subtracts, and until #1987 refunded Stars still counted as a
    deposit toward the withdrawal gate.

    Best-effort like the rest of the reversal path: nothing here is
    allowed to make an update fail. No wallet is touched — see
    :data:`PAYMENT_REVERSALS` for why the debit stays a human decision.
    """
    facts = _ReversalFacts(
        payment_id=refund.telegram_payment_charge_id,
        ref_label="Payload",
        ref=refund.invoice_payload or "?",
        amount=f"{refund.total_amount} {refund.currency}",
    )
    await _alert_reversal(
        _HandlerReversalContext(settings=settings, bot=bot, engines=engines),
        Provider.STARS,
        "refunded_payment",
        facts,
        authenticated=True,
    )


async def _alert_uncredited(
    application: Application,
    provider: Provider,
    facts: _ReversalFacts,
    *,
    cause: UncreditedCause | None = None,
    rationed: bool = False,
) -> None:
    """Count a paid-but-uncredited payment and tell the owner.

    Same best-effort shape as :func:`_alert_reversal` and for the same
    reason: the route has already decided to answer 200, so neither the
    counter nor the DM may turn a bookkeeping problem into a retry loop.

    The alert does name causes, and deliberately: each parser has a
    short, closed list of refusals on this path, and the owner's next
    move differs for each, so printing the shortlist is cheaper than
    sending them to the journal cold. What it does not do is claim
    which one fired. The parser already logged that, and a guess
    printed beside a real payment id would be believed.

    What the message has to carry regardless is the payment id,
    because that is what matches a row in the provider's dashboard
    against a user who is about to ask where their coins went.

    #1183: the causes and the journal prefix are per provider (see
    ``_UNCREDITED_CAUSES`` / ``_UNCREDITED_JOURNAL``) rather than
    hard-coded, because the crypto route's parser-refused branch
    reaches the same three shapes for entirely different reasons. The
    assembled RollyPay message is unchanged to the byte.

    #1643 adds two keyword-only halves, both defaulting to the old
    behaviour so that byte-for-byte promise still holds:

    ``cause`` — the refusal the parser actually took, when it was able
    to say. It replaces the "чаще всего" shortlist with the one line
    that is true, and only the YooKassa route can supply it, because
    only there does the parser learn the verdict from the provider
    rather than from an unsigned body.

    ``rationed`` — take a slot of :func:`_uncredited_alert_allowed`
    first. For a route whose card can be provoked repeatedly by a
    stranger; the counter and the log above still fire either way, so
    a silenced flood stays visible where it is measured.
    """
    PAYMENT_UNCREDITED.labels(provider=provider.value).inc()
    title = _PROVIDER_TITLE.get(provider, provider.value)
    verdict = _UNCREDITED_VERDICT.get(cause, "") if cause is not None else ""
    if verdict:
        cause_line = f": {verdict}"
    else:
        # Either the provider cannot name the cause, or a new
        # ``UncreditedCause`` member arrived without a line here. Both
        # fall back to the shortlist rather than to silence.
        causes = _UNCREDITED_CAUSES.get(provider, "")
        cause_line = f" (чаще всего: {causes})" if causes else ""
    proof = _UNCREDITED_PROOF.get(provider, _UNCREDITED_PROOF_DEFAULT)
    journal = _UNCREDITED_JOURNAL.get(provider, provider.value)
    log.bind(
        provider=provider.value,
        payment_id=facts.payment_id,
        order_id=facts.ref,
        amount=facts.amount,
    ).error("payment_paid_but_uncredited")
    if rationed and not _uncredited_alert_allowed():
        return
    admin_id = application.settings.bot.admin_chat_id
    if not admin_id:
        return
    try:
        await application.bot.send_message(
            admin_id,
            f"🚨 <b>{html.escape(title)}: оплата прошла, монеты НЕ зачислены</b>\n\n"
            f"Платёж: <code>{html.escape(_clamp(facts.payment_id))}</code>\n"
            f"{html.escape(facts.ref_label)}: "
            f"<code>{html.escape(_clamp(facts.ref))}</code>\n"
            f"Сумма: <b>{html.escape(_clamp(facts.amount)) or '?'}</b>\n\n"
            f"{html.escape(proof)} — но зачисление отклонено проверкой"
            f"{html.escape(cause_line)}. Точная причина — в журнале, строка "
            f"<code>{html.escape(journal)}:</code>.\n\n"
            "Деньги у вас, монет у человека нет: начислите вручную или верните платёж.",
        )
    except Exception as exc:  # noqa: BLE001 — courtesy alert, log-only
        log.warning("uncredited alert DM failed: {e}", e=exc)


async def _alert_credit_refused(
    application: Application,
    event: ParsedEvent,
    outcome: CreditOutcome,
) -> None:
    """#296: a verified payment the credit pipeline refused — tell the owner.

    This is the last of the three paid-but-empty-handed shapes to get a
    voice. :func:`_alert_uncredited` covers the events the *parser*
    refused, :func:`_alert_reversed_payment` the ones a refund reached
    first; this one covers the events that got all the way into
    :class:`PaymentsService` and came back ``CREDIT_REFUSED`` (the
    economy layer would not take the write — since #770 that means the
    balance ceiling, no longer a missing wallet) or ``INVALID_AMOUNT``
    (the coin count failed validation). Until now those bumped a
    counter and said nothing else.

    That silence was justified in writing —
    ``handlers/topup.py::_alert_stars_uncredited`` explained the webhook
    legs get by on "a counter and an ``ERROR`` line" because a non-2xx
    re-arms the provider's retry ladder — and neither half survives
    reading this module: there was no ``ERROR`` line on this path, and
    every caller of :func:`_credit_event` answers 200, so the ladder is
    spent rather than armed. What was actually left was a metric nobody
    watches minute to minute, against a customer who paid and is about
    to ask where their coins went.

    ``external_id`` is the value that matches a row in the provider's
    dashboard; ``user_id`` is who to credit by hand. ``external_id`` is
    escaped: it is provider-controlled text, the bot sends HTML, and
    one ``<`` would make Telegram reject the one message here that must
    not go missing. ``user_id`` is not escaped and does not need to be
    — :class:`ParsedEvent` types it ``int``, so it cannot carry a
    ``<``. That is the whole of its safety (#1473): the annotation, not
    an escape. Widen the field to a string one day and this line starts
    needing one.

    The payer is deliberately NOT DM'd. The Stars leg can reply because
    it is holding the buyer's own message; here the only handle on the
    buyer is a user id read out of provider metadata, and a bot opening
    a conversation off the back of that is a bigger surface than the
    problem needs. The owner has the id and can reach them.

    Best effort, after the counter and the log, for the usual reason:
    the route has already committed to answering 200, and a Telegram
    outage must not turn a bookkeeping problem into a retry loop.
    """
    title = _PROVIDER_TITLE.get(event.provider, event.provider.value)
    log.bind(
        provider=event.provider.value,
        payment_id=event.external_id,
        uid=event.user_id,
        coins=event.coins,
        reason=outcome.value,
    ).error("payment_paid_but_credit_refused")
    admin_id = application.settings.bot.admin_chat_id
    if not admin_id:
        return
    paid = ""
    if event.fiat_amount is not None:
        paid = f"{event.fiat_amount} {event.fiat_currency or ''}".strip()
    try:
        await application.bot.send_message(
            admin_id,
            f"🚨 <b>{html.escape(title)}: оплата прошла, монеты НЕ зачислены</b>\n\n"
            f"Платёж: <code>{html.escape(_clamp(event.external_id))}</code>\n"
            f"Пользователь: <code>{event.user_id}</code>\n"
            f"Оплачено: <b>{html.escape(_clamp(paid)) or '?'}</b> → "
            f"<b>{event.coins}</b> монет\n"
            f"Причина: <code>{html.escape(_clamp(outcome.value))}</code>\n\n"
            "Подпись верна, платёж настоящий, но зачисление отклонено уже на "
            "стороне бота: сумма не прошла проверку либо баланс упёрся в "
            "потолок. Провайдеру отвечено 200, повторной доставки не будет — "
            "начислите вручную или верните платёж.",
        )
    except Exception as exc:  # noqa: BLE001 — courtesy alert, log-only
        log.warning("credit-refused alert DM failed: {e}", e=exc)


async def _alert_reversed_payment(application: Application, event: ParsedEvent) -> None:
    """#226: a ``paid`` callback landed on an id we had already reversed.

    The refusal itself is correct and happens in
    :class:`PaymentsService` — the merchant has the money back, so the
    coins must not go out. What needs saying out loud is the honest
    cost of that decision: if the refund is later cancelled, this buyer
    paid and got nothing, and the only trace would be one ``INFO`` line
    that reads like an ordinary duplicate delivery.

    So it is counted on the same series as a paid-but-uncredited
    payment — from the owner's side that is exactly what it is — and
    DM'd with its own wording. Reusing :func:`_alert_uncredited` would
    have been cheaper and would have lied: that text blames the parser
    ("расчёт не в рублях, нет user_id в metadata"), and here the parser
    did its job perfectly.

    Best-effort throughout, like every alert on this route: the request
    is already answered 200 and a Telegram hiccup must not turn a
    bookkeeping note into a provider retry loop.
    """
    PAYMENT_UNCREDITED.labels(provider=event.provider.value).inc()
    title = _PROVIDER_TITLE.get(event.provider, event.provider.value)
    log.bind(
        provider=event.provider.value,
        payment_id=event.external_id,
        user_id=event.user_id,
        coins=event.coins,
    ).error("payment_paid_after_reversal")
    admin_id = application.settings.bot.admin_chat_id
    if not admin_id:
        return
    try:
        await application.bot.send_message(
            admin_id,
            f"🚨 <b>{html.escape(title)}: оплата по уже возвращённому платежу</b>\n\n"
            f"Платёж: <code>{html.escape(_clamp(event.external_id))}</code>\n"
            f"Пользователь: <code>{event.user_id}</code>\n"
            f"НЕ начислено: <b>{event.coins} {_COIN_EMOJI}</b>\n\n"
            "По этому платежу возврат пришёл раньше самой оплаты, бот его "
            "записал — поэтому монеты сейчас не выданы. Обычно это и значит, "
            "что деньги вернулись клиенту, и делать ничего не нужно.\n\n"
            f"Но если возврат отменён и деньги всё-таки у вас — начислите "
            f"{event.coins} {_COIN_EMOJI} вручную.",
        )
    except Exception as exc:  # noqa: BLE001 — courtesy alert, log-only
        log.warning("reversed-payment alert DM failed: {e}", e=exc)
