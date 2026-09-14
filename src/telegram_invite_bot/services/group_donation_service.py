"""Donations to a group: a slice of a purchase (RR-2 #14), or ``/donate``.

Two entry points, one money path
--------------------------------
:meth:`GroupDonationService.route` is the purchase slice described
below. :meth:`GroupDonationService.donate` is #2007's ``/donate`` — a
member paying the group out of their own wallet. They differ in exactly
two places: where the coins come from (minted-from-a-purchase vs debited
from the donor) and whether the operator's ``percent`` knob gates them
(it gates the slice; it must never gate a donation the user typed the
amount of). Everything downstream — the ``donations`` row, the
``group_top_donators`` counter, the ``group_xp`` bump, the owner payout
minus the developer cut, the recalc and today's snapshot — is the same
code, because legacy's two writers (``donation_from_purchase``
bot.py:10715, ``donations_do_donate`` bot.py:10643) were already the
same four statements copied twice.

What legacy actually does
-------------------------
When ``/shop`` is used with a group selected, legacy's ``buy_item``
adds one extra step after the item is delivered (bot.py:13240-13242)::

    if group_id and PURCHASE_DONATION_TO_GROUP_PERCENT > 0:
        donation_amount = commission_amount(item.price, PURCHASE_DONATION_TO_GROUP_PERCENT)
        donation_from_purchase(group_id, user_id, donation_amount, "С покупки в группе")

``donation_from_purchase`` (bot.py:10715) then does two very different
things, and the distinction matters because the backlog wording ("% of
price to the group treasury") describes neither of them precisely:

* **Rating points, not treasury.** The full slice lands as
  ``groups_donations.group_xp`` — the "Гром/Thunder" score ``/rating``
  ranks groups by. It does NOT touch ``total_donations`` (frozen in
  legacy, bot.py:10915 «Сейчас не пополняется») and it does NOT touch
  the ``group_treasury`` table ``/group_pay`` withdraws from. Same
  TRUTH-RULE already recorded in ``services/treasury_service.py``.
* **Coins to the group's creator.** The slice minus the developer
  commission is *minted* to ``get_chat_creator_id(group_id)`` as
  ``type='donate_to_owner'``; the fee goes to ``ADMIN_CHAT_ID`` as
  ``type='donate_commission'``. If no creator can be resolved, legacy
  credits nobody (``if owner_id:``) but still keeps the xp.

So the user-facing promise is "part of the price goes to the group" —
mechanically: the group climbs the leaderboard by the full slice, and
its owner is paid ~85% of that slice in coins.

Why the creator lookup is NOT done here
---------------------------------------
Legacy resolves the owner mid-function with a blocking Telegram API
call (``get_chat_creator_id``) while holding an open SQLite write
transaction. This port takes ``owner_id`` as an argument instead: the
handler resolves it via :func:`utils.telegram_admin.chat_creator_id`
*before* the money path, so no network round-trip is ever held inside
a write transaction (the same rule the rest of the pipeline follows —
SQLite writers are serialised process-wide, so a slow API call inside
one is a global stall). ``None`` reproduces legacy's "xp only, no
payout" branch exactly.

The owner payout is a MINT, not a transfer
------------------------------------------
The buyer's coins are burned by :class:`PurchaseService`; the owner's
share is created fresh (legacy ``add_coins``, bot.py:10751 — same).
A buyer who is also the group's creator therefore gets a partial rebate
on their own purchase (``percent`` minus the developer cut, ~14% at the
default knobs) — they are still down ~86% of the price, so this is a
loyalty discount, not a coin printer, and it is exactly what legacy
paid. The ``le=100`` bound on
``EconomyConfig.purchase_donation_to_group_percent`` is what keeps it
that way — a rebate can never exceed the price it came from, so
self-purchases can't be turned into a coin printer by a config typo.

Atomicity / failure posture
---------------------------
Every write here — the donation rows, the xp bump, both credits and
both ledger rows — runs on the shared ``economy`` session the caller
owns, so they commit with the purchase or roll back with it. There is
no debit in the flow (the buyer was already charged by
:class:`PurchaseService`); the coins are minted, so the only failure
mode is a missed payout, never a stuck purchase. Credits are CHECKED
(SEC-2 posture): ``get_or_create`` self-heals a missing wallet, the
``credit()`` return is tested for ``None``, and the ledger row is
written only after a credit actually landed — the books can never
claim a payout that didn't happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.services.referral_commission_service import (
    purchase_commission_amount,
)
from telegram_invite_bot.utils.economy import validate_credit_amount
from telegram_invite_bot.utils.time import rating_history_date

_log = logger.bind(component="services.group_donation")

if TYPE_CHECKING:
    from telegram_invite_bot.repositories.donations_rating_repo import (
        DonationsRatingRepo,
    )
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo

# Ledger discriminators, byte-identical to legacy (bot.py:10751, 10753)
# so a donation audit spans the cutover: legacy's rows are still in the
# ledger under exactly these strings, and a rename here would split the
# history in two rather than migrate it.
_OWNER_TXN_TYPE = "donate_to_owner"
_FEE_TXN_TYPE = "donate_commission"
# The donor's own side of a ``/donate`` (bot.py:10696). Only ``donate``
# has one: a purchase slice is minted, so there is nobody to debit.
_DONOR_TXN_TYPE = "donate"

# Legacy default message on the ``donations`` row (bot.py:13242).
_PURCHASE_DONATION_MESSAGE = "С покупки в группе"


class GroupDonationOutcome(StrEnum):
    """Mutually-exclusive results of one routing attempt."""

    ROUTED = "routed"
    """Group xp credited AND the owner's coins actually landed."""

    ROUTED_NO_OWNER = "routed_no_owner"
    """Group xp credited, but nobody was paid: no chat creator could be
    resolved (legacy's ``if owner_id:`` skip, bot.py:10747), the split
    left the owner 0 coins, or the credit itself was refused. The group
    still climbs the leaderboard — the xp is never conditional on the
    payout succeeding."""

    DISABLED = "disabled"
    """``purchase_donation_to_group_percent <= 0`` — group routing switched
    off by the operator (legacy gate, bot.py:13240)."""

    NO_GROUP = "no_group"
    """A global (no-group) purchase — nothing to route."""

    INVALID_AMOUNT = "invalid_amount"
    """Non-positive price, or a slice that fails the credit validator."""


