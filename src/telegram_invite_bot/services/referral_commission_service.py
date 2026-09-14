"""Referral purchase-commission payout (L-22 / L-32 finish).

What L-22/L-32 *really* are (verified against legacy)
-----------------------------------------------------
The backlog rows say "transfer fee … route to bot/referrer" which
reads as if the /send tax were split with the sender's inviter. The
legacy code says otherwise:

* **Transfers pay NO referrer share.** The legacy transfer path
  (``bot.py:10244-10295``) computes one tax cut
  (``COINS_TRANSFER_TAX`` with the VIP discount) and credits it 100%
  to ``ADMIN_CHAT_ID`` (``bot.py:10269-10273``); the only ledger rows
  are ``type='transfer'`` and ``type='tax'``. No referral lookup
  appears anywhere in that function. :class:`TransferService` is
  therefore already at full parity — adding an inviter split there
  would *invent* economics legacy never had.

* **The real referrer payout is purchase-side.** Legacy
  ``apply_purchase_commissions(buyer_id, coins_purchased)``
  (``bot.py:9900-9903``) runs ``_apply_referral_commission``
  (``bot.py:9856-9878``) after every *purchase*:

  - shop item bought with coins — ``buy_item``, ``bot.py:13243``
    (**no longer ported — dropped by T-020/R10**, see the caller
    contract below);
  - coin top-up via Telegram Stars — ``bot.py:18261``;
  - coin top-up via Crypto Pay — ``bot.py:18666``;
  - coin top-up via YooKassa — ``bot.py:18691``;
  - coin top-up via Stripe — ``bot.py:18706``.

  The inviter (``economy.users.referred_by``, written by the
  ``/start ref_<id>`` deep-link) receives
  ``commission_amount(coins, REFERRAL_COMMISSION_PERCENT)`` =
  ``max(1, int(coins * percent / 100))`` (``bot.py:9782-9786``),
  credited via ``add_coins(..., transaction_type="referral")``
  (``bot.py:9868``) — i.e. one wallet credit plus one ledger row
  ``type='referral', to_id=inviter``. That row type is exactly what
  ``/commission`` (``bot.py:25105``, ``get_referral_earnings`` at
  ``bot.py:9842-9852``) and the ported ``handlers/commission.py`` /
  ``handlers/referrals.py`` SUM over, so a credit landed through
  this service shows up in both reports with no further wiring.

This module is the aiogram-side port of that purchase-side rule.

Skip conditions (all legacy parity, ``bot.py:9856-9866``):

* commission percent <= 0 (feature disabled by admin),
* non-positive purchase amount (``commission_amount`` returns 0),
* buyer has no inviter (``referred_by`` NULL/0),
* inviter == buyer (self-referral guard).

Atomicity / failure posture
---------------------------
There is no debit anywhere in this flow — the commission is *minted*
to the inviter (legacy ``add_coins`` with ``from_id=0``), so the only
mutation pair is wallet-credit + ledger-row, both on the same
``economy`` session the caller owns. The credit return value is
CHECKED (same posture as the transfer-tax path after SEC-2): on
``None`` we log loudly and write NO ledger row, so the books can
never claim a payout that didn't land. The inviter wallet is
``get_or_create``'d first (legacy calls ``register_user`` inside
``add_coins``, ``bot.py:9724``), so a missing wallet row self-heals
instead of silently dropping the payout.

Caller contract (narrowed by T-020/R10)
---------------------------------------
``apply_purchase_commission`` must be invoked by every flow where a
purchase brings **real money into the ecosystem** — the four top-up
paths (Stars, Crypto Pay, YooKassa, Stripe), all of which run through
``PaymentsService``. There the mint is an acquisition cost: someone
paid, and the inviter who brought that customer is paid out of a real
inflow.

It must NOT be invoked for a **coin-paid shop buy**, even though
legacy did (``buy_item``, bot.py:13243) and #75 faithfully ported
that. A shop buy spends coins that already exist and burns them —
it is the bot's largest sink. Paying 10% to the inviter and 5% to the
developer handed 15% of every burn straight back as fresh supply, and
because ``purchase_commission_amount`` carries a ``max(1, …)`` floor,
a shop item priced at 1 coin burned 1 and minted 2 — a net
money-printer bounded only by how fast the item could be re-bought.
``PurchaseService`` therefore no longer holds a commission service at
all; the seam is gone rather than merely switched off, so it cannot
be re-wired by accident. See ``docs/ECONOMY_RATE_AUDIT.md`` §8.5.

The referrer DM legacy sends (``bot.py:9870-9876``) is rendered by
:func:`render_referral_commission_notice`; sending it (and resolving
the referrer's language from ``users.db``) stays with the caller,
because this service holds only the ``economy`` session and no Bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.economy import validate_credit_amount

_log = logger.bind(component="services.referral_commission")

if TYPE_CHECKING:
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.referrals_repo import ReferralsRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo

# Ledger discriminator both /commission and /referrals SUM over
# (legacy writer: bot.py:9868; readers: bot.py:9847, handlers/commission.py,
# repositories/referrals_repo.py ``_REFERRAL_TXN_TYPE``).
_REFERRAL_TXN_TYPE = "referral"

# Developer-commission ledger discriminator. Legacy lands these rows as
# ``type='system'`` because ``_apply_developer_commission`` calls
# ``add_coins`` with ``transaction_type=None, admin_id=None`` (bot.py:9893)
# and ``add_coins`` defaults that pair to "system" (bot.py:9742).
_DEVELOPER_TXN_TYPE = "system"

# Coin emoji for the referrer notice — same hardcode posture as
# ``payments_service._COIN_EMOJI`` (legacy ``COM_EMOJI``).
_COIN_EMOJI = "🪙"


def purchase_commission_amount(amount: int, percent: int) -> int:
    """Legacy ``commission_amount`` (bot.py:9782-9786), verbatim.

    ``max(1, int(amount * percent / 100))`` — note the 1-coin FLOOR:
    any commissionable purchase pays the inviter at least one coin,
    even when the percentage rounds to zero. Zero only when the
    amount or the percent is non-positive (feature off / nothing
    bought). Pinned by tests so a rewrite can't silently drop the
    floor.

    #1274: the FLOAT ``int(… / 100)`` here is the shipped behaviour
    on every production money path — this is the only commission
    helper with call sites. Its twin
    :func:`telegram_invite_bot.utils.economy.commission_amount`
    computes the same cut with exact ``// 100`` and calls the float
    form a precision bug; both statements are true and neither
    obliges the other to change. The two answers can only differ
    once ``amount * percent`` exceeds ``2**53``, which the coin caps
    make unreachable, so keeping legacy's arithmetic verbatim here
    costs nothing and keeps the parity claim in the first line
    literally checkable against ``bot.py:9782-9786``.
    """
    if amount <= 0 or percent <= 0:
        return 0
    return max(1, int(amount * percent / 100))


class CommissionOutcome(StrEnum):
    """Mutually-exclusive results — same StrEnum posture as
    :class:`TransferOutcome` so log lines render the name directly."""

    CREDITED = "credited"
    """Inviter wallet credited and the ``type='referral'`` ledger row
    written."""

    DISABLED = "disabled"
    """``referral_commission_percent <= 0`` — admin switched the
    programme off (legacy bot.py:9861)."""

    INVALID_AMOUNT = "invalid_amount"
    """Non-positive purchase amount — nothing to commission."""

    NO_REFERRER = "no_referrer"
    """Buyer joined organically (``referred_by`` NULL/0) or is their
    own referrer (legacy self-guard, bot.py:9864)."""

    CREDIT_FAILED = "credit_failed"
    """The wallet credit returned ``None`` (validation cap / balance
    cap). Logged loudly; NO ledger row is written so the books stay
    honest. The purchase itself is unaffected."""

    NO_RECIPIENT = "no_recipient"
    """Developer-commission only: no developer wallet configured
    (``ADMIN_CHAT_ID`` falsy) or the buyer IS the developer (legacy
    ``if not dev_id or dev_id == buyer_id``, bot.py:9888-9890)."""


@dataclass(frozen=True, slots=True)
class CommissionResult:
    """What one ``apply_purchase_commission`` call produced.

    ``referrer_id`` / ``commission`` are populated on CREDITED (and on
    CREDIT_FAILED, so ops can replay the exact missing payout from the
    log + result); zero/None on the skip outcomes.
    """

    outcome: CommissionOutcome
    referrer_id: int | None = None
    commission: int = 0
    percent: int = 0
    """The rate that produced ``commission``.

    No reader exists — grepped across ``src`` and ``tests`` and
    both are empty (#1626). Every percent a user actually sees is
    read straight from settings at the render site
    (``handlers/referral.py:99``, ``handlers/commission.py:108``),
    so the docstring standing here — «carried for the referrer-DM
    render» — named a consumer that has never existed.

    Kept rather than deleted because an outcome that reports an
    amount without the rate behind it cannot be replayed from a
    log line, and both halves now fill it: the developer half
    returned the default 0 while logging ``self._developer_percent``
    one line above, so the first caller to trust this field would
    have rendered "0%".
    """


@dataclass(frozen=True, slots=True)
class PurchaseCommissions:
    """Both halves of legacy ``apply_purchase_commissions`` (bot.py:9900-9903).

    ``referral`` is ``_apply_referral_commission`` (bot.py:9856-9878),
    ``developer`` is ``_apply_developer_commission`` (bot.py:9881-9897).
    Legacy always runs both, in that order, after every commissionable
    purchase — shop buy (bot.py:13243) AND all four top-up paths
    (Stars bot.py:18261, Crypto 18666, YooKassa 18691, Stripe 18706 —
    the same four this module's header lists at :26-29).
    """

    referral: CommissionResult
    developer: CommissionResult


class ReferralCommissionService:
    """Purchase-side referral + developer payouts over the shared economy session.

    ``developer_percent`` / ``developer_id`` default to 0 = developer
    commission disabled, so a constructor that omits them — tests, and
    any call site that predates the developer cut — keeps its exact
    behaviour instead of silently paying one.
    """

    def __init__(
        self,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        referrals_repo: ReferralsRepo,
        *,
        percent: int,
        developer_percent: int = 0,
        developer_id: int = 0,
    ) -> None:
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._referrals = referrals_repo
        self._percent = percent
        # Legacy DEVELOPER_COMMISSION_PERCENT (bot.py:3173, default 5)
        # and the recipient wallet ADMIN_CHAT_ID (bot.py:9888).
        self._developer_percent = developer_percent
        self._developer_id = developer_id

    async def apply_purchase_commission(
        self, *, buyer_id: int, coins_purchased: int
    ) -> CommissionResult:
        """Pay the buyer's inviter their cut of ``coins_purchased``.

        Steps mirror legacy ``_apply_referral_commission``
        (bot.py:9856-9878): config gate → amount gate → inviter
        lookup (with self-referral guard) → floor-respecting amount →
        checked credit → ledger row.
        """
        if self._percent <= 0:
            return CommissionResult(outcome=CommissionOutcome.DISABLED)
        if coins_purchased <= 0:
            return CommissionResult(outcome=CommissionOutcome.INVALID_AMOUNT)

        referrer_id = await self._referrals.fetch_inviter(buyer_id)
        if referrer_id is None or referrer_id == buyer_id:
            return CommissionResult(outcome=CommissionOutcome.NO_REFERRER)

        commission = purchase_commission_amount(coins_purchased, self._percent)
        if commission <= 0 or not validate_credit_amount(commission):
            # Defensive: percent>0 and coins>0 guarantee commission>=1,
            # so only the global credit cap can land here (a purchase
            # so large the cut itself exceeds the per-credit ceiling).
            _log.bind(buyer=buyer_id, referrer=referrer_id, commission=commission).error(
                "referral commission rejected by credit validation — not credited"
            )
            return CommissionResult(
                outcome=CommissionOutcome.CREDIT_FAILED,
                referrer_id=referrer_id,
                commission=commission,
            )

        # Self-heal a missing inviter wallet (legacy register_user inside
        # add_coins, bot.py:9724) before the checked credit — same M-E-2
        # posture as the transfer-tax treasury credit.
        await self._economy.get_or_create(referrer_id)
        credited = await self._economy.credit(referrer_id, commission)
        if credited is None:
            _log.bind(buyer=buyer_id, referrer=referrer_id, commission=commission).error(
                "referral commission credit failed (balance cap?) — not credited"
            )
            return CommissionResult(
                outcome=CommissionOutcome.CREDIT_FAILED,
                referrer_id=referrer_id,
                commission=commission,
            )

        # Ledger row AFTER the successful credit, never before — the
        # ``type='referral'`` stream is what /commission and /referrals
        # report as earnings, so a row without a matching wallet credit
        # would overstate what the inviter actually received.
        #
        # ``from_id=None`` because these coins are MINTED: the credit
        # above calls ``economy.credit``, nothing is taken from the
        # buyer's wallet. This row used to carry ``from_id=buyer_id`` on
        # the theory that "the readers filter on ``to_id`` + ``type``
        # only" — which is false. ``transactions_repo.window_stats``
        # sums ``ABS(amount) WHERE from_id == user`` with NO type
        # filter, and ``TransactionsRepo.recent`` renders any row whose
        # ``from_id`` is the viewer as a minus — see its ``signed``
        # binding, which flips the sign on exactly that test (#1476:
        # this used to cite a line range that had drifted onto
        # ``reversed_total``, a different function entirely). So every
        # real top-up grew the buyer's reported "spent" by the
        # commission he never paid. ``from_id=None`` is what every other
        # mint in the codebase writes — promo, roulette, rps, pvp, duel,
        # p2p, treasury, check, inventory_use, message_activity.
        # Traceability is not lost: the buyer's id is already in
        # ``reason``.
        await self._ledger.record(
            from_id=None,
            to_id=referrer_id,
            amount=commission,
            reason=f"referral_purchase_commission:{buyer_id}",
            type=_REFERRAL_TXN_TYPE,
        )

        _log.bind(
            buyer=buyer_id,
            referrer=referrer_id,
            commission=commission,
            percent=self._percent,
        ).info("referral purchase commission credited")
        return CommissionResult(
            outcome=CommissionOutcome.CREDITED,
            referrer_id=referrer_id,
            commission=commission,
            percent=self._percent,
        )

    async def apply_developer_commission(
        self, *, buyer_id: int, coins_purchased: int
    ) -> CommissionResult:
        """Credit the developer wallet their cut of ``coins_purchased``.

        Port of legacy ``_apply_developer_commission`` (bot.py:9881-9897):

        * gates: amount > 0 (bot.py:9883), percent > 0 (bot.py:9886),
          dev wallet configured and != buyer (bot.py:9888-9890);
        * amount: same ``commission_amount`` 1-coin-floor math as the
          referral half (bot.py:9891);
        * credit: ``add_coins(dev_id, …, admin_id=None,
          transaction_type=None)`` (bot.py:9893) — with both None the
          legacy ledger row lands as ``type='system'``
          (``transaction_type or ("admin_give" if admin_id else
          "system")``, bot.py:9742). We write the same ``'system'``
          type so legacy-era and new-era dev-commission rows aggregate
          identically in /transactions-style audits.

        Same checked-credit / ledger-after-credit posture as the
        referral half; no debit exists anywhere in the flow, so the
        only failure mode is a missed mint — never a stuck purchase.
        Unlike the referral half there is NO recipient DM: legacy only
        logs (bot.py:9895).
        """
        if self._developer_percent <= 0:
            return CommissionResult(outcome=CommissionOutcome.DISABLED)
        if coins_purchased <= 0:
            return CommissionResult(outcome=CommissionOutcome.INVALID_AMOUNT)
        dev_id = self._developer_id
        if dev_id <= 0 or dev_id == buyer_id:
            return CommissionResult(outcome=CommissionOutcome.NO_RECIPIENT)

        commission = purchase_commission_amount(coins_purchased, self._developer_percent)
        if commission <= 0 or not validate_credit_amount(commission):
            _log.bind(buyer=buyer_id, dev=dev_id, commission=commission).error(
                "developer commission rejected by credit validation — not credited"
            )
            return CommissionResult(
                outcome=CommissionOutcome.CREDIT_FAILED,
                referrer_id=dev_id,
                commission=commission,
            )

        # Self-heal a missing developer wallet (legacy register_user
        # inside add_coins, bot.py:9724) before the checked credit.
        await self._economy.get_or_create(dev_id)
        credited = await self._economy.credit(dev_id, commission)
        if credited is None:
            _log.bind(buyer=buyer_id, dev=dev_id, commission=commission).error(
                "developer commission credit failed (balance cap?) — not credited"
            )
            return CommissionResult(
                outcome=CommissionOutcome.CREDIT_FAILED,
                referrer_id=dev_id,
                commission=commission,
            )

        # Ledger row AFTER the successful credit. Reason matches legacy
        # byte-for-byte (bot.py:9893) so an admin audit spans the
        # cutover — legacy's rows carry this exact sentence and are
        # still in the ledger.
        # ``from_id=None`` for the same reason as the referral row above:
        # the coins are minted, and ``window_stats``/``recent`` DO read
        # ``from_id`` without a type filter. The buyer stays traceable
        # through ``reason``, which names him explicitly.
        #
        # That byte-for-byte requirement is also why #1547 localised this
        # row at the READER: ``profile._TX_PROSE_REASONS`` matches the
        # sentence whole and renders
        # ``h_profile_tx_developer_commission`` instead, leaving the
        # stored bytes — and the grep — untouched.
        await self._ledger.record(
            from_id=None,
            to_id=dev_id,
            amount=commission,
            reason=f"Комиссия с покупки монет (покупатель {buyer_id})",
            type=_DEVELOPER_TXN_TYPE,
        )

        _log.bind(
            buyer=buyer_id,
            dev=dev_id,
            commission=commission,
            percent=self._developer_percent,
        ).info("developer purchase commission credited")
        return CommissionResult(
            outcome=CommissionOutcome.CREDITED,
            referrer_id=dev_id,
            commission=commission,
            percent=self._developer_percent,
        )

    async def apply_purchase_commissions(
        self, *, buyer_id: int, coins_purchased: int
    ) -> PurchaseCommissions:
        """Run BOTH commission halves, referral first — legacy
        ``apply_purchase_commissions`` order (bot.py:9900-9903).

        Each half is independent in legacy (separate try/except around
        each, bot.py:9867-9878 / 9892-9897); here each half returns a
        typed outcome for the expected non-payout cases and only raises
        on genuinely unexpected DB errors, which the callers
        (:class:`PurchaseService`, ``PaymentsService``) catch so a
        commission bug can never void the buyer's purchase.
        """
        referral = await self.apply_purchase_commission(
            buyer_id=buyer_id, coins_purchased=coins_purchased
        )
        developer = await self.apply_developer_commission(
            buyer_id=buyer_id, coins_purchased=coins_purchased
        )
        return PurchaseCommissions(referral=referral, developer=developer)


def render_referral_commission_notice(lang: str, *, commission: int, percent: int) -> str:
    """The referrer's "you earned a cut" DM body (legacy bot.py:9870-9876).

    Rendered here so every caller (shop purchase, four payment
    webhooks) shows the identical card; *sending* it is the caller's
    job (best-effort, swallow-exceptions — a blocked DM must never
    roll back the credit, same posture as ``payments_service``).
    """
    return t(
        "h_referral_commission_credited",
        lang,
        amount=commission,
        sign=_COIN_EMOJI,
        percent=percent,
    )
