"""``/admin_withdrawals`` — pending-withdrawals management panel (L-97).

Legacy ``/admin_withdrawals`` (bot.py:20652) renders a panel with the
pending count, the cumulative fiat total, the first 5 requests, and an
inline confirm/reject flow. The port covers the full operator loop:

* the card — count, fiat exposure, a paged oldest-first listing
  (legacy capped at the first 5 with no paging; L-97 adds ◀️/▶️
  navigation so a deep queue is reachable without SQL);
* ✅ **approve** — manual-payout doctrine v1: the row flips to
  ``completed`` and the user is DM'd, with NO Crypto Pay auto-transfer
  — the operator pays out externally, exactly the legacy contract
  (bot.py:20717 ``admin_confirm_withdrawal`` updates the row and DMs
  «Средства отправлены по указанным реквизитам» without any provider
  call). This is the L-99-adjacent decision: ``withdraw_instant`` was
  EOL in legacy, payouts stay manual. The auto-payout service path
  (:meth:`WithdrawService.approve`) exists but is intentionally not
  wired here.
* 🚫 **reject** — refund the escrow (existing
  :meth:`WithdrawService.reject` path, mirroring legacy bot.py:20743
  ``admin_reject_withdrawal``) and DM the user. An optional reason is
  available via the command form ``/admin_withdrawals reject <id>
  [reason]`` — the inline button uses the default wording, matching
  legacy which had no reason input at all.

Same posture as every other ``/admin_*`` handler:

* Silent-drop for non-devs (existence must not enumerate dev IDs).
* Private-only on both router chains, messages and callbacks
  (per-user PII like ``payment_details`` must not render in
  groups).
* HTML-escape on operator-displayed strings — ``payment_details`` and
  the reject reason are free text, so escaping is load-bearing.
* Card numbers are masked to BIN + last 4 before rendering (#172,
  :func:`_mask_pan`) — private-only keeps the card out of groups, but
  it still lands in a chat history that can be screenshotted or
  forwarded, and nothing in the live payout path needs a full PAN.

Ageing (#169). Payouts here are *manual*, so ``pending`` is a queue
worked by hand — and a hand-worked queue nobody is reminded of stops
being worked silently (prod carried one ``pending`` row for five months
without a signal anywhere). The card therefore renders each request's
age and flags anything past :data:`STALE_WITHDRAWAL_AGE` with ⚠️, plus a
queue-wide stale counter so the operator sees the backlog even from a
page that happens to hold only fresh rows. The push half of the same
signal — an owner DM the first time a request goes stale — lives in
:mod:`telegram_invite_bot.scheduler.economy_cleanup`; this module is
pull-only by design.

Amounts (#236). A request carries its payout figure in one of two
columns, and the port's own writer fills only the second one:
``WithdrawalsRepo.create`` writes ``amount_crypto`` plus ``currency``
= the ASSET, and never touches ``amount_fiat`` or ``payment_details``
at all. Reading the fiat column alone therefore rendered every
request this bot is able to create as ``4500 COM -> — USDT | —`` —
the card named the debit and hid the credit, which is the one figure
an operator paying out by hand actually needs. Each row now renders
whichever column is populated (``format_crypto_amount`` /
``format_fiat_amount``), and both when a legacy row carries both.

The queue-wide total is summed over ``amount_fiat`` alone, so on a
queue of port-written rows it is structurally ``0``. A line reading
``≈ fiat total: 0.00`` above a non-empty queue is worse than no line
— it reads as a measured zero rather than as an absent column — so
it renders only when non-zero, the same "print it only when it says
something" posture the stale counter already takes. Nothing replaces
it for crypto rows: ``currency`` holds the asset there, and a sum
mixing USDT with TON would be a number with no meaning. Legacy's own
crypto branch (bot.py:20427) also wrote no ``amount_fiat``, so the
empty column is not a regression against legacy — the regression is
that the port made ``amount_crypto`` load-bearing and never rendered
it.

``processing`` rows (#283). The queue listed here is ``pending`` OR
``processing``, not ``pending`` alone. ``processing`` is the lease
``WithdrawalsRepo.claim_processing`` takes before an auto-payout
provider call, and nothing releases it on a timeout — so a process
that dies mid-transfer leaves a real request, with the user's coins
already in escrow, in a status no admin surface listed. Those rows
render with a ⏳ marker and deliberately WITHOUT ✅/🚫 buttons: both
:meth:`WithdrawService.approve_manual` and
:meth:`WithdrawService.reject` guard on ``pending``, so a button on a
leased row could only ever report failure. Recovery stays off the
card on purpose — a leased row may have money already in flight, and
choosing between re-driving and refunding needs the provider's
answer, not a tap.

The push half of that signal — the owner DM in
:mod:`telegram_invite_bot.scheduler.economy_cleanup` — still watches
``pending`` only. That is a stated gap, not an oversight:
``processing`` is unreachable until :meth:`WithdrawService.approve`
is wired (nothing in ``src/`` calls it today), and widening the alert
ledger belongs with that decision rather than ahead of it.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType
from loguru import logger
from sqlalchemy import case, func, select

from telegram_invite_bot.db.models.economy import ProcessedWebhook, WithdrawalRequest
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import WithdrawApprove, WithdrawReject
from telegram_invite_bot.keyboards.builders.withdrawals import WithdrawPage
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.language import best_effort_language_for_user
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.repositories.withdrawals_repo import PROCESSING_STATUS
from telegram_invite_bot.services.withdraw_service import (
    ApproveOutcome,
    RejectOutcome,
)
from telegram_invite_bot.utils.economy import (
    STALE_WITHDRAWAL_AGE,
    format_age,
    format_crypto_amount,
    format_fiat_amount,
    parse_db_timestamp,
)
from telegram_invite_bot.utils.numbers import parse_int_token
from telegram_invite_bot.utils.time import db_now

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.filters import CommandObject
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.withdraw_service import WithdrawService

log = logger.bind(component="handlers.admin.withdrawals")


_SAMPLE_SIZE = 5
_DETAILS_TRUNCATE = 20

# What stays visible on a masked card: the 6-digit BIN (identifies the
# issuing bank) and the last 4 (what the user themselves quotes). That
# pair is the maximum a PAN may be displayed as under PCI DSS 3.3, and
# it is also exactly what an operator needs to tell two requests apart.
_PAN_HEAD = 6
_PAN_TAIL = 4
_PAN_BULLET = "\u2022"
# A card-length digit run ANYWHERE in the value: 12 digits (shortest
# real PAN, Maestro) through 19 (longest), optionally broken by single
# spaces or dashes, and not itself part of a longer number. Scanning
# instead of testing the whole string is the point — legacy
# ``payment_details`` is free text a user typed, so the card arrives as
# «Сбербанк 4111 1111 1111 1111» at least as often as bare, and a
# whole-string test would print exactly those in full. ``[0-9]`` rather
# than ``\d``: the latter also matches Eastern Arabic digits, which
# slice into nonsense instead of into a mask (the #102 lesson).
_PAN_RE = re.compile(r"(?<![0-9])[0-9](?:[ -]?[0-9]){11,18}(?![0-9])")

#: Statuses the panel lists: the hand-worked queue plus the auto-payout
#: lease (#283). Ordered as rendered — ``pending`` first is also the
#: ``claim_processing`` source status, so the pair reads as one lifecycle.
_LISTED_STATUSES = ("pending", PROCESSING_STATUS)


class _SampleRow(NamedTuple):
    """One request as the card renders it.

    A NamedTuple rather than a bare tuple on purpose: this row carries
    two different money columns (``amount_fiat`` in minor units,
    ``amount_crypto`` as a float) plus two different text columns, and
    a positional unpack of eight fields is exactly where a column swap
    survives review. Reading ``row.amount_crypto`` cannot be quietly
    fed a currency code.

    M-E-6: ``amount_fiat`` is Integer minor units (cents/kopecks) and
    renders through ``utils/economy.format_fiat_amount``.
    ``amount_crypto`` is the asset-denominated float the port's own
    writer fills, and renders through ``format_crypto_amount`` (#236).
    ``currency`` means the fiat code on a legacy row and the ASSET on a
    port-written one — which is why it is never summed.
    """

    id: int
    user_id: int
    amount_com: int
    amount_fiat: int | None
    amount_crypto: float | None
    currency: str | None
    payment_details: str | None
    created_at: str | None
    status: str


def _page_count(queue_count: int) -> int:
    """Total pages for the current queue — at least 1 so an empty
    queue still renders page 1/1 semantics (no nav row)."""
    return max(1, -(-queue_count // _SAMPLE_SIZE))


class _Snapshot(NamedTuple):
    """What one :func:`_gather` round trip tells the card.

    ``queue_count`` is what the pager counts — every listed row,
    ``pending`` and ``processing`` alike — while ``pending_count`` and
    ``processing_count`` split it for the header. Keeping all three
    named rather than re-deriving one from the others at each call site
    is what stops the pager and the header from ever disagreeing about
    how long the queue is.
    """

    queue_count: int
    pending_count: int
    processing_count: int
    total_fiat: int
    sample: list[_SampleRow]
    page: int
    stale_count: int
    #: #1400: the subset of ``sample``'s user ids with at least one
    #: reversed payment on record. A set rather than a per-row flag
    #: because it is answered by one indexed query over the whole page,
    #: and because the renderer asks it once per row by membership.
    #: Defaults to empty so a caller that does not need the marker (a
    #: unit test rendering a hand-built snapshot) reads as "no
    #: chargebacks", never as an omission.
    chargeback_users: frozenset[int] = frozenset()


async def _gather(
    registry: EngineRegistry, *, page: int = 0, now: datetime | None = None
) -> _Snapshot:
    """One COUNT/SUM aggregate + one paged sample SELECT against
    ``economy.withdrawal_requests``, returned as a :class:`_Snapshot`.

    The queue is ``pending`` OR ``processing`` (#283) — see the module
    docstring for why a leased row has to be visible here.

    ``stale_count`` is queue-wide, not page-wide (#169): the operator
    must learn there are old requests even while looking at a page of
    fresh ones. It rides along on the existing aggregate as an extra
    column rather than costing a second round trip. Because the WHERE
    now spans both statuses, a stranded lease inherits the ⚠️ for free:
    a row that has sat in ``processing`` past the threshold is exactly
    the case the counter exists to surface.

    ``page`` is clamped against the live count so a stale ▶️ button
    (queue shrank since render) lands on the last real page instead of
    an empty card.

    Two separate ``connect()`` blocks rather than one — identical
    reasoning to ``admin/check_groups``: a diagnostic isn't hot-path,
    failure of one block shouldn't poison the other, and the per-
    block boundary keeps the function trivially auditable.
    """
    now = now or db_now()
    # ``created_at`` is the fixed-width ``"YYYY-MM-DD HH:MM:SS"`` text the
    # write side stamps, so a lexicographic ``<`` is a chronological ``<``
    # — the same equivalence ``WithdrawalsRepo.period_usage`` relies on.
    # Rows with a NULL (legacy imports) or malformed ``created_at`` make
    # the comparison NULL and fall to the ``else_`` 0: unknown age is
    # counted as not-stale rather than raising a fake alarm the operator
    # cannot act on. Their row still renders — see ``_age_cell``.
    #
    # The one known imprecision: a legacy row written with a ``T``
    # separator sorts after a space-separated cutoff of the same date
    # (``"T"`` > ``" "``), so within the threshold window such a row can
    # miss this counter by up to a day. That direction is deliberate —
    # it under-reports, never invents an alarm — and the row's own ⚠️
    # is unaffected, because ``_age_cell`` parses rather than compares
    # strings. So the operator still sees it in the listing.
    stale_before = (now - STALE_WITHDRAWAL_AGE).isoformat(sep=" ", timespec="seconds")
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(WithdrawalRequest.amount_fiat), 0),
                    func.coalesce(
                        func.sum(case((WithdrawalRequest.created_at < stale_before, 1), else_=0)),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (WithdrawalRequest.status == PROCESSING_STATUS, 1),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                ).where(WithdrawalRequest.status.in_(_LISTED_STATUSES))
            )
        ).one()
        queue_count = int(row[0])
        # M-E-6: ``amount_fiat`` is Integer minor units now, so the
        # SUM is exact-int — no float drift across thousands of rows.
        # COALESCE keeps the empty-table case at 0.
        total_fiat_minor = int(row[1] or 0)
        stale_count = int(row[2] or 0)
        processing_count = int(row[3] or 0)
        # Derived, never counted separately: the two must add up to the
        # number the pager slices, and subtraction is the only way to
        # guarantee that without a second aggregate.
        pending_count = queue_count - processing_count

    page = min(max(0, page), _page_count(queue_count) - 1)
    sample: list[_SampleRow] = []
    if queue_count > 0:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(
                    WithdrawalRequest.id,
                    WithdrawalRequest.user_id,
                    WithdrawalRequest.amount_com,
                    WithdrawalRequest.amount_fiat,
                    WithdrawalRequest.amount_crypto,
                    WithdrawalRequest.currency,
                    WithdrawalRequest.payment_details,
                    WithdrawalRequest.created_at,
                    WithdrawalRequest.status,
                )
                .where(WithdrawalRequest.status.in_(_LISTED_STATUSES))
                # ASC by id matches legacy's "oldest first" — operators
                # process the queue head-first, so the card shows the
                # rows they're about to touch.
                .order_by(WithdrawalRequest.id.asc())
                .offset(page * _SAMPLE_SIZE)
                .limit(_SAMPLE_SIZE)
            )
            sample = [
                _SampleRow(
                    id=int(r[0]),
                    user_id=int(r[1]),
                    amount_com=int(r[2]),
                    amount_fiat=(int(r[3]) if r[3] is not None else None),
                    amount_crypto=(float(r[4]) if r[4] is not None else None),
                    currency=r[5],
                    payment_details=r[6],
                    created_at=r[7],
                    status=str(r[8]),
                )
                for r in rows.all()
            ]
    # #1400: does anyone on this page have money the provider took back?
    # ``lifetime_deposits`` already subtracts it from the R2/R6 gates, so
    # a chargeback account cannot silently keep its raised cap — but the
    # gates are arithmetic and this desk is a human decision, and the
    # operator about to press ✅ deserves to see the fact rather than
    # infer it from a headroom figure that shrank. One indexed read over
    # ``idx_processed_webhooks_user``, restricted to the ids actually on
    # screen, in its own ``connect()`` block for the same reason the two
    # above are separate: a diagnostic is not a hot path, and a failure
    # here must not cost the operator the queue itself.
    chargeback_users: frozenset[int] = frozenset()
    if sample:
        user_ids = {row.user_id for row in sample}
        async with engine.connect() as conn:
            flagged = await conn.execute(
                select(ProcessedWebhook.user_id)
                .where(
                    ProcessedWebhook.user_id.in_(user_ids),
                    ProcessedWebhook.reversed_at.is_not(None),
                )
                .distinct()
            )
            chargeback_users = frozenset(int(r[0]) for r in flagged.all())
    return _Snapshot(
        queue_count=queue_count,
        pending_count=pending_count,
        processing_count=processing_count,
        total_fiat=total_fiat_minor,
        sample=sample,
        page=page,
        stale_count=stale_count,
        chargeback_users=chargeback_users,
    )


def _truncate(s: str | None) -> str:
    """Truncate ``payment_details`` to keep the card readable.

    Legacy rendered ``details[:20] + "..."`` when over 20 chars and we
    match exactly. The original reason — an operator comparing this
    card against a legacy screenshot — went away with the process in
    T-011; the boundary stays where it is because it is pinned by the
    test that asserts on truncation, and moving it would change what
    every archived payout screenshot means. The 21st character is
    intentionally NOT shown.
    """
    if not s:
        return "—"
    if len(s) > _DETAILS_TRUNCATE:
        return s[:_DETAILS_TRUNCATE] + "…"
    return s


def _mask_pan(s: str | None) -> str:
    """Render ``payment_details``, masking the middle of a card number.

    ``_truncate`` alone was not a mask: it cuts at 20 characters and a
    PAN is 16, so every card ever typed into the legacy ``/withdraw``
    form rendered here IN FULL. This card is a Telegram message — it
    lives in the operator's chat history, on Telegram's servers, and in
    any screenshot or forward of it — so "admin-only" is where the
    exposure starts, not where it ends. Prod carried exactly one such
    row for five months (#172).

    Every card-length digit run (12–19 digits, spaces and dashes
    allowed) is replaced IN PLACE by BIN + bullets + last 4, so the
    surrounding text a user typed — a bank name, «карта Сбер» —
    survives. Anything shorter is left alone: an SBP phone (11
    digits) and a crypto address are payout *instruments* the
    operator reads off this card, and neither is a secret the way a
    PAN is, so bulleting them would cost readability and buy nothing.

    Nothing is lost operationally. The live ``/withdraw`` flow is
    Crypto Pay only and never writes ``payment_details`` at all
    (``WithdrawalsRepo.create``), so the only rows carrying a PAN are
    legacy ones that no payout path can act on. If a manual card payout
    is ever built, it needs a deliberate single-row reveal — not a
    listing that prints every queued card at once.
    """
    if not s:
        return "\u2014"
    # Mask BEFORE truncating, never after: ``_truncate`` keeps the first
    # 20 characters and a PAN is 16, so the truncate-first order hands
    # the whole card straight through — which is precisely the bug.
    return _truncate(_PAN_RE.sub(_mask_one_pan, s))


def _mask_one_pan(match: re.Match[str]) -> str:
    """Collapse one matched card-length digit run to BIN + bullets + last 4.

    Separators are dropped rather than kept: re-spacing a half-bulleted
    number buys nothing, and one canonical shape is what lets a reader
    recognise the cell as masked at a glance.
    """
    digits = match.group().replace(" ", "").replace("-", "")
    hidden = len(digits) - _PAN_HEAD - _PAN_TAIL
    return f"{digits[:_PAN_HEAD]}{_PAN_BULLET * hidden}{digits[-_PAN_TAIL:]}"


def _age_cell(created_at: str | None, now: datetime) -> str:
    """Render the age column for one request: ``"3h"``, ``"⚠️ 12d"``, ``"?"``.

    Three outcomes, and the third is the one worth stating: a row whose
    ``created_at`` is NULL or unparseable gets ``"?"``, never ⚠️ and
    never an invented age. Legacy imports really do carry such rows, and
    a made-up "0m" on one would read as *fresh* — the exact opposite of
    the truth — while a made-up ⚠️ would send the operator chasing a
    timestamp that does not exist. ``"?"`` is the honest cell, and the
    raw ``created_at`` is still printed beside it.
    """
    parsed = parse_db_timestamp(created_at)
    if parsed is None:
        return "?"
    age = now - parsed
    label = format_age(age)
    return f"⚠️ {label}" if age >= STALE_WITHDRAWAL_AGE else label


def _payout_cell(row: _SampleRow) -> str:
    """The credit side of one request — whichever amount column the
    writer actually filled (#236).

    ``amount_crypto`` wins when both are present because it is the
    figure the operator transfers; the fiat value on such a row is the
    quote that produced it, so it leads and the crypto rides in
    parentheses. A row with neither column renders ``—`` rather than a
    zero: absent is not the same as free, and only one of those two is
    safe for someone about to move money by hand.
    """
    fiat = (
        format_fiat_amount(row.amount_fiat, currency=None) if row.amount_fiat is not None else None
    )
    crypto = (
        format_crypto_amount(row.amount_crypto, None) if row.amount_crypto is not None else None
    )
    if fiat is not None and crypto is not None:
        return f"{fiat} ({crypto})"
    return fiat or crypto or "—"


def _render(snapshot: _Snapshot, *, now: datetime | None = None) -> str:
    pending_count = snapshot.pending_count
    sample = snapshot.sample
    page = snapshot.page
    lines = ["🏧 <b>Pending withdrawals</b>", ""]
    lines.append(f"• pending: <code>{pending_count}</code>")
    if snapshot.processing_count:
        # #283: a lease nothing releases on a timeout. Only rendered
        # when non-zero — on the healthy path this status does not
        # exist, and a permanent "processing: 0" would train the
        # operator to skip the line on the one day it isn't zero.
        lines.append(f"• ⏳ processing (leased): <code>{snapshot.processing_count}</code>")
    if snapshot.total_fiat:
        # M-E-6: total is summed minor units (int) — exact across
        # thousands of rows. Render through ``format_fiat_amount`` so
        # the 2dp form ("123.45") stays in one place. Currency is mixed
        # across rows, so we render without the suffix here.
        #
        # #236: only when non-zero. This sums ``amount_fiat`` alone, a
        # column the port's own writer never fills, so on a queue of
        # port-written rows it is structurally 0 — and "≈ fiat total:
        # 0.00" over a non-empty queue reads as a measured zero rather
        # than as an absent column.
        total_fiat_str = format_fiat_amount(snapshot.total_fiat, currency=None)
        lines.append(f"• ≈ fiat total: <code>{total_fiat_str}</code>")
    if snapshot.stale_count:
        # Queue-wide, so it stays true on every page. Only rendered when
        # non-zero: a clean queue should read as clean, not as a "stale: 0"
        # line the operator has to parse before relaxing.
        hours = int(STALE_WITHDRAWAL_AGE.total_seconds() // 3600)
        lines.append(f"• ⚠️ stale (&gt;{hours}h): <code>{snapshot.stale_count}</code>")
    if sample:
        pages = _page_count(snapshot.queue_count)
        lines.append("")
        if pages > 1:
            # Multi-page: the section header carries the cursor so the
            # operator sees where in the queue this slice sits.
            lines.append(f"<b>Page {page + 1}/{pages} (oldest first):</b>")
        else:
            lines.append(f"<b>Latest {len(sample)} (oldest first):</b>")
        now = now or db_now()
        for row in sample:
            safe_details = html.escape(_mask_pan(row.payment_details))
            # ``currency`` labels whichever amount ``_payout_cell``
            # chose: the fiat code on a legacy row, the ASSET on a
            # port-written one. Escaped either way — it is a column a
            # legacy writer could put anything in.
            cur_str = html.escape(row.currency) if row.currency else "—"
            payout_str = _payout_cell(row)
            created_str = html.escape(row.created_at or "—")
            # ``_age_cell`` emits only ASCII + the ⚠️ emoji, so it needs
            # no escaping — but ``created_str`` beside it does, and stays
            # escaped: it is legacy-writable free text.
            age_str = _age_cell(row.created_at, now)
            lease = "⏳ " if row.status == PROCESSING_STATUS else ""
            # #1400: emoji + ASCII, so no escaping — and appended rather
            # than given a column of its own, because on a healthy queue
            # every row would carry an empty one.
            chargeback = " | ⚠️ chargeback" if row.user_id in snapshot.chargeback_users else ""
            lines.append(
                f"  • {lease}<code>#{row.id}</code> uid=<code>{row.user_id}</code> "
                f"<code>{row.amount_com}</code> DLAB → <code>{payout_str}</code> "
                f"{cur_str} | {safe_details} | {created_str} | {age_str}{chargeback}"
            )
    return "\n".join(lines)


def _keyboard(snapshot: _Snapshot) -> InlineKeyboardMarkup | None:
    """One ✅/🚫 row per *pending* request plus a ◀️/▶️ nav row when the
    queue spans multiple pages; ``None`` for an empty queue.

    ``processing`` rows are listed but get no buttons (#283): both
    :meth:`WithdrawService.approve_manual` and
    :meth:`WithdrawService.reject` guard on ``pending``, so a button
    there could only ever report failure — and offering an action that
    cannot succeed on a row that may have money in flight is worse than
    offering none.

    Labels are language-neutral (emoji + ``#id``) so the card needs no
    operator-language lookup. Each button carries only the ``request_id``
    (or target page) — the callback handlers re-check developer auth
    server-side, so the payload is a target reference, not a capability
    token.
    """
    page = snapshot.page
    rows: list[list[InlineKeyboardButton]] = []
    for row in snapshot.sample:
        if row.status != "pending":
            continue
        rid = row.id
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ #{rid}",
                    callback_data=WithdrawApprove(request_id=rid).pack(),
                ),
                InlineKeyboardButton(
                    text=f"🚫 #{rid}",
                    callback_data=WithdrawReject(request_id=rid).pack(),
                ),
            ]
        )
    pages = _page_count(snapshot.queue_count)
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(text="◀️", callback_data=WithdrawPage(page=page - 1).pack())
            )
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton(text="▶️", callback_data=WithdrawPage(page=page + 1).pack())
            )
        if nav:
            rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def handle_admin_withdrawals(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_withdrawals; silently dropped"
        )
        return
    snapshot = await _gather(registry)
    await message.answer(_render(snapshot), reply_markup=_keyboard(snapshot))
    log.bind(
        user_id=user.id,
        pending=snapshot.pending_count,
        processing=snapshot.processing_count,
        total_fiat=snapshot.total_fiat,
        stale=snapshot.stale_count,
    ).info("/admin_withdrawals rendered")


async def _refresh_card(
    callback: CallbackQuery, registry: EngineRegistry, *, page: int = 0
) -> None:
    """Re-render the panel in place after an action so the operator sees
    the queue shrink (or to show another page). Best-effort: a failed
    edit (message too old, race, identical content) is swallowed — any
    action already committed."""
    msg = callback.message
    if not isinstance(msg, MessageType):
        return
    snapshot = await _gather(registry, page=page)
    try:
        await msg.edit_text(_render(snapshot), reply_markup=_keyboard(snapshot))
    except (TelegramBadRequest, TelegramForbiddenError):
        log.bind(chat_id=msg.chat.id, message_id=msg.message_id).debug(
            "/admin_withdrawals card refresh swallowed"
        )


async def _notify_user(bot: Bot, user_id: int, text: str) -> None:
    """DM the target user about their request's resolution. Swallows the
    blocked-bot / deleted-account races — the money move already
    committed, so a failed notification must not raise."""
    try:
        await bot.send_message(user_id, text)
    except (TelegramBadRequest, TelegramForbiddenError):
        log.bind(user_id=user_id).debug("/withdraw user notification swallowed")


async def _perform_approve(
    *,
    request_id: int,
    admin_id: int,
    admin_lang: str,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    withdraw_service: WithdrawService,
    bot: Bot,
) -> str:
    """Approve ``request_id`` under the manual-payout doctrine and DM
    the user. Returns the rendered operator toast/reply text.

    No Crypto Pay call happens here — the row flips to ``completed``
    and the operator pays out externally (legacy bot.py:20717 parity).
    Which makes the payout figure the whole point of the toast (#236):
    the operator is being told to move money, and the confirmation
    used to name only the request id. ``approve_manual`` already
    back-fills ``amount_crypto`` from ``to_crypto`` and ``asset`` from
    the row's currency, so a real figure is always available here — it
    was simply being dropped on the floor.
    """
    result = await withdraw_service.approve_manual(request_id=request_id, admin_id=admin_id)
    outcome = result.outcome

    if outcome is ApproveOutcome.COMPLETED and result.user_id is not None:
        # The DM goes to the payee, not to the operator, so it must be
        # rendered in the PAYEE's language (#1507). The source of truth
        # is ``user_settings.language`` in users.db — where ``/lang``
        # writes — not ``economy.users.language``, which
        # ``EconomyRepo.get_or_create`` stamps once under
        # ``ON CONFLICT DO NOTHING`` and never refreshes.
        user_lang = await best_effort_language_for_user(
            result.user_id,
            users_repo=users_repo,
            settings_repo=user_settings_repo,
            fallback=admin_lang,
        )
        await _notify_user(
            bot,
            result.user_id,
            t("h_withdraw_dm_completed_manual", user_lang, request_id=request_id),
        )
        log.bind(
            request_id=request_id,
            admin_id=admin_id,
            uid=result.user_id,
            amount_crypto=result.amount_crypto,
            asset=result.asset,
        ).info("/admin_withdrawals approve → completed (manual payout)")
        # ``asset`` is a DB column, and the command form renders this
        # same string as HTML — escape it exactly like the reject
        # reason below. The inline-button path shows it in a plain-text
        # toast, so an escape there is cosmetic at worst; a missing one
        # on the HTML path is a 400.
        return t(
            "h_withdraw_adm_completed_manual",
            admin_lang,
            request_id=request_id,
            amount=html.escape(format_crypto_amount(result.amount_crypto, result.asset)),
        )

    log.bind(request_id=request_id, admin_id=admin_id, outcome=outcome.value).warning(
        "/admin_withdrawals approve not completed"
    )
    return t(
        _APPROVE_TOASTS.get(outcome, "h_withdraw_adm_failed"),
        admin_lang,
        request_id=request_id,
    )


async def _perform_reject(
    *,
    request_id: int,
    admin_id: int,
    admin_lang: str,
    reason: str | None,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    withdraw_service: WithdrawService,
    bot: Bot,
) -> str:
    """Reject ``request_id`` (escrow refund) and DM the user — with the
    operator's ``reason`` when one was given, the default wording
    otherwise. Returns the rendered operator toast/reply text.

    Shared by the 🚫 inline button (no reason input, like legacy) and
    the ``/admin_withdrawals reject <id> [reason]`` command form.
    """
    result = await withdraw_service.reject(request_id=request_id, admin_id=admin_id, note=reason)
    outcome = result.outcome

    if outcome is RejectOutcome.REJECTED and result.user_id is not None:
        # The DM goes to the payee, not to the operator, so it must be
        # rendered in the PAYEE's language (#1507). The source of truth
        # is ``user_settings.language`` in users.db — where ``/lang``
        # writes — not ``economy.users.language``, which
        # ``EconomyRepo.get_or_create`` stamps once under
        # ``ON CONFLICT DO NOTHING`` and never refreshes.
        user_lang = await best_effort_language_for_user(
            result.user_id,
            users_repo=users_repo,
            settings_repo=user_settings_repo,
            fallback=admin_lang,
        )
        if reason:
            # Free operator text rendered into an HTML message — escape.
            dm = t(
                "h_withdraw_dm_rejected_reason",
                user_lang,
                request_id=request_id,
                amount=result.amount_com or 0,
                reason=html.escape(reason),
            )
        else:
            dm = t(
                "h_withdraw_dm_rejected",
                user_lang,
                request_id=request_id,
                amount=result.amount_com or 0,
            )
        await _notify_user(bot, result.user_id, dm)
        log.bind(request_id=request_id, admin_id=admin_id, uid=result.user_id).info(
            "/admin_withdrawals reject → refunded"
        )
        return t("h_withdraw_adm_rejected", admin_lang, request_id=request_id)

    log.bind(request_id=request_id, admin_id=admin_id, outcome=outcome.value).warning(
        "/admin_withdrawals reject not completed"
    )
    return t(
        _REJECT_TOASTS.get(outcome, "h_withdraw_adm_failed"),
        admin_lang,
        request_id=request_id,
    )


class _RejectArgs(NamedTuple):
    """The parsed form of ``/admin_withdrawals reject <id> [reason]``."""

    request_id: int
    reason: str | None


def parse_reject_args(args: str) -> _RejectArgs | None:
    """Parse the command's argument tail, or ``None`` when it is unusable.

    A module-level function rather than three lines inside the handler
    for the same reason :func:`handlers.groupadmin.parse_staff_grant` is
    one: the parse is the part with the edge cases, and inside a handler
    it can only be reached through a wired Dispatcher.

    #1692: the id used to be read with a bare ``int()`` guarded by
    ``except ValueError``, which is the wrong sibling. A twenty-digit
    run parses cleanly and then raises ``OverflowError`` inside
    ``WithdrawalsRepo.get``, which binds it — one layer past the handler
    that had already accepted the argument. Verified against a live
    schema: ``OverflowError: Python int too large to convert to SQLite
    INTEGER``.

    Two narrower forms the old parse accepted are now refused, both
    deliberately. ``-5`` used to reach the repo and come back NOT_FOUND;
    request ids are AUTOINCREMENT, so a negative one cannot exist and
    the usage hint is the more honest answer. ``+5`` used to read as 5;
    :func:`parse_int_token` takes a sign only when asked, and asking
    here would re-admit the negative. Both are developer-only surfaces.
    """
    parts = args.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "reject":
        return None
    request_id = parse_int_token(parts[1])
    if request_id is None:
        return None
    reason = parts[2].strip() if len(parts) == 3 else None
    return _RejectArgs(request_id, reason or None)


async def handle_admin_withdrawals_command(
    message: Message,
    command: CommandObject,
    settings: Settings,
    registry: EngineRegistry,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    withdraw_service: WithdrawService,
    bot: Bot,
    lang: str,
) -> None:
    """``/admin_withdrawals`` entry — bare renders the card; the
    ``reject <id> [reason]`` argument form rejects with an operator
    reason (the one thing the inline button can't carry).

    ``lang`` is the root :class:`LanguageMiddleware` stamp for whoever
    typed the command — the operator's own language, already resolved
    once per update. Re-deriving it here from ``economy.users`` was the
    #1507 regression: that column is never refreshed by ``/lang``."""
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_withdrawals; silently dropped"
        )
        return
    args = (command.args or "").strip()
    if not args:
        await handle_admin_withdrawals(message, settings, registry)
        return

    parsed = parse_reject_args(args)
    if parsed is None:
        await message.answer(t("h_withdraw_adm_usage", lang))
        return
    reply = await _perform_reject(
        request_id=parsed.request_id,
        admin_id=user.id,
        admin_lang=lang,
        reason=parsed.reason,
        users_repo=users_repo,
        user_settings_repo=user_settings_repo,
        withdraw_service=withdraw_service,
        bot=bot,
    )
    await message.answer(reply)


async def handle_withdraw_approve(
    callback: CallbackQuery,
    callback_data: WithdrawApprove,
    settings: Settings,
    registry: EngineRegistry,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    withdraw_service: WithdrawService,
    bot: Bot,
    lang: str,
) -> None:
    """✅ Approve — mark completed (manual payout), DM the user."""
    user = callback.from_user
    if not settings.bot.is_developer(user.id):
        await callback.answer()
        return
    toast = await _perform_approve(
        request_id=callback_data.request_id,
        admin_id=user.id,
        admin_lang=lang,
        users_repo=users_repo,
        user_settings_repo=user_settings_repo,
        withdraw_service=withdraw_service,
        bot=bot,
    )
    await callback.answer(toast, show_alert=True)
    # Whatever the outcome, re-render: a completed/rejected row drops
    # out, a not-found / already-processed click re-syncs a stale card.
    await _refresh_card(callback, registry)


async def handle_withdraw_reject(
    callback: CallbackQuery,
    callback_data: WithdrawReject,
    settings: Settings,
    registry: EngineRegistry,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    withdraw_service: WithdrawService,
    bot: Bot,
    lang: str,
) -> None:
    """🚫 Reject — refund the escrow, mark the row rejected, DM the user."""
    user = callback.from_user
    if not settings.bot.is_developer(user.id):
        await callback.answer()
        return
    toast = await _perform_reject(
        request_id=callback_data.request_id,
        admin_id=user.id,
        admin_lang=lang,
        reason=None,
        users_repo=users_repo,
        user_settings_repo=user_settings_repo,
        withdraw_service=withdraw_service,
        bot=bot,
    )
    await callback.answer(toast, show_alert=True)
    await _refresh_card(callback, registry)


async def handle_withdraw_page(
    callback: CallbackQuery,
    callback_data: WithdrawPage,
    settings: Settings,
    registry: EngineRegistry,
) -> None:
    """◀️/▶️ — re-render the card at the requested (clamped) page."""
    user = callback.from_user
    if not settings.bot.is_developer(user.id):
        await callback.answer()
        return
    await _refresh_card(callback, registry, page=callback_data.page)
    await callback.answer()


# Outcome → toast i18n-key maps. Kept beside the handlers so a new
# outcome surfaces as a missing-key here rather than a silent fallthrough.
#
# The panel currently calls ``approve_manual``, which cannot produce the
# three provider outcomes — those belong to ``approve``, the auto-payout
# path that is not wired yet. They are mapped anyway, and deliberately:
# PAYOUT_UNCONFIRMED means the transfer may well have gone through and
# the request was left leased on purpose, so the generic "failed" toast
# would tell the admin "nothing happened" — the one thing it does not
# mean. An admin who reads that and refunds the user by hand pays twice.
# Wiring ``approve`` must not depend on someone remembering this file.
_APPROVE_TOASTS: dict[ApproveOutcome, str] = {
    ApproveOutcome.NOT_FOUND: "h_withdraw_adm_not_found",
    ApproveOutcome.ALREADY_PROCESSED: "h_withdraw_adm_already",
    ApproveOutcome.APP_WALLET_EMPTY: "h_withdraw_adm_wallet_empty",
    ApproveOutcome.PROVIDER_ERROR: "h_withdraw_adm_provider_error",
    ApproveOutcome.PAYOUT_UNCONFIRMED: "h_withdraw_adm_payout_unconfirmed",
}
_REJECT_TOASTS: dict[RejectOutcome, str] = {
    RejectOutcome.NOT_FOUND: "h_withdraw_adm_not_found",
    RejectOutcome.ALREADY_PROCESSED: "h_withdraw_adm_already",
    RejectOutcome.REFUND_FAILED: "h_withdraw_adm_refund_failed",
}


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    """Private-only on both event types — payment_details is per-user
    PII (card numbers, wallet addresses, IBANs typed via the legacy
    /withdraw form). Rendering in a group would be an immediate
    privacy leak.

    The approve/reject paths need an economy session (withdraw service
    + repo), so :class:`EconomyMiddleware` is mounted with the withdraw
    config on BOTH chains: callbacks for the inline buttons, messages
    for the ``reject <id> [reason]`` command form.

    :class:`SessionMiddleware` joins it on both chains for the payee's
    language (#1507). The DM that tells someone their withdrawal was
    approved or refunded has to be in the language they chose, and
    ``/lang`` stores that in ``user_settings`` in users.db — the
    economy wallet's ``language`` column is stamped once at wallet
    creation, under ``ON CONFLICT DO NOTHING``, and never refreshed.
    The ``dispatcher`` provider in ``di/providers.py`` also mounts
    ``SessionMiddleware`` outer on both event types, so this is
    belt-and-braces in prod — but it is what makes the router usable on
    its own, which is how every handler here is exercised.
    """
    router = Router(name="admin.withdrawals")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)
    # Both event types: a router-level ``message.filter`` does NOT
    # propagate to ``callback_query`` in aiogram 3, so without this the
    # card's buttons stayed live after a forward into a group.
    router.callback_query.filter(
        lambda c: c.message is not None and c.message.chat.type == ChatType.PRIVATE
    )
    router.message.middleware(EconomyMiddleware(registry, withdraw_config=settings.withdraw))
    router.callback_query.middleware(EconomyMiddleware(registry, withdraw_config=settings.withdraw))
    router.message.middleware(SessionMiddleware(registry))
    router.callback_query.middleware(SessionMiddleware(registry))

    async def _entry(
        message: Message,
        command: CommandObject,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        withdraw_service: WithdrawService,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_admin_withdrawals_command(
            message,
            command,
            settings,
            registry,
            users_repo,
            user_settings_repo,
            withdraw_service,
            bot,
            lang,
        )

    async def _approve(
        callback: CallbackQuery,
        callback_data: WithdrawApprove,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        withdraw_service: WithdrawService,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_withdraw_approve(
            callback,
            callback_data,
            settings,
            registry,
            users_repo,
            user_settings_repo,
            withdraw_service,
            bot,
            lang,
        )

    async def _reject(
        callback: CallbackQuery,
        callback_data: WithdrawReject,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        withdraw_service: WithdrawService,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_withdraw_reject(
            callback,
            callback_data,
            settings,
            registry,
            users_repo,
            user_settings_repo,
            withdraw_service,
            bot,
            lang,
        )

    async def _page(callback: CallbackQuery, callback_data: WithdrawPage) -> None:
        await handle_withdraw_page(callback, callback_data, settings, registry)

    router.message.register(_entry, Command("admin_withdrawals", ignore_case=True))
    router.callback_query.register(_approve, WithdrawApprove.filter(), F.from_user)
    router.callback_query.register(_reject, WithdrawReject.filter(), F.from_user)
    router.callback_query.register(_page, WithdrawPage.filter(), F.from_user)
    return router