@dataclass(frozen=True, slots=True)
class GroupDonationResult:
    """What one :meth:`GroupDonationService.route` call produced.

    ``amount`` is the full slice added to the group's ``group_xp``;
    ``to_owner`` + ``fee`` are the coin split of that same slice.
    ``owner_credited`` is ``False`` when the credit itself failed
    (validation / balance cap) — the xp still landed, so the receipt must
    not promise coins that never arrived.

    Only ``to_owner`` is zeroed in that case. ``fee`` stays the COMPUTED
    figure and does NOT mean money was withheld: it is what the split
    would have been, and it is what the structured log line below
    records. A receipt renderer must therefore gate the commission row on
    ``owner_credited``, not on ``fee`` being non-zero.

    #1401 widened that gap rather than closing it: a fee whose payout
    failed is no longer credited to the developer either, so on
    ``owner_credited is False`` with ``to_owner > 0`` this field is the
    only trace that a split was ever computed.
    """

    outcome: GroupDonationOutcome
    group_id: int = 0
    amount: int = 0
    to_owner: int = 0
    fee: int = 0
    owner_id: int | None = None
    owner_credited: bool = False
    percent: int = 0


class DonateOutcome(StrEnum):
    """Mutually-exclusive results of one ``/donate`` (#2007)."""

    DONATED = "donated"
    """Donor debited, group xp credited AND the owner's coins landed."""

    DONATED_NO_OWNER = "donated_no_owner"
    """Donor debited and the group ranked, but nobody was paid — no chat
    creator resolved, or the credit itself was refused. Same posture as
    :attr:`GroupDonationOutcome.ROUTED_NO_OWNER`: the xp is never
    conditional on the payout, and legacy behaved identically
    (``if owner_id:``, bot.py:10747)."""

    INSUFFICIENT = "insufficient"
    """The donor cannot cover the sum. Nothing was written; ``balance``
    carries what they actually have, so the refusal can say so."""

    INVALID_AMOUNT = "invalid_amount"
    """Non-positive, or a sum that fails the credit validator. The
    handler's own min/max gates catch this first — this is the service
    refusing to trust them."""


