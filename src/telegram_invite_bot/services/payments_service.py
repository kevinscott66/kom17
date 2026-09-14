"""Payment-webhook orchestrator (T-025).

Composes the three pieces a verified payment event needs to land:

1. **Idempotency gate** — ``ProcessedWebhooksRepo.is_processed`` short-
   circuits before any wallet write. Duplicate deliveries return a
   :class:`CreditOutcome.IDEMPOTENT` and the router answers 200.

2. **Wallet credit** — ``EconomyService.credit`` adds coins and
   writes one ledger row inside the same SQLAlchemy transaction.
   Why not :class:`TransferService`: payment credits have no
   counter-party. See ADR 0014.

3. **Idempotency record** — ``ProcessedWebhooksRepo.mark_processed``
   appends the (provider, external_id) row inside the same
   transaction as the credit. Atomic: a flush error on either side
   rolls back both, and the provider's next retry reprocesses
   cleanly.

4. **User notification** — best-effort HTML DM ("balance topped up
   by N coins"), mirroring legacy. Failures are swallowed: a
   blocked-by-user DM must not roll back the credit.

The service is constructed once per webhook delivery from the router
(it holds a per-request session); tests build it directly with
mocked dependencies.

Legacy ``apply_purchase_commissions`` (bot.py:9900-9903) — the
referral kickback AND the developer commission — IS called here when
a :class:`ReferralCommissionService` is injected (#75), same-session
with the credit so all three wallet writes land in one transaction.
It runs inside a SAVEPOINT (R15): the kickback is a courtesy, the
top-up is a purchase, and the courtesy failing must not be able to
unwind the purchase. See the comment at the call site.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.repositories.processed_webhooks_repo import (
        ProcessedWebhooksRepo,
    )
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.services.economy_service import EconomyService
    from telegram_invite_bot.services.payments.base import ParsedEvent
    from telegram_invite_bot.services.referral_commission_service import (
        PurchaseCommissions,
        ReferralCommissionService,
    )

log = logger.bind(component="payments_service")

# Coin emoji rendered into the notification template (``balance_topup_ok``)
# at the ``{sign}`` placeholder. Mirrors legacy ``COM_EMOJI`` (the bot
# uses 🪙 when its token-emoji and TON-emoji would collide, which is
# the current state on prod). Hardcoded here rather than in a config
# because the legacy fallback is hardcoded — flipping it requires a
# coordinated rebrand and is out of scope for T-025.
_COIN_EMOJI = "🪙"


def _format_fiat(event: ParsedEvent) -> str:
    """Render the #239 charge for the success log line.

    Returns an em dash when the event carries no amount, so the field
    is present on every credited line regardless of provider. A
    consistently-shaped line is what makes ``grep 'payments:
    credited'`` a usable reconciliation tool; a field that sometimes
    vanishes turns every parse into a special case.

    The rate is appended only when there was one — Stripe and Crypto
    Pay price in the currency coins are already denominated in, and
    printing ``@None`` for them would suggest a missing value rather
    than an absent conversion.
    """
    if event.fiat_amount is None:
        return "—"
    text = f"{event.fiat_amount} {event.fiat_currency or '?'}"
    if event.fx_rate is not None:
        text = f"{text} @{event.fx_rate}"
    return text


class CreditOutcome(StrEnum):
    """High-level result of a webhook credit attempt.

    Routers map these to per-provider HTTP responses (200 in most
    cases — see the table in ADR 0014). Returning a typed outcome
    lets the router compose its response without inspecting service
    internals.
    """

    CREDITED = "credited"
    """The wallet was credited and the idempotency row written."""

    IDEMPOTENT = "idempotent"
    """A row in processed_webhooks already existed. No credit applied."""

    CREDIT_REFUSED = "credit_refused"
    """The economy layer would not take the write. Logged; 200 to ack.

    Until #770 this was ``USER_NOT_FOUND`` and it meant exactly that:
    the payer had no wallet row, so the UPDATE matched nothing and a
    settled payment came back a refusal. That cause is gone —
    :meth:`PaymentsService.handle_event` credits with
    ``ensure_wallet=True`` and seeds the row first. What survives is
    the balance ceiling (``balance + amount <= _MAX_AMOUNT``, enforced
    in SQL at economy_repo.py:305), which is why the name no longer
    mentions the user: whatever the cause, the money cleared and the
    coins did not move, and the router's job is the same either way.
    """

    INVALID_AMOUNT = "invalid_amount"
    """EconomyService rejected the credit (validation). Logged; 200."""

    REVERSED = "reversed"
    """#226: the row in processed_webhooks is a reversal tombstone.

    The refund for this payment reached us before the payment itself
    did — providers retry a ``paid`` callback for the best part of an
    hour, so a refund issued inside that hour can overtake the retry.
    No credit applied, and unlike IDEMPOTENT this is NOT healthy: the
    router alerts the owner, because if the refund is later cancelled
    the buyer paid and got nothing.
    """


class PaymentsService:
    """Provider-agnostic credit-on-success orchestrator."""

    def __init__(
        self,
        *,
        economy: EconomyService,
        idempotency: ProcessedWebhooksRepo,
        bot: Bot | None,
        user_settings: UserSettingsRepo | None = None,
        referral_commission: ReferralCommissionService | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Compose dependencies for one webhook delivery.

        ``bot`` is Optional: tests pass ``None`` to assert the credit
        path without an aiogram client. Prod passes the real Bot from
        :class:`Application` and the service uses it for the
        confirmation DM.

        ``user_settings`` is Optional for the same reason — the DM
        path looks up the user's language, but tests that don't
        exercise DMs can omit it.

        ``session`` is the economy session the caller's transaction is
        open on. It is Optional only because the commission is: R15
        runs the commission inside a SAVEPOINT on this session, so
        passing a commission without it is refused outright rather
        than silently degrading back to the unsafe shape.
        """
        if referral_commission is not None and session is None:
            raise ValueError(
                "PaymentsService: referral_commission requires session — the "
                "commission runs inside a SAVEPOINT so that a DB-level failure "
                "in the kickback cannot void the buyer's paid top-up (R15)."
            )
        self._economy = economy
        self._idempotency = idempotency
        self._bot = bot
        self._user_settings = user_settings
        # L-22/L-32 + #75: legacy apply_purchase_commissions runs on
        # every top-up (bot.py:18261/18666/18691/18706) and pays BOTH
        # the buyer's inviter and the developer wallet. Optional so
        # existing tests and providers that don't exercise the
        # commission path stay untouched; ``None`` = no payouts.
        self._referral_commission = referral_commission
        self._session = session
        # Outcome of the last handle_event's commission run — read by
        # the webhook router after commit to send the referrer's DM
        # (legacy bot.py:9870-9876) without widening CreditOutcome.
        self.last_commissions: PurchaseCommissions | None = None

    async def handle_event(self, event: ParsedEvent) -> CreditOutcome:
        """Process one already-verified, already-parsed event.

        Pipeline:
        1. Idempotency check on (provider, external_id).
        2. Credit via EconomyService.
        3. Record processed_webhooks row.
        4. Best-effort DM.

        Steps 1–3 run inside the caller's transaction (the router
        opens a session that wraps the whole thing). Step 4 happens
        AFTER the commit (caller-controlled) — a network failure
        sending the DM must not invalidate a successful credit.

        Returns a :class:`CreditOutcome` for the router to map to a
        status code.
        """
        self.last_commissions = None
        provider = event.provider.value
        if await self._idempotency.is_processed(provider=provider, external_id=event.external_id):
            # #226: a row exists, but "exists" now covers two facts.
            # Either we credited this payment before (a redelivery), or
            # a reversal for it arrived first and left a tombstone. The
            # second read is deliberately INSIDE this branch: the hot
            # path is the one that finds no row at all, and that path
            # is untouched — only deliveries the gate already stopped
            # pay for the extra lookup.
            existing = await self._idempotency.get(provider=provider, external_id=event.external_id)
            if existing is not None and existing.is_tombstone:
                log.warning(
                    "payments: {provider}:{eid} was reversed before it was paid "
                    "— NOT crediting {coins} coins to {uid}",
                    provider=provider,
                    eid=event.external_id,
                    coins=event.coins,
                    uid=event.user_id,
                )
                return CreditOutcome.REVERSED
            log.info(
                "payments: duplicate webhook {provider}:{eid} — skipping credit",
                provider=provider,
                eid=event.external_id,
            )
            return CreditOutcome.IDEMPOTENT

        wallet = await self._economy.credit(
            event.user_id,
            event.coins,
            type=f"purchase_{provider}",
            reason=event.reason,
            from_id=None,
            # #770: the buyer has already paid — a wallet row that was
            # never created (first-ever interaction is the purchase, or
            # a user who predates the economy tables) must not turn a
            # settled payment into "credit failed". Legacy self-healed
            # here via register_user inside add_coins (bot.py:9724), and
            # the commission cuts a few lines down already do the same
            # — ``apply_purchase_commission`` and
            # ``apply_developer_commission`` in
            # referral_commission_service.py both ``get_or_create``
            # ahead of their checked credit. This puts the payer on
            # equal footing with the people paid out of them.
            #
            # #1522: those two sites used to be cited as line
            # numbers (:279, :377). The second had drifted onto a
            # blank line, so a reader checking the #770 reasoning
            # landed on nothing. Symbols, not line numbers, per the
            # #1476 precedent.
            ensure_wallet=True,
        )
        if wallet is None:
            # Two sub-cases collapsed into one signal (the
            # EconomyService contract): validation rejected the
            # amount OR the economy layer refused the write. The
            # second used to be "no wallet row"; since #770 seeds one
            # above, what is left is the balance ceiling. Both are
            # operational failures the provider can't help with —
            # log and ack so they stop retrying. The operator
            # investigates from the log line.
            log.error(
                "payments: credit failed {provider}:{eid} uid={uid} coins={coins}",
                provider=provider,
                eid=event.external_id,
                uid=event.user_id,
                coins=event.coins,
            )
            # Discriminate by re-checking the validation (small,
            # synchronous) — lets the router log a more useful line
            # and keeps the two refusals apart in the metric.
            from telegram_invite_bot.utils.economy import validate_credit_amount

            if not validate_credit_amount(event.coins):
                return CreditOutcome.INVALID_AMOUNT
            return CreditOutcome.CREDIT_REFUSED

        await self._idempotency.mark_processed(
            provider=provider,
            external_id=event.external_id,
            user_id=event.user_id,
            credited_amount=event.coins,
            processed_at=datetime.now(UTC).replace(tzinfo=None),
            # #239: the audit trail rides along with the idempotency
            # row because that row is already the one durable record
            # of "this payment credited these coins to this user" —
            # giving the fiat figure a table of its own would create
            # a second thing that can be present when the first is
            # absent. Passed straight through: the service does not
            # read these, does not validate them, and no outcome
            # depends on them.
            fiat_amount=event.fiat_amount,
            fiat_currency=event.fiat_currency,
            fx_rate=event.fx_rate,
        )
        # L-22/L-32: pay the buyer's inviter their cut, in the SAME
        # transaction as the credit so the kickback and the top-up land
        # together. The service handles all expected non-payout outcomes
        # itself (no referrer / disabled / cap); the blanket except is
        # for the unexpected only — a kickback bug must never void a
        # customer's paid top-up.
        #
        # R15: that promise needs a SAVEPOINT to be true. Catching the
        # exception does not undo what it did to the transaction — a
        # failed flush deactivates the enclosing transaction, so the
        # caller's ``async with session.begin():`` then rolls the WHOLE
        # thing back on exit: credit, ledger row and idempotency row,
        # silently and without raising. We would still have returned
        # CREDITED and the router would still have answered 200, so the
        # provider never retries and the customer is simply out of
        # pocket. ``begin_nested`` confines the damage: the rollback
        # unwinds to the savepoint, the commission is dropped, and the
        # top-up survives to commit.
        commission = self._referral_commission
        session = self._session
        if commission is not None and session is not None:
            try:
                async with session.begin_nested():
                    self.last_commissions = await commission.apply_purchase_commissions(
                        buyer_id=event.user_id, coins_purchased=event.coins
                    )
            except Exception:  # noqa: BLE001 — kickback must not break the credit
                # Cleared explicitly: a commission that raised part-way
                # has been rolled back, so the router must not read a
                # half-populated result and DM someone about coins they
                # never received.
                self.last_commissions = None
                log.opt(exception=True).error(
                    "payments: referral commission failed for {provider}:{eid} — "
                    "rolled back to savepoint, top-up credit unaffected",
                    provider=provider,
                    eid=event.external_id,
                )
        # #239: the charge is on the success line too, not only in the
        # column. The journal is what an operator actually reads when
        # reconciling a provider report against a date, and it is the
        # only copy that survives if the row is ever lost.
        # ``fiat=—`` for an event that carried nothing.
        log.info(
            "payments: credited {provider}:{eid} uid={uid} +{coins} fiat={fiat}",
            provider=provider,
            eid=event.external_id,
            uid=event.user_id,
            coins=event.coins,
            fiat=_format_fiat(event),
        )
        return CreditOutcome.CREDITED

    async def notify_user(self, event: ParsedEvent) -> None:
        """Best-effort post-credit DM. Swallows exceptions.

        Called by the router AFTER the credit transaction has
        committed — a DM failure (blocked-by-user, network blip,
        Telegram outage) must not roll back the wallet write. Legacy
        does the same "try/except Exception: pass" dance around its
        ``bot.send_message`` call; preserved byte-identically.

        HTML parse mode is the global default for the new pipeline
        (see ``di/providers.py:AppProvider.bot``), so the template is
        rendered as HTML.
        """
        if self._bot is None:
            return
        lang = "ru"
        if self._user_settings is not None:
            try:
                lang = await self._user_settings.get_language(event.user_id) or "ru"
            except Exception as exc:
                log.warning(
                    "payments: lang lookup failed uid={uid}: {exc}",
                    uid=event.user_id,
                    exc=exc,
                )
        try:
            text = t("balance_topup_ok", lang, coins_amt=event.coins, sign=_COIN_EMOJI)
            await self._bot.send_message(event.user_id, text)
        except Exception as exc:
            # Match legacy: log + swallow. A blocked-by-user DM is
            # routine and should not page anyone.
            log.warning("payments: DM failed uid={uid}: {exc}", uid=event.user_id, exc=exc)
