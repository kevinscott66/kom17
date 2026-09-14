"""``/withdraw_status`` — caller's own withdrawal-request history.

Legacy ``/withdraw_status`` (bot.py:20365) reads the last 10
``withdrawal_requests`` rows for the calling user, ordered newest-first,
and returns a plain Markdown list. No keyboard, no FSM, no side-effects
on balance or limits. Read-only from start to finish.

The user-facing complement to the freshly-ported (Stage 39)
``/admin_withdrawals`` operator card. Where the admin card sums fiat
across all pending rows, this one filters by ``user_id`` and shows
*every* status the caller has — pending, completed, rejected — so the
user can verify "yes, the COM I withdrew yesterday cleared" without
having to ping support.

Behaviour parity (pinned):

* Last 10 rows, ``id`` DESC — legacy literal. Newest-first matches the
  question users actually ask ("did my latest one go through?").
* Empty state → friendly "no requests" line, not silent.
* Status is localised, NOT rendered verbatim (#245(a) — this is a
  deliberate break with legacy, which printed the raw column at
  ``bot.py:20383``). The column holds two spellings of one event:
  legacy's reject path wrote ``'cancelled'`` (``bot.py:20754``), this
  port's writes ``'rejected'``
  (:mod:`telegram_invite_bot.services.withdraw_service`), and prod
  already carries a row of each. Printing the column meant one command
  answering the same question with two different words depending on
  which era wrote the row. :data:`_STATUS_KEYS` maps both onto one
  label. An unrecognised status still falls through to the escaped raw
  value: statuses are operator-set free-text, so the render must not
  turn a status nobody anticipated into a blank or a raised key.
* Currency / payment_details are not shown on this card — the caller
  already knows what they typed; this is a "did it happen?" view, not
  a receipt. Matches legacy.
* No private/group restriction at the router level — legacy answers in
  group chats too. The query is user-scoped, so there's no privacy
  leak from rendering in a group: the caller is identifying themselves
  via ``message.from_user.id``.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Final

from aiogram import Router
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.db.models.economy import WithdrawalRequest
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.withdrawals_repo import PROCESSING_STATUS
from telegram_invite_bot.utils.economy import format_fiat_amount

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.withdraw_status")


_LIMIT = 10

#: Stored ``withdrawal_requests.status`` -> i18n key (#245(a)).
#:
#: Two spellings share one label on purpose. ``'rejected'`` is what
#: this port writes; ``'cancelled'`` is what legacy wrote for the same
#: admin action (``bot.py:20754``). Both mean "refused, escrow
#: refunded", and prod holds rows of each era, so a user scrolling
#: their own history would otherwise see the difference and have no way
#: to know it is not one.
#:
#: The stored literal is deliberately NOT normalised — see
#: :mod:`telegram_invite_bot.services.withdraw_service`, whose enum and
#: ``claim_terminal`` write ``'rejected'``, and the four tests that pin
#: it. A rewrite would buy nothing an alias here does not, and would
#: cost a migration over one row.
_STATUS_KEYS: Final[dict[str, str]] = {
    "pending": "h_withdraw_state_pending",
    PROCESSING_STATUS: "h_withdraw_state_processing",
    "completed": "h_withdraw_state_completed",
    "rejected": "h_withdraw_state_rejected",
    "cancelled": "h_withdraw_state_rejected",
}

# (id, amount_com, amount_fiat_minor, currency, status, created_at)
# M-E-6: ``amount_fiat`` is now stored as minor units (int) — see
# ``db/models/economy.WithdrawalRequest`` and
# ``utils/economy.format_fiat_amount``.
_Row = tuple[int, int, int | None, str | None, str | None, str | None]


async def _gather(registry: EngineRegistry, *, user_id: int) -> list[_Row]:
    """Last 10 rows for ``user_id``, newest-first.

    One ``connect()``, one SELECT — read-only, not hot-path. ``id DESC``
    is intentional: in the legacy schema ``created_at`` is a free-form
    string (we kept it as TEXT in the ORM for that exact reason) so
    sorting on it is unsafe across rows with mixed formats. ``id`` is
    a monotonically-increasing autoincrement and gives the same
    newest-first answer without the text-sort hazard.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                WithdrawalRequest.id,
                WithdrawalRequest.amount_com,
                WithdrawalRequest.amount_fiat,
                WithdrawalRequest.currency,
                WithdrawalRequest.status,
                WithdrawalRequest.created_at,
            )
            .where(WithdrawalRequest.user_id == user_id)
            .order_by(WithdrawalRequest.id.desc())
            .limit(_LIMIT)
        )
        return [
            (
                int(r[0]),
                int(r[1]),
                (int(r[2]) if r[2] is not None else None),
                r[3],
                r[4],
                r[5],
            )
            for r in rows.all()
        ]


def _render(rows: list[_Row], lang: str) -> str:
    if not rows:
        return f"{t('h_withdraw_status_header', lang)}\n\n{t('h_withdraw_status_empty', lang)}"
    lines = [t("h_withdraw_status_header", lang), ""]
    for rid, com, fiat, cur, status, created in rows:
        # Each render position is HTML-escaped even when the source
        # *should* be safe — currency is operator-typed via the legacy
        # /withdraw form (string column), status is operator-set, and
        # created_at carries an ISO string but a future admin tool
        # could overwrite it. Cheap insurance against an operator
        # accidentally typing ``<`` somewhere.
        cur_str = html.escape(cur) if cur else "—"
        # Known statuses get a localised word; anything else falls back
        # to the escaped column so an unanticipated operator-set value
        # is still legible instead of vanishing behind a dash.
        if status and status in _STATUS_KEYS:
            status_str = t(_STATUS_KEYS[status], lang)
        else:
            status_str = html.escape(status) if status else "—"
        created_str = html.escape(created) if created else "—"
        # M-E-6: ``fiat`` is minor units; route through the shared
        # helper so the format ("12.34") stays in one place. The
        # currency suffix is already rendered separately on this card,
        # so we pass ``currency=None`` to keep the legacy column
        # layout ("<amount> CUR — STATUS").
        fiat_str = format_fiat_amount(fiat, currency=None) if fiat is not None else "—"
        lines.append(
            t(
                "h_withdraw_status_row",
                lang,
                rid=rid,
                com=com,
                fiat=fiat_str,
                currency=cur_str,
                status=status_str,
                created=created_str,
            )
        )
    return "\n".join(lines)


async def handle_withdraw_status(message: Message, registry: EngineRegistry, *, lang: str) -> None:
    user = message.from_user
    if user is None:
        # Anonymous/sender_chat-as-author has no user_id — nothing to
        # scope the query to. Legacy implicitly skips this branch
        # (reads ``message.from_user.id`` without a guard and the
        # safe_handler swallows the AttributeError).
        return
    rows = await _gather(registry, user_id=user.id)
    await message.answer(_render(rows, lang))
    log.bind(user_id=user.id, count=len(rows), lang=lang).info("/withdraw_status rendered")


def build_router(registry: EngineRegistry) -> Router:
    """No private-only filter at the router level — see module docstring.
    The query is user-scoped so group rendering doesn't leak other
    users' rows.
    """
    router = Router(name="withdraw_status")

    async def _entry(message: Message, lang: str) -> None:
        await handle_withdraw_status(message, registry, lang=lang)

    router.message.register(_entry, Command("withdraw_status", ignore_case=True))
    return router