@dataclass(frozen=True, slots=True)
class DonateResult:
    """What one :meth:`GroupDonationService.donate` call produced.

    ``balance`` is the donor's balance AFTER the debit on success, and
    their untouched balance on :attr:`DonateOutcome.INSUFFICIENT` — in
    both cases "what they have now", which is the only figure a receipt
    or a refusal ever wants to print.

    ``group_xp`` is the group's score after the bump, read back inside
    the same transaction. It is deliberately NOT ``total_donations``:
    legacy's own success card printed that column (bot.py:24906) and it
    has not moved since before the cutover (bot.py:10915 «Сейчас не
    пополняется»), so every legacy receipt quoted a frozen number while
    the board it was congratulating the user about ranked on another.

    ``to_owner`` / ``owner_credited`` follow the same contract as
    :class:`GroupDonationResult`: ``to_owner`` is zeroed when the payout
    did not land, ``fee`` stays the computed figure.
    """

    outcome: DonateOutcome
    group_id: int = 0
    amount: int = 0
    to_owner: int = 0
    fee: int = 0
    owner_id: int | None = None
    owner_credited: bool = False
    balance: int = 0
    group_xp: int = 0


class _DebitRefused(Exception):  # noqa: N818 — control flow, never surfaced
    """The donor's debit did not land, so the SAVEPOINT must unwind.

    Raised and caught inside :meth:`GroupDonationService.donate` only.
    An ordinary ``return`` cannot do this job: the debit happens inside
    ``session.begin_nested()``, and returning out of that block exits
    the context manager through its success path, which RELEASEs the
    savepoint instead of rolling it back.
    """


