"""``/send`` (transfer) flow composed over EconomyRepo + TransactionsRepo.

Closes the gap that :class:`EconomyService.transfer` leaves open:
the legacy /send (``bot.py:10235-10300``) is a *tax-aware* transfer
that splits the gross ``amount`` into a net to the recipient and a
cut to the admin wallet. ``EconomyService.transfer`` is the
tax-naive primitive (one ledger row, no admin cut); this service
wraps it with the tax math, the admin-destination credit and the
extra ledger row, so the upcoming ``/send`` handler arrives as
thin glue — same shape as ``/daily`` after Stages 8-13.

Why a separate service (not a method on EconomyService)
-------------------------------------------------------
EconomyService is the small-vocabulary primitive layer
(credit / debit / transfer / set_balance) — each method is one
wallet mutation + one ledger row. Putting the tax-aware orchestration
there would bloat its surface and tangle two abstraction levels.
TransferService is the *flow* — validation, tax computation,
multi-step orchestration, atomic rollback contract — built ON TOP
of the EconomyRepo primitives. Same separation that put DailyService
on top of EconomyService rather than as a method.

Atomicity contract
------------------
The caller wraps :meth:`send` in ``async with session.begin()``.
Without that wrapper, a recipient-credit failure after the sender
debit would leak money the same way legacy can if a process crash
lands between the two writes. The middleware that owns the session
(:class:`EconomyMiddleware`) is the natural site for the wrapper
once the handler lands.

The check-then-mutate pattern (read balance → validate → debit)
under the outer transaction is race-safe because :meth:`EconomyRepo.debit`
re-checks the balance in the WHERE clause (``balance >= amount``)
and returns None on rowcount=0. A concurrent debit that drains
the sender between our Python check and our SQL UPDATE collapses
to ``INSUFFICIENT_FUNDS`` rather than a phantom-debit visible to
the next read.

Tax destination
---------------
Legacy hardcodes the admin recipient as ``ADMIN_CHAT_ID``
(``bot.py:10269``). The new pipeline accepts it via
:attr:`TransferConfig.admin_user_id`; ``None`` means "burn the
tax" (debit the sender by the full gross, credit the recipient by
the net, never credit any admin). That posture is used in tests
to avoid seeding an admin wallet for every transfer assertion.

No referrer split on transfers (L-22 / L-32 verdict)
----------------------------------------------------
The lost-features backlog phrased L-22 as "deduct transfer fee,
route to bot/referrer", implying the sender's inviter receives a
share of the transfer tax. Verified against legacy: **false**. The
legacy transfer function (``bot.py:10244-10295``) credits the whole
tax cut to ``ADMIN_CHAT_ID`` (``bot.py:10269-10273``) and writes
only ``type='transfer'`` + ``type='tax'`` rows — no referral lookup,
no inviter credit, anywhere in the flow. The referrer payout legacy
DOES have is *purchase-side*: ``apply_purchase_commissions``
(``bot.py:9900``) after shop buys (``bot.py:13243``) and coin
top-ups (Stars ``18261``, Crypto Pay ``18666``, YooKassa ``18691``,
Stripe ``18706``). That rule is ported as
:class:`~telegram_invite_bot.services.referral_commission_service.ReferralCommissionService`;
this service stays referral-free on purpose — adding a split here
would invent economics legacy never had.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.services.effects_service import TransferEffects
from telegram_invite_bot.utils.transfer import compute_transfer_tax

_log = logger.bind(component="services.transfer")

if TYPE_CHECKING:
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


class TransferOutcome(StrEnum):
    """Mutually-exclusive results a handler branches on.

    StrEnum so log lines render the name directly and a future
    external audit log can store the outcome as plain text — same
    posture as :class:`ClaimOutcome` in DailyService.
    """

    SUCCESS = "success"
    """Sender debited, recipient credited, admin (if configured)
    credited, ledger rows written."""

    SELF_TRANSFER = "self_transfer"
    """``from_id == to_id``. Legacy quietly rejects with a "can't
    gift yourself" message; we surface it as a distinct outcome so
    metrics distinguish typo (rare) from real intent (very rare)."""

    INVALID_AMOUNT = "invalid_amount"
    """Non-positive amount. Defensive — the handler validator
    should reject before we get here, but surfacing the outcome
    keeps the service safe under direct service-level calls (tests,
    future internal flows)."""

    NO_SENDER_WALLET = "no_sender_wallet"
    """Sender has no wallet row. Rare — handler typically calls
    ``get_or_create`` first — but possible if the wallet was wiped
    by an admin /reset between the handler entry and this call."""

    NO_RECIPIENT_WALLET = "no_recipient_wallet"
    """Recipient lookup failed. Handler is responsible for
    resolving username → user_id; this outcome means the user_id
    resolved but no wallet exists (the recipient never interacted
    with the bot)."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    """Sender's balance is below the gross ``amount``. Surfaces
    both at the Python pre-check and at the SQL race guard (a
    concurrent debit collapses to the same outcome — same UX
    either way)."""


@dataclass(frozen=True, slots=True)
class TransferResult:
    """What a /send call produced. Read ``outcome`` first.

    Mirrors :class:`ClaimResult` shape: a tagged-union dataclass
    keeps the success-path fields (``amount`` / ``received`` /
    ``tax`` / ``sender_balance``) zero on the failure paths so the
    caller can render a uniform receipt template without per-field
    ``if outcome is SUCCESS`` guards.
    """

    outcome: TransferOutcome
    amount: int = 0
    """Gross debit from the sender on SUCCESS; 0 otherwise."""
    received: int = 0
    """Net credit to the recipient on SUCCESS; 0 otherwise.
    Always ``amount - tax``."""
    tax: int = 0
    """Tax cut moved to the admin wallet (or burned) on SUCCESS."""
    sender_balance: int = 0
    """Sender's post-transfer balance on SUCCESS — rendered in the
    receipt directly. Re-reading via a fresh ``get()`` would race
    with a concurrent credit; returning it from the same write
    that produced it pins the value."""


@dataclass(frozen=True, slots=True)
class TransferConfig:
    """Numeric knobs + admin-destination wiring.

    Both fields are threaded from :class:`EconomyMiddleware`, which
    reads them off ``Settings`` (#193). The defaults below are the
    legacy ones, and they are what a caller that omits the config
    gets:

    * ``base_tax_rate`` ← ``COINS_TRANSFER_TAX``. The legacy default
      is **0** — ``"coins_transfer_tax": 0`` at bot.py:2550, no tax
      at all — and the operator's own ``settings.json`` carries that
      0 too. This used to default to ``0.05`` under a docstring
      asserting 0.05 *was* the legacy default; it never was, so every
      ported /send charged a 5% tax legacy did not (#193).
    * ``admin_user_id`` ← ``ADMIN_CHAT_ID`` in production; legacy
      credits the cut to that wallet at bot.py:10270-10273. ``None``
      means "burn the cut" — the ledger says so out loud with
      ``reason="transfer_tax_burned"`` — and tests leave it None to
      exercise the burn path cleanly.

    Passed in (not module-level) so a test can pin a specific rate
    without touching global state, and so an operator "change tax to
    0.02" is a ``COINS_TRANSFER_TAX`` env edit, not a code change.
    """

    base_tax_rate: float = 0.0
    admin_user_id: int | None = None


class TransferService:
    """Atomic tax-aware /send execution against the shared session."""

    def __init__(
        self,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        *,
        config: TransferConfig | None = None,
    ) -> None:
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._config = config or TransferConfig()

    async def send(
        self,
        *,
        from_id: int,
        to_id: int,
        amount: int,
        effects: TransferEffects | None = None,
    ) -> TransferResult:
        """Run the /send flow.

        Steps, in order (the order matters):

        1. Cheap validators (self-transfer, non-positive amount) —
           short-circuit before any DB I/O.
        2. Sender read + balance pre-check — surfaces
           ``NO_SENDER_WALLET`` and ``INSUFFICIENT_FUNDS`` without
           taking a row lock on the recipient first.
        3. Recipient read — surfaces ``NO_RECIPIENT_WALLET`` before
           we mutate the sender, so an unknown recipient doesn't
           require a rollback.
        4. Tax computation via the pure helper (no DB).
        5. Sender debit by GROSS amount — race-safe via
           ``balance >= amount`` SQL guard. Re-check failure here
           means a concurrent debit drained the sender between
           step 2 and step 5; same outcome as a Python pre-check
           rejection, surfaced as ``INSUFFICIENT_FUNDS``.
        6. Recipient credit by NET. If the recipient wallet
           vanished mid-flow (admin /reset), the outer rollback
           restores the sender — we return ``NO_RECIPIENT_WALLET``
           but the user sees no money loss.
        7. Admin credit by tax (if configured AND tax > 0). The
           admin wallet is ``get_or_create``'d first so a missing
           treasury row self-heals rather than silently burning the
           cut (M-E-2 in audits/01_economy.md). Pre-M-E-2 behaviour
           dropped the tax on the floor whenever the operator
           forgot to seed the admin row.
        8. Ledger writes: one row for the main transfer (records
           the NET amount — the coins that actually reached the
           recipient at step 6, matching legacy bot.py:10283 — under
           ``type='transfer'``), one for the tax
           cut (``type='tax'``) if non-zero. Two rows because the
           admin treasury audit query joins on ``type='tax'``;
           tucking the tax inside the main row would require every
           reader to re-derive ``amount * effective_rate`` to find
           the cut.

        Self-transfer rejection (step 1) is deliberately cheaper
        than legacy's "validate first, then check wallet": a /send
        of N coins to oneself with ``N > balance`` returns
        ``SELF_TRANSFER`` here but ``INSUFFICIENT_FUNDS`` in legacy.
        Both are valid rejections; we report the more meaningful
        one because self-transfer is a *typo* the user can fix,
        balance is a *constraint* they can't.
        """
        effects = effects or TransferEffects()
        cfg = self._config

        # Step 1: cheap validators.
        if from_id == to_id:
            return TransferResult(outcome=TransferOutcome.SELF_TRANSFER)
        if amount <= 0:
            return TransferResult(outcome=TransferOutcome.INVALID_AMOUNT)

        # Step 2: sender read + balance pre-check.
        sender = await self._economy.get(from_id)
        if sender is None:
            return TransferResult(outcome=TransferOutcome.NO_SENDER_WALLET)
        if sender.balance < amount:
            return TransferResult(outcome=TransferOutcome.INSUFFICIENT_FUNDS)

        # Step 3: recipient existence check (before we mutate sender).
        recipient = await self._economy.get(to_id)
        if recipient is None:
            return TransferResult(outcome=TransferOutcome.NO_RECIPIENT_WALLET)

        # Step 4: tax math.
        tax = compute_transfer_tax(
            amount,
            base_rate=cfg.base_tax_rate,
            discount_percent=effects.tax_discount_percent,
        )
        net = amount - tax

        # Step 5/6: race-safe sender debit + recipient credit, wrapped
        # in a SAVEPOINT so a post-debit credit failure (recipient
        # wallet vanished between step 3 and step 6) reverts the
        # sender's debit atomically without depending on the outer
        # middleware's exception-driven rollback.
        #
        # R-FIX-002-fp: ``session.begin_nested()`` relies on SQLAlchemy
        # 2.x's *autobegin* — the AsyncSession must have an outer
        # transaction implicitly opened on first SQL emission so the
        # nested call lands a real SAVEPOINT (and not just a no-op
        # against a session that has never seen a BEGIN). Every caller
        # path into this service runs under
        # :class:`BaseSessionMiddleware`, which constructs the
        # AsyncSession with the default ``autobegin=True``; if a future
        # caller passes a session built with ``autobegin=False`` the
        # SAVEPOINT will silently degrade to a flush and the
        # debit-without-credit window re-emerges. The test
        # ``test_transfer_uses_savepoint_for_atomicity`` in
        # ``tests/integration/services/test_transfer_service.py`` (line
        # 419) pins this by monkey-patching
        # :meth:`AsyncSession.begin_nested` and asserting it's actually
        # called. The path used to read ``tests/unit/services/`` — a
        # file that does not exist, so the one pin standing between
        # this service and a silent regression looked absent (#1520).
        #
        # R-FIX-002: previously the credit-failure branch returned
        # ``NO_RECIPIENT_WALLET`` *normally* after the sender debit
        # had been issued — :class:`BaseSessionMiddleware` only rolls
        # back on raised exceptions, so the sender's debit was
        # committed and coins evaporated. Using ``session.begin_nested``
        # gives us a savepoint that rolls back only the inner pair on
        # credit failure while leaving the ambient transaction state
        # intact (the middleware's outer commit still fires for any
        # other writes in the same session).
        session = self._economy._session  # noqa: SLF001 — same-package use
        async with session.begin_nested() as savepoint:
            updated_sender = await self._economy.debit(from_id, amount)
            if updated_sender is None:
                # No need to roll back the savepoint explicitly — the
                # debit didn't mutate any row (UPDATE matched 0 rows),
                # but exiting via raise/return inside the ctx still
                # commits the empty savepoint. We just bail.
                return TransferResult(outcome=TransferOutcome.INSUFFICIENT_FUNDS)

            credited = await self._economy.credit(to_id, net)
            if credited is None:
                # Recipient wallet vanished mid-flow → roll the
                # savepoint back so the sender's debit is undone, then
                # return ``NO_RECIPIENT_WALLET`` to the caller. Sender
                # sees no money loss; the outer transaction can still
                # commit any other unrelated work.
                await savepoint.rollback()
                return TransferResult(outcome=TransferOutcome.NO_RECIPIENT_WALLET)

        # Step 7: tax destination (if configured AND > 0). ``tax_payee``
        # is who the ledger row will name — it drops to ``None``
        # (= burned by the system) if the treasury credit doesn't land.
        tax_payee = cfg.admin_user_id
        if tax > 0 and cfg.admin_user_id is not None:
            # M-E-2: when an admin destination is configured but the
            # admin wallet row is missing, a plain ``credit`` returns
            # ``None`` (rowcount=0) and the tax silently evaporates —
            # the sender is debited gross, the recipient is credited
            # net, and the difference goes nowhere. ``type='tax'`` is
            # still written to the ledger, so the books look balanced
            # while the treasury wallet never receives the coins.
            # Audit 01_economy.md M-E-2 explicitly calls this out:
            # "in production an admin wallet that should exist but is
            # missing is exactly the case we want to alarm on".
            #
            # Fix: ``get_or_create`` the admin wallet idempotently
            # before the credit, so a missing admin row self-heals
            # rather than burning revenue. The op is cheap (one
            # ``EXISTS`` probe; an UPSERT only on first ever access)
            # and inherits the row-level race safety the helper
            # already provides for user wallets.
            #
            # #1521: the credit lands through the escrow primitive,
            # not through ``credit``. Both write the same
            # ``balance + amount`` under the same ceiling guard, but
            # ``credit`` also bumps ``total_earned`` — and legacy's
            # tax leg is a bare ``UPDATE users SET balance =
            # balance + ?`` (bot.py:10268-10273) while its sender and
            # recipient legs do bump the counters. Tax is revenue the
            # treasury collects, not coins it earned in the game, so
            # the #238 balance-only primitive is the right one. The
            # drift would have been monotonic and unrepairable:
            # ``bump_totals`` refuses negative arguments
            # (economy_repo.py:514), so an inflated ``total_earned``
            # can never be walked back.
            await self._economy.get_or_create(cfg.admin_user_id)
            taxed = await self._economy.release(cfg.admin_user_id, tax)
            if taxed is None:
                # SEC: after get_or_create the admin wallet exists, so the
                # only remaining failure is a balance-cap overflow on the
                # treasury. Don't let it pass silently — the sender was
                # debited gross and the tax ledger row below would imply
                # the treasury received coins it didn't. Log loudly for
                # ops; the rare overflow needs a manual treasury sweep.
                _log.bind(admin_id=cfg.admin_user_id, tax=tax).error(
                    "transfer tax credit failed (treasury balance cap?) — tax not credited"
                )
                # Every other service in this package writes its ledger
                # row only AFTER the credit lands, precisely so a sum
                # over the rows equals the coins that actually moved.
                # Naming the treasury here would break that: the tax
                # stream is what the treasury audit query totals, and
                # it would report income the wallet never received. The
                # coins did leave the sender, so the row still gets
                # written — as a burn (``to_id=None``), which is what
                # actually happened.
                tax_payee = None

        # Step 8: ledger rows.
        #
        # The main row records the NET amount — what actually landed in
        # the recipient's wallet at step 6 — matching legacy, which
        # inserts ``final_amount`` and not the gross (bot.py:10283,
        # the same variable it credits at bot.py:10265).
        #
        # This used to store the gross, justified as "the ledger sums
        # match the user-visible 'you sent N' text". That justification
        # was false twice over. The receipt is rendered from
        # :class:`TransferResult` (the ``h_send_success`` render in
        # ``handlers/send.handle_send`` reads ``result.amount`` /
        # ``.received`` / ``.tax``), never from the ledger, so no
        # rendering depended on it. And the gross made both sides of
        # the ledger lie: the recipient's history showed income of
        # ``amount`` when ``net`` arrived, and the sender's two rows
        # summed to ``gross + tax`` — the tax counted twice — because
        # :meth:`TransactionsRepo.recent` derives the sign from
        # ``to_id == user_id`` and takes the magnitude as given, as
        # does ``TransactionsRepo.window_stats``. With the net, the
        # pair sums to exactly the coins that left the sender.
        #
        # The tax row is keyed on ``type='tax'`` for the treasury audit
        # query; legacy writes that row at bot.py:10288-10293.
        # Deliberate divergence: legacy stores ``-tax`` (bot.py:10292)
        # while this row stores ``+tax``. The port's convention is
        # magnitude-plus-direction (the "Sign convention" section of
        # the ``transactions_repo`` module docstring), so a negative
        # magnitude here would be read back inverted by the very query
        # the row exists for.
        await self._ledger.record(
            from_id=from_id,
            to_id=to_id,
            amount=net,
            reason="transfer",
            type="transfer",
        )
        if tax > 0:
            await self._ledger.record(
                from_id=from_id,
                to_id=tax_payee,
                amount=tax,
                reason="transfer_tax" if tax_payee is not None else "transfer_tax_burned",
                type="tax",
            )

        return TransferResult(
            outcome=TransferOutcome.SUCCESS,
            amount=amount,
            received=net,
            tax=tax,
            sender_balance=updated_sender.balance,
        )
