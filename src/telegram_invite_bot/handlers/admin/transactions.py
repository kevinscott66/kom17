"""``/admin_transactions`` — read-only tail of the economy ledger.

The legacy admin panel exposes a "view transactions" entry deep inside
its FSM tree, but reaching it during an incident takes 4+ clicks
through inline keyboards — exactly when an operator least wants to
navigate UI. This card answers the one question that comes up first
during a balance-anomaly report: *"what were the last few coin
movements, and is anything obviously wrong?"*

The ``economy.transactions`` table is the canonical ledger: every
``/buy``, every ``/send``, every shop purchase writes a row here with
``from_id`` / ``to_id`` / ``amount`` and a free-text ``reason``. The
card renders the latest 10 rows newest-first so an operator scanning
during an incident sees the most recent activity at the top.

Two writer conventions share this table and the card has to render
both honestly:

* ``amount`` (#1587) — legacy ``record_transaction`` documents it as
  SIGNED (bot.py:10345 — "amount: Сумма (положительная = получение,
  отрицательная = отправка)") and 224 of the 1354 production rows
  still carry a negative amount. The new pipeline writes a POSITIVE
  magnitude and puts direction in the ``from_id``/``to_id`` pair
  alone (the "Sign convention" section of
  :mod:`telegram_invite_bot.repositories.transactions_repo`, and
  ``db/models/economy.py``). One column, two meanings — so the card
  prints the MAGNITUDE and lets the arrow carry direction; echoing
  the raw sign would show two identical spends differently.
* the system side (#1586) — legacy writes the literal ``0``
  (bot.py:10343-10344 — "0 = система"), the new pipeline writes
  NULL. Both mean "no counterparty user" and both render as an
  em-dash; see :func:`_fmt_party`.

Same posture as every other ``/admin_*``:

* Silent-drop for non-devs (existence must not enumerate dev IDs).
* Private-only at the router level — the ledger row exposes
  ``from_id`` / ``to_id`` pairs that, for direct ``/send`` transfers,
  identify who paid whom. Rendering that in a group is a privacy
  leak.
* HTML-escape on ``reason`` — it's user/operator-typed via the legacy
  ``/send`` form. Escaping is load-bearing.
* HTML-escape on ``type`` too — defence-in-depth: the column is set
  by code paths today, but a future admin tool could let an operator
  type a custom transaction type.

10 rows is the same cap ``/withdraw_status`` chose for the same
reason — readable in a single Telegram message, enough to spot a
pattern, small enough that an operator can scan it without scrolling.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.db.models.economy import Transaction
from telegram_invite_bot.db.names import DBName

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.transactions")


_LIMIT = 10

# (id, from_id, to_id, amount, reason, type, date)
_Row = tuple[int, int | None, int | None, int, str | None, str, datetime]


async def _gather(registry: EngineRegistry) -> list[_Row]:
    """Latest 10 rows by ``id`` DESC.

    Same reason as ``/withdraw_status`` for sorting on ``id`` instead
    of ``date``: ``id`` is a monotonic autoincrement, so newest-first
    on it is unambiguous. ``date`` is a real DateTime here (unlike
    ``withdrawal_requests.created_at`` which is TEXT), but legacy
    write paths occasionally insert with ``date`` left NULL or set to
    a placeholder — sorting on ``id`` avoids those edge cases entirely.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                Transaction.id,
                Transaction.from_id,
                Transaction.to_id,
                Transaction.amount,
                Transaction.reason,
                Transaction.type,
                Transaction.date,
            )
            .order_by(Transaction.id.desc())
            .limit(_LIMIT)
        )
        return [
            (
                int(r[0]),
                (int(r[1]) if r[1] is not None else None),
                (int(r[2]) if r[2] is not None else None),
                int(r[3]),
                r[4],
                r[5],
                r[6],
            )
            for r in rows.all()
        ]


def _fmt_party(uid: int | None) -> str:
    """Render a party id. "No counterparty user" → ``"—"``.

    TWO spellings mean that and both are live in the same table
    (#1586). The new pipeline writes NULL. Legacy writes the literal
    ``0``: ``record_transaction`` documents it that way
    (bot.py:10343-10344 — "from_id: ID отправителя (0 = система)")
    and its writers pass it positionally — bot.py:14757 stakes an
    escrow as ``(user_id, 0, -amount, ...)`` and bot.py:14776 refunds
    it as ``(0, user_id, amount, ...)``.

    Handling only NULL was the whole defect: on production 1062 rows
    carry ``from_id=0`` and 227 carry ``to_id=0``, against 44 and 12
    NULLs — so 95% of the ledger rendered its system side as
    ``<code>0</code>``, a plausible-looking user id that an operator
    chasing a balance anomaly would go and look up.

    Folding ``0`` in is safe rather than lucky: ``user_id`` is the
    SQLite rowid, which starts at 1; Telegram never issues id 0; and
    ``economy.users`` has no such row on production. Operators
    recognise the em-dash convention from the other admin cards.
    """
    return "—" if uid in (None, 0) else f"<code>{uid}</code>"


def _render(rows: list[_Row]) -> str:
    if not rows:
        return "💸 <b>Recent transactions</b>\n\n<i>Ledger is empty.</i>"
    lines = ["💸 <b>Recent transactions</b>", ""]
    lines.append(f"<i>Latest {len(rows)} (newest first):</i>")
    lines.append("")
    for rid, frm, to, amount, reason, ttype, dt in rows:
        # Magnitude only: direction is already in the ``from → to``
        # arrow on the same line — and after #1586 that arrow is
        # right for legacy rows too, not just for the NULL-spelled
        # ones. The stored sign is NOT a reliable signal: the new
        # pipeline writes every amount positive while legacy wrote
        # spends negative (224 of the 1354 production rows), so
        # echoing it verbatim would print two identical spends with
        # opposite signs and read as a data bug. The module docstring
        # carries the full convention split (#1587).
        amount_str = str(abs(amount))
        reason_str = html.escape(reason) if reason else "—"
        ttype_str = html.escape(ttype) if ttype else "—"
        # ``date`` is a DateTime in the ORM, but the underlying SQLite
        # column can come back as a naive datetime under aiosqlite. We
        # format with a fixed pattern rather than ``isoformat()`` so
        # the operator sees the same shape regardless of how legacy
        # wrote the row (some legacy paths write str(datetime.now())
        # which round-trips as a naive datetime; others write
        # datetime.utcnow() which produces the same shape).
        date_str = dt.strftime("%Y-%m-%d %H:%M") if dt else "—"
        lines.append(
            f"• <code>#{rid}</code> {_fmt_party(frm)} → {_fmt_party(to)} "
            f"<code>{amount_str}</code> DLAB "
            f"[<i>{ttype_str}</i>] {reason_str} ({date_str})"
        )
    return "\n".join(lines)


async def handle_admin_transactions(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_transactions; silently dropped"
        )
        return
    rows = await _gather(registry)
    await message.answer(_render(rows))
    log.bind(user_id=user.id, count=len(rows)).info("/admin_transactions rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    """Private-only at the router level — see module docstring on the
    from_id/to_id privacy concern."""
    router = Router(name="admin.transactions")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_transactions(message, settings, registry)

    router.message.register(_entry, Command("admin_transactions", ignore_case=True))
    return router