class GroupDonationService:
    """Purchase → group-rating + owner-payout routing over the shared session."""

    def __init__(
        self,
        donations_rating_repo: DonationsRatingRepo,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        *,
        percent: int,
        developer_percent: int = 0,
        developer_id: int = 0,
    ) -> None:
        self._donations = donations_rating_repo
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._percent = percent
        self._developer_percent = developer_percent
        self._developer_id = developer_id

    @property
    def percent(self) -> int:
        """The configured slice, for handlers that advertise it up front."""
        return self._percent

    async def route(
        self,
        *,
        group_id: int,
        user_id: int,
        price: int,
        owner_id: int | None,
    ) -> GroupDonationResult:
        """Route ``percent`` of ``price`` to ``group_id``.

        Order mirrors legacy ``donation_from_purchase`` (bot.py:10715):
        donation rows + xp first, then the owner payout, then the
        leaderboard recalc and today's history snapshot — so a group that
        just received its first ever donation is ranked immediately
        rather than at the next unrelated recalc.
        """
        if group_id == 0:
            return GroupDonationResult(outcome=GroupDonationOutcome.NO_GROUP)
        if self._percent <= 0:
            return GroupDonationResult(outcome=GroupDonationOutcome.DISABLED, group_id=group_id)

        amount = purchase_commission_amount(price, self._percent)
        if amount <= 0 or not validate_credit_amount(amount):
            _log.bind(group=group_id, user=user_id, price=price, amount=amount).error(
                "group donation slice rejected by credit validation — not routed"
            )
            return GroupDonationResult(
                outcome=GroupDonationOutcome.INVALID_AMOUNT, group_id=group_id
            )

        # Naive LOCAL time: these are legacy-shared columns (see the repo
        # method's docstring) whose existing rows are local, not UTC.
        now = datetime.now()  # noqa: DTZ005 — matches the legacy donation writer

        # Every write of the slice goes inside one SAVEPOINT. The caller
        # treats this routing as best-effort — ``handlers/shop.py``
        # swallows the exception so a bonus can never void a purchase —
        # and it then commits the purchase on this same session. Without
        # the savepoint the writes that already landed ride that commit
        # out: the owner keeps his payout and the ledger keeps its rows
        # while ``recalc_positions`` never ran, so the money moved but
        # the board it bought a place on is stale. A failed flush is the
        # worse shape of the same hole — it deactivates the enclosing
        # transaction and the caller's commit then takes the PURCHASE
        # down, after the buyer was shown a success card. Same remedy and
        # same reasoning as ``payments_service.py`` (R15) for the
        # referral kickback.
        session = self._economy._session  # noqa: SLF001 — service over its own repo
        async with session.begin_nested():
            await self._donations.record_donation(
                group_id=group_id,
                user_id=user_id,
                amount=amount,
                message=_PURCHASE_DONATION_MESSAGE,
                now=now,
            )

            to_owner, fee, owner_credited = await self._pay_owner(
                group_id=group_id,
                user_id=user_id,
                amount=amount,
                owner_id=owner_id,
                owner_reason=f"С покупки в группе {group_id} (за вычетом комиссии)",
                fee_reason=f"Комиссия с покупки в группе {group_id}",
            )

            # Recalc + snapshot last: both read the xp this call just wrote.
            await self._donations.recalc_positions()
            # Moscow calendar, like every other writer of this table — the
            # row is an upsert keyed on the date, so the three call sites
            # have to agree or they overwrite each other's days.
            await self._donations.save_history_snapshot(group_id, today=rating_history_date())

        # Keyed on what actually landed, not on what was attempted: an
        # owner we resolved but failed to credit is reported exactly like
        # an owner we never found, so no caller can render a payout line
        # off a truthy ``owner_id`` alone.
        outcome = (
            GroupDonationOutcome.ROUTED if owner_credited else GroupDonationOutcome.ROUTED_NO_OWNER
        )
        _log.bind(
            group=group_id,
            buyer=user_id,
            price=price,
            amount=amount,
            owner=owner_id,
            to_owner=to_owner if owner_credited else 0,
            fee=fee,
        ).info("group purchase donation routed")
        return GroupDonationResult(
            outcome=outcome,
            group_id=group_id,
            amount=amount,
            to_owner=to_owner if owner_credited else 0,
            fee=fee,
            owner_id=owner_id,
            owner_credited=owner_credited,
            percent=self._percent,
        )

    async def _pay_owner(
        self,
        *,
        group_id: int,
        user_id: int,
        amount: int,
        owner_id: int | None,
        owner_reason: str,
        fee_reason: str,
    ) -> tuple[int, int, bool]:
        """Split ``amount`` between the group's creator and the developer.

        Returns ``(to_owner, fee, owner_credited)`` with both figures as
        COMPUTED — the caller decides how to report a payout that did
        not land (see :class:`GroupDonationResult`). Shared verbatim by
        both entry points because legacy shared it too: the split, the
        ``if owner_id:`` skip and both ledger types are byte-identical
        between ``donation_from_purchase`` (bot.py:10747-10753) and
        ``donations_do_donate`` (bot.py:10701-10707); only the two
        ``reason`` sentences differ, and those are what tell a purchase
        slice from a typed donation in a finances panel.
        """
        fee = 0
        to_owner = 0
        owner_credited = False
        # #1401: a fee is a CUT of a payout, so it may not outlive
        # the payout. Distinguished from the legitimate zero below,
        # because the two look identical in ``owner_credited``.
        owner_payout_failed = False
        if owner_id:
            fee = purchase_commission_amount(amount, self._developer_percent)
            to_owner = amount - fee
            owner_credited = await self._credit(
                recipient=owner_id,
                amount=to_owner,
                group_id=group_id,
                buyer_id=user_id,
                reason=owner_reason,
                txn_type=_OWNER_TXN_TYPE,
            )
            # ``_credit`` returns ``False`` for two unrelated
            # reasons: a rounding zero (``amount <= 0`` — a 1-coin
            # slice whose whole value IS the fee, which legacy paid
            # out, bot.py:10750/10752) and a real refusal (the
            # credit validator, or a balance cap that made
            # ``EconomyRepo.credit`` return ``None``). Only the
            # second may cancel the fee; treating the first as a
            # failure would quietly stop charging commission on
            # every minimum-size donation.
            owner_payout_failed = to_owner > 0 and not owner_credited
        # Outside the ``if owner_id`` on purpose: an ownerless group
        # has ``fee == 0`` and the ``amount <= 0`` skip inside
        # ``_credit`` already handles it, so the gate needs to say
        # nothing about that case.
        if not owner_payout_failed and self._developer_id > 0:
            await self._credit(
                recipient=self._developer_id,
                amount=fee,
                group_id=group_id,
                buyer_id=user_id,
                reason=fee_reason,
                txn_type=_FEE_TXN_TYPE,
            )
        return to_owner, fee, owner_credited

    async def donate(
        self,
        *,
        group_id: int,
        user_id: int,
        amount: int,
        title: str | None,
        owner_id: int | None,
    ) -> DonateResult:
        """``/donate`` (#2007): the donor pays ``amount`` to ``group_id``.

        Ports legacy ``donations_do_donate`` (bot.py:10643-10711) with
        its statement order intact — debit, donor ledger row, the four
        donation writes, the owner split, recalc, snapshot — and three
        deliberate differences:

        * **the whole thing is one transaction.** Legacy committed the
          debit and the donation rows, and only then recorded the donor's
          ledger row and paid the owner (both after ``conn.commit()``,
          bot.py:10690-10707) on their own connections. A crash in
          between left coins burned with no ledger row and no payout —
          money gone from the books entirely. Here the debit, the ledger
          row, the group writes and both credits share one SAVEPOINT.
        * **the aggregate row is ensured, not assumed.** Legacy's xp bump
          is a bare ``UPDATE groups_donations`` (bot.py:10687), so a
          donation to a group that had never received one updated zero
          rows: the coins were burned and the board never moved.
          :meth:`DonationsRatingRepo.record_donation` inserts the row
          first.
        * **the group's title is backfilled** while we hold it, so the
          leaderboard can name a group it has just learned about instead
          of printing a bare id.

        ``owner_id`` is resolved by the handler BEFORE this call, for the
        same reason :meth:`route` takes it — no Telegram round-trip may
        happen inside an open SQLite write transaction. ``None``
        reproduces legacy's "xp only, no payout" branch.
        """
        if amount <= 0 or not validate_credit_amount(amount):
            return DonateResult(
                outcome=DonateOutcome.INVALID_AMOUNT, group_id=group_id, amount=amount
            )

        # Read the balance up front so the refusal can name it (legacy
        # does the same, bot.py:10658). The authoritative check is the
        # ``WHERE balance >= amount`` inside ``EconomyRepo.debit`` below;
        # this read only decides which card the user sees.
        wallet = await self._economy.get_or_create(user_id)
        if wallet.balance < amount:
            return DonateResult(
                outcome=DonateOutcome.INSUFFICIENT,
                group_id=group_id,
                amount=amount,
                balance=wallet.balance,
            )

        # Naive LOCAL time, like every other writer of these
        # legacy-shared columns — see ``record_donation``.
        now = datetime.now()  # noqa: DTZ005 — matches the legacy donation writer
        session = self._economy._session  # noqa: SLF001 — service over its own repo
        try:
            async with session.begin_nested():
                debited = await self._economy.debit(user_id, amount)
                if debited is None:
                    # Lost the race against another spend between the
                    # read above and here, or the wallet vanished.
                    raise _DebitRefused
                balance = debited.balance
                # The donor's row first: it is the only side of this
                # that represents money LEAVING someone, and the
                # receipt's assertion order follows the ledger's.
                # ``to_id=0`` and a negative amount are legacy's
                # spelling (bot.py:10694-10696), kept so a donation
                # audit spans the cutover.
                await self._ledger.record(
                    from_id=user_id,
                    to_id=0,
                    amount=-amount,
                    reason=f"Донат в группу {group_id}",
                    type=_DONOR_TXN_TYPE,
                )
                # Empty message: a bare ``/donate <sum>`` carries no
                # comment, and legacy bound ``(comment or "")``
                # (bot.py:10673). This is also what tells a typed
                # donation from a purchase slice in the ``donations``
                # table, where the slice always says «С покупки в группе».
                await self._donations.record_donation(
                    group_id=group_id,
                    user_id=user_id,
                    amount=amount,
                    message="",
                    now=now,
                )
                # After ``record_donation``, which is what guarantees the
                # aggregate row exists — ``save_group_identity`` never
                # creates one and would silently no-op on a group's first
                # ever donation.
                await self._donations.save_group_identity(group_id, link=None, title=title)
                to_owner, fee, owner_credited = await self._pay_owner(
                    group_id=group_id,
                    user_id=user_id,
                    amount=amount,
                    owner_id=owner_id,
                    owner_reason=f"Донат в группу {group_id} (за вычетом комиссии)",
                    fee_reason=f"Комиссия с доната в группу {group_id}",
                )
                await self._donations.recalc_positions()
                await self._donations.save_history_snapshot(group_id, today=rating_history_date())
                # Read back inside the same transaction: the receipt has
                # to quote the score the board now ranks on, and outside
                # this block another donation may already have moved it.
                group_xp = await self._donations.group_xp(group_id)
        except _DebitRefused:
            fresh = await self._economy.get(user_id)
            return DonateResult(
                outcome=DonateOutcome.INSUFFICIENT,
                group_id=group_id,
                amount=amount,
                balance=fresh.balance if fresh is not None else 0,
            )

        outcome = DonateOutcome.DONATED if owner_credited else DonateOutcome.DONATED_NO_OWNER
        _log.bind(
            group=group_id,
            donor=user_id,
            amount=amount,
            owner=owner_id,
            to_owner=to_owner if owner_credited else 0,
            fee=fee,
            group_xp=group_xp,
        ).info("group donation accepted")
        return DonateResult(
            outcome=outcome,
            group_id=group_id,
            amount=amount,
            to_owner=to_owner if owner_credited else 0,
            fee=fee,
            owner_id=owner_id,
            owner_credited=owner_credited,
            balance=balance,
            group_xp=group_xp,
        )

    async def _credit(
        self,
        *,
        recipient: int,
        amount: int,
        group_id: int,
        buyer_id: int,
        reason: str,
        txn_type: str,
    ) -> bool:
        """Checked mint + ledger-after-credit. ``False`` on any skip/failure.

        Non-positive amounts are a normal skip (legacy guards both credits
        with ``> 0``, bot.py:10750/10752): a 1-coin slice with a 5%
        developer cut floors the fee at 1 and leaves the owner 0.
        """
        if amount <= 0:
            return False
        if not validate_credit_amount(amount):
            _log.bind(group=group_id, recipient=recipient, amount=amount, buyer=buyer_id).error(
                "group donation payout rejected by credit validation — not credited"
            )
            return False
        # Self-heal a missing wallet before the checked credit (legacy
        # register_user inside add_coins, bot.py:9724).
        await self._economy.get_or_create(recipient)
        credited = await self._economy.credit(recipient, amount)
        if credited is None:
            _log.bind(group=group_id, recipient=recipient, amount=amount, buyer=buyer_id).error(
                "group donation payout credit failed (balance cap?) — not credited"
            )
            return False
        # ``from_id=None``: the credit above MINTS these coins, so there is
        # no payer. The row used to say ``from_id=buyer_id``, which made
        # ``window_stats`` count the owner slice and the developer fee as
        # money the buyer spent — it sums ``ABS(amount) WHERE from_id ==
        # user`` with no type filter — and made ``TransactionsRepo.recent``
        # render both as a minus on his own finances panel, via the
        # ``signed`` binding that flips the sign for a viewer found in
        # ``from_id`` (#1476: the line range this used to cite pointed at
        # ``reversed_total``). A 1000-coin group buy showed him -1150.
        #
        # The buyer is deliberately NOT folded into ``reason``: this row
        # lands on the recipient's finances panel, so naming him there
        # would hand the group owner a stranger's user id. He is bound to
        # the failure logs above instead, which is where a payout audit
        # actually starts.
        #
        # The string below is no longer printed verbatim — #1547 moved the
        # wording to ``h_profile_tx_group_share`` so an English reader is
        # not handed a Russian sentence — but it IS still matched, whole,
        # by ``profile._TX_PROSE_REASONS`` to keep the rows already
        # written readable. Reword it and the panel silently falls back
        # to printing it raw again, which is what
        # tests/regression/test_tx_reason_labels.py exists to catch.
        await self._ledger.record(
            from_id=None,
            to_id=recipient,
            amount=amount,
            reason=reason,
            type=txn_type,
        )
        return True


__all__ = [
    "DonateOutcome",
    "DonateResult",
    "GroupDonationOutcome",
    "GroupDonationResult",
    "GroupDonationService",
]
