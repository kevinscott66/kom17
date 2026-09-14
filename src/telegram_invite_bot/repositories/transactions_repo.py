"""Append-only ledger writer for ``economy.transactions``.

Every credit / debit / transfer landed through the new pipeline gets
one row here, mirroring legacy ``record_transaction`` (bot.py:9743 et
al.). The shape matches what legacy writes so:

* ``/mydonates``, ``/cstats``, ``/admin_donations`` and every other
  read-side handler — all of them in this package now — see a uniform
  stream of rows regardless of which pipeline did the write, so a
  ledger that spans the cutover reads as one history.
* Manual SQL audits a developer runs against ``economy.db`` keep
  working without "and also check this other table the new code
  uses" caveats.

The ledger is **append-only** by design. Edits / deletes never happen
through this repo — wrong-amount fixes are done by inserting a
compensating row (``type='admin_set'`` with the corrective delta),
which keeps the row stream a complete history of decisions made.

Sign convention
---------------
``amount`` is the *absolute* magnitude of the move; the direction is
encoded by ``from_id`` (debited) and ``to_id`` (credited). The
legacy code is inconsistent about this — some sites set ``amount``
to a negative number for spends — so the read-side handlers that
SUM amounts have always been read-with-an-asterisk. The new pipeline
writes only positive ``amount`` and leaves the direction to the
``from_id``/``to_id`` pair, which is the saner convention and what
:mod:`telegram_invite_bot.handlers.referrals` already assumes.

Why not part of EconomyRepo
---------------------------
EconomyRepo owns ``economy.users`` (the wallet). The ledger is a
different table with different write semantics (append-only,
insert-only). Keeping them in the same repo class would force every
test that exercises one to wire mocks for the other — and the
service layer composes them anyway. Two small repos > one fat one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select

from telegram_invite_bot.db.models.economy import ProcessedWebhook, Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ``type`` discriminators the TTS (``/voice``) pipeline writes. A
# successful synthesis lands one ``TTS_DEBIT`` row (``from_id=user``);
# a synthesis that fails after the pre-debit is undone by a matching
# ``TTS_REFUND`` credit (``to_id=user``). Both carry the SAME positive
# ``amount`` (the new pipeline writes magnitudes, direction is the
# from_id/to_id pair — see this module's docstring). Netting refunds
# against debits therefore yields the count and coin-cost of the voice
# uses that actually produced audio. The strings are the exact values
# ``services/tts_service.py`` passes to ``EconomyService.debit/credit``.
_TTS_DEBIT = "tts"
_TTS_REFUND = "tts_refund"

# ``type`` discriminator the passive message reward writes
# (``middlewares/message_activity.py``, one row per credited
# grant). Unlike the TTS pair above this one has no refund
# counterpart: a reward the wallet refuses mints nothing and
# writes no row at all, so a plain SUM over these rows is the
# exact number of coins the day actually minted for a user.
_MESSAGE_REWARD = "message_reward"


@dataclass(frozen=True, slots=True)
class VoiceUsageStats:
    """A user's lifetime ``/voice`` (TTS) usage, refunds netted out.

    * ``total`` — successful syntheses: ``tts`` debit rows minus the
      ``tts_refund`` credit rows that undid a failed attempt.
    * ``coins_spent`` — net coins the user actually paid for voice
      (summed debit amounts minus summed refund amounts).
    * ``today`` — successful syntheses on the current UTC calendar day.

    All three are clamped at zero so a (pathological) refund-heavy
    history can never render a negative count in the card.
    """

    total: int
    coins_spent: int
    today: int


@dataclass(frozen=True, slots=True)
class WindowStats:
    """RR-2 #27: a user's cashflow over a time window (the /balance weekly
    block). ``received`` / ``sent`` are coin sums; ``tx_count`` is the number
    of ledger rows the user was party to."""

    received: int
    sent: int
    tx_count: int


@dataclass(frozen=True, slots=True)
class RecentTx:
    """One recent ledger row from the user's perspective (#1/#2 profile
    finances panel). ``signed_amount`` is the coin delta to the user:
    positive when credited (``to_id == user``), negative when debited
    (``from_id == user``). ``reason``/``type`` ride along for the label,
    ``date`` is the stored naive-UTC timestamp."""

    signed_amount: int
    type: str
    reason: str | None
    date: datetime


# ``PaymentsService`` tags every provider top-up ``purchase_<provider>``
# (payments_service.py:241). The prefix is the ledger's only marker that
# real money — not minted coins — funded a credit.
_PURCHASE_TYPE_PREFIX = "purchase_"


class TransactionsRepo:
    """``economy.transactions`` writer + the read-only voice aggregate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def window_stats(self, user_id: int, *, since: datetime) -> WindowStats:
        """Received / sent / transaction-count for ``user_id`` since ``since``.

        Mirrors legacy's /balance weekly block (bot.py:17903). The new
        pipeline stores ``amount`` positive with direction in from/to, so
        ``received`` sums rows crediting the user and ``sent`` sums rows
        debiting them.

        Both sums take ``ABS`` per row, not once over the total. Legacy
        wrote spends negative and those rows are still in prod, so a
        signed ``SUM`` lets a legacy -100 cancel a new +100 inside the
        same week and reports "sent: 0" for a user who spent 200. Summing
        magnitudes makes the number depend on which rows are in the
        window, not on which writer produced them.

        #779: there is deliberately NO type filter, and reversal pairs are
        therefore counted on both sides. A withdrawal that is created and
        then rejected writes ``withdraw_escrow`` (debit) and
        ``withdraw_refund`` (credit) for the same amount, so a 200-coin
        request that never paid out adds 200 to ``sent``, 200 to
        ``received`` and 2 to ``tx_count``. The same shape exists for
        ``duel_refund``, ``pvp_refund``, ``rps_refund``, ``tts_refund``,
        ``couple_activity_refund`` and ``marriage_extend_refund``.

        This is accepted rather than filtered, because filtering is not
        symmetric: nothing links a refund row to the hold it reverses, so
        dropping the credit leg would leave the debit standing and report
        a loss the user never took — strictly worse than reporting both.
        Both legs are real ledger movements and they net to zero, which is
        also how the legacy weekly block behaved for its own stake/refund
        pairs: ``bot.py:10529-10536`` selects ``WHERE (from_id=? OR
        to_id=?) AND date > ...`` with no type predicate either. If this
        ever needs to change, the fix is a reversal link on the row, not a
        type blacklist here.
        """
        received = (
            await self._session.execute(
                select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
                    Transaction.to_id == user_id, Transaction.date >= since
                )
            )
        ).scalar_one()
        sent = (
            await self._session.execute(
                select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
                    Transaction.from_id == user_id, Transaction.date >= since
                )
            )
        ).scalar_one()
        count = (
            await self._session.execute(
                select(func.count()).where(
                    or_(
                        Transaction.from_id == user_id,
                        Transaction.to_id == user_id,
                    ),
                    Transaction.date >= since,
                )
            )
        ).scalar_one()
        return WindowStats(received=int(received), sent=int(sent), tx_count=int(count))

    async def lifetime_deposits(self, user_id: int) -> int:
        """Coins ``user_id`` has ever received by *paying* for them (T-019).

        Every top-up path — Stars, Crypto Pay, YooKassa, Stripe — lands in
        the ledger through ``PaymentsService`` as
        ``type='purchase_<provider>'``, so the ``purchase_`` prefix is the
        one honest signal that real money entered the system on this
        user's behalf. Nothing else in the economy writes that prefix:
        minted coins (daily, referral, promo, message rewards) and
        player-to-player moves all carry their own types.

        Used by the withdrawal gate (``docs/ECONOMY_RATE_AUDIT.md`` R2) to
        tell a customer from an account that has only ever been paid *by*
        the bot. Returns ``0`` for a user with no purchases.

        ``autoescape`` matters here: the prefix ends in ``_``, which LIKE
        reads as "any single character". Unescaped, this would also count
        a hypothetical ``purchaseX…`` type — a money gate must match what
        it says it matches, not one character wider.

        #1400: money the provider TOOK BACK is subtracted. A reversal
        deliberately leaves the wallet alone (the debit is a human
        decision — see ``webhook/payments._alert_reversal``) and writes
        nothing to the ledger at all, so the ``purchase_`` sum above
        still counts a charged-back top-up in full. That funded both
        gates: top up, file a chargeback, keep a raised R2 threshold and
        a raised R6 cap, and repeat. The subtraction happens HERE rather
        than in the caller so neither gate can be wired to the raw
        figure by accident.

        #1988 — read the previous paragraph as narrowly as it is
        written. The subtraction is only as wide as the set of reversals
        that actually STAMP ``processed_webhooks.reversed_at``, and that
        is not every provider:

        * RollyPay and Telegram Stars stamp, so for them the loop above
          is closed. Those are also the two providers live today.
        * YooKassa never stamps. Its alert passes
          ``authenticated=False`` on purpose (#188 — YooKassa signs
          nothing, and an anonymous POST must not be able to mark a
          stranger's credit reversed), so ``stamp`` is false and this
          figure does not move for a YooKassa chargeback.
        * Stripe stamps nothing either, for an unrelated reason: it
          credits under a checkout ``session_id`` and reverses under a
          ``payment_intent`` / ``charge``, two ids that share no
          column, so the lookup finds nothing to stamp.

        Neither SDK is installed (``requirements.txt`` has both
        commented out), so nothing is leaking today — but enabling
        either one reopens the #1400 loop silently, because the code
        that would have to change is in ``webhook/payments.py`` and
        nothing here would fail. That is the trap this paragraph
        exists to spring.

        Tombstone rows (#226 — a reversal that arrived before the credit
        it reverses) carry ``user_id=0`` and ``credited_amount=0``, so
        they cannot move any real user's total.

        Floored at zero, defensively: the minuend is read from the
        ledger and the subtrahend from ``processed_webhooks``, two
        tables with no foreign key between them, so any drift — a
        hand-written credit row, a ledger row pruned by some future
        retention job — must not hand a money gate a negative number,
        which would flip the R2 gate's meaning outright.

        Telegram Stars are NOT an instance of that drift, though this
        docstring used to name them as one. A Stars purchase settles
        without a webhook POST but still writes BOTH rows in the same
        economy transaction: ``purchase_stars`` in the ledger and a
        ``processed_webhooks`` row keyed by the
        ``telegram_payment_charge_id``
        (``handlers/topup.credit_stars_payment`` →
        ``PaymentsService.credit``). Since #1987 a Stars refund stamps
        ``reversed_at`` on that same row, so both sides of the
        subtraction move together for Stars exactly as they do for a
        webhook provider.
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.to_id == user_id,
                Transaction.type.startswith(_PURCHASE_TYPE_PREFIX, autoescape=True),
            )
        )
        return max(0, int(result.scalar_one()) - await self.reversed_credits(user_id))

    async def reversed_credits(self, user_id: int) -> int:
        """Coins credited to ``user_id`` on a deposit that was later
        reversed — the whole deposit, not the reversed part of it.

        The distinction is deliberate and it is not a rounding detail.
        ``ProcessedWebhookRepo.mark_reversed`` records no amount: it
        stamps ``reversed_at`` and nothing else, so the only figure this
        table holds for a reversed row is what was originally credited.
        A partial refund therefore removes the full deposit from the
        gate. RollyPay's ``refund_request.completed``
        (``services/payments/rollypay.py``, :data:`REVERSAL_EVENTS`) can
        be partial, so this is reachable rather than theoretical: a 10%
        refund erases 100% of the credit toward the lifetime gate.

        That direction is the safe one — it under-credits the user and
        never the house — but a reader looking for "how much came back"
        will not find it here, because it is not stored anywhere.

        One indexed read over ``processed_webhooks``
        (``idx_processed_webhooks_user``). Split out from
        :meth:`lifetime_deposits` so the withdrawal desk can show the
        figure on the admin card without re-deriving it, and so a test
        can pin the subtraction and the sum separately.

        Counts credited AMOUNTS, not reversal events: the marker columns
        are set once and overwritten with the same values on a provider
        redelivery (see :class:`ProcessedWebhook`), so a redelivered
        reversal cannot double-count.
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(ProcessedWebhook.credited_amount), 0)).where(
                ProcessedWebhook.user_id == user_id,
                ProcessedWebhook.reversed_at.is_not(None),
            )
        )
        return int(result.scalar_one())

    async def recent(self, user_id: int, *, limit: int = 5) -> list[RecentTx]:
        """The user's most recent ledger rows, newest first (#1/#2 finances
        panel). Each row's ``signed_amount`` is normalised to the user's
        perspective: ``+abs(amount)`` when they were credited (``to_id``),
        ``-abs(amount)`` when debited (``from_id``). Rows where the user is
        both sides (none today) read as a credit.

        The direction comes from the ``from_id``/``to_id`` pair and never
        from the sign of ``amount``, including for the legacy rows that
        carry one (#291). That is safe rather than lucky: legacy wrote a
        negative spend as ``from_id=<spender>, to_id=0``, so it already
        reads as a debit, and all 224 such rows on production have that
        shape. Deriving the direction from the sign instead would break
        the case legacy got right — a negative row credited to someone
        would be reported to the *recipient* as money they paid out.
        """
        rows = (
            await self._session.execute(
                select(
                    Transaction.from_id,
                    Transaction.to_id,
                    Transaction.amount,
                    Transaction.type,
                    Transaction.reason,
                    Transaction.date,
                )
                .where(
                    or_(
                        Transaction.from_id == user_id,
                        Transaction.to_id == user_id,
                    )
                )
                .order_by(Transaction.date.desc(), Transaction.id.desc())
                .limit(limit)
            )
        ).all()
        result: list[RecentTx] = []
        # Not ``date``: the module now imports ``datetime.date`` for the
        # daily-cap bounds below, and a loop variable of that name would
        # shadow it (F402).
        for _from_id, to_id, amount, type_, reason, tx_date in rows:
            magnitude = abs(int(amount))
            signed = magnitude if to_id == user_id else -magnitude
            result.append(RecentTx(signed_amount=signed, type=type_, reason=reason, date=tx_date))
        return result

    async def record(
        self,
        *,
        amount: int,
        type: str,
        from_id: int | None = None,
        to_id: int | None = None,
        reason: str | None = None,
        date: datetime | None = None,
    ) -> None:
        """Append one ledger row.

        All meaningful arguments are keyword-only because the
        ``from_id`` / ``to_id`` pair is easy to swap by accident in
        positional form — and a swapped row would credit the wrong
        wallet on the read side. Forcing keywords at the call site
        also makes the *kind* of move explicit (``to_id=user_id`` for
        a payout, ``from_id=user_id`` for a spend, both set for a
        transfer).

        ``date`` defaults to ``datetime.utcnow()`` so callers don't
        have to construct one. Explicit ``date`` is useful for tests
        and for backfilling missed events, both of which legacy
        supports via the same default.
        """
        row = Transaction(
            from_id=from_id,
            to_id=to_id,
            amount=amount,
            reason=reason,
            type=type,
            date=date or datetime.now(UTC).replace(tzinfo=None),
        )
        self._session.add(row)
        # Flush without commit — the service layer composes this
        # write with the EconomyRepo update inside a single
        # transaction, and the outer commit will land both. Flushing
        # here surfaces constraint errors (e.g. ``type=None``) before
        # the caller returns to the handler.
        await self._session.flush()

    async def voice_usage_stats(
        self, user_id: int, *, now: datetime | None = None
    ) -> VoiceUsageStats:
        """Aggregate a user's ``/voice`` usage, netting refunds. PURE READ.

        A successful synthesis writes one ``type='tts'`` row keyed by
        ``from_id=user_id``; a synthesis that fails after the pre-debit
        is undone by a ``type='tts_refund'`` row keyed by
        ``to_id=user_id`` carrying the same positive ``amount``. So the
        truthful "how many voices did this user actually get" is

            COUNT(tts WHERE from_id=user) - COUNT(tts_refund WHERE to_id=user)

        and the coins truly spent is the analogous SUM-minus-SUM. Each
        side is one indexed aggregate; the two debit/refund halves are
        run as four scalar aggregates (count+sum per type) rather than a
        single CASE-heavy statement so the query stays trivially
        portable across the prod sqlite and the in-memory test DB, and
        the netting is obvious at the call site.

        ``today`` counts only the debit rows whose ``date`` falls in the
        current UTC calendar day; a same-day refund is rare and netting
        it per-day would understate "voices made today" the moment a
        refund from a *previous* day's failure landed — the lifetime
        ``total`` is where refunds belong. ``now`` is injectable for
        deterministic tests.
        """
        now = now or datetime.now(UTC)
        # The ``date`` column stores naive UTC datetimes (the writer does
        # ``datetime.now(UTC).replace(tzinfo=None)``), so compare against
        # naive UTC bounds for today's calendar day.
        today = now.astimezone(UTC).date()
        day_start = datetime.combine(today, time.min)
        # Half-open [day_start, tomorrow_start) avoids the 23:59:59.x edge.
        tomorrow_start = datetime.combine(today + timedelta(days=1), time.min)

        debit_where = Transaction.from_id == user_id
        refund_where = Transaction.to_id == user_id

        debit_count_stmt = (
            select(func.count())
            .select_from(Transaction)
            .where(debit_where)
            .where(Transaction.type == _TTS_DEBIT)
        )
        # ``ABS`` per row for the same reason as ``window_stats``: legacy
        # ``tts`` rows are negative, new ones positive, and a signed SUM
        # over both understates what the user actually spent.
        debit_sum_stmt = (
            select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0))
            .where(debit_where)
            .where(Transaction.type == _TTS_DEBIT)
        )
        refund_count_stmt = (
            select(func.count())
            .select_from(Transaction)
            .where(refund_where)
            .where(Transaction.type == _TTS_REFUND)
        )
        refund_sum_stmt = (
            select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0))
            .where(refund_where)
            .where(Transaction.type == _TTS_REFUND)
        )
        today_count_stmt = (
            select(func.count())
            .select_from(Transaction)
            .where(debit_where)
            .where(Transaction.type == _TTS_DEBIT)
            .where(Transaction.date >= day_start)
            .where(Transaction.date < tomorrow_start)
        )

        debit_count = int((await self._session.execute(debit_count_stmt)).scalar_one())
        debit_sum = int((await self._session.execute(debit_sum_stmt)).scalar_one())
        refund_count = int((await self._session.execute(refund_count_stmt)).scalar_one())
        refund_sum = int((await self._session.execute(refund_sum_stmt)).scalar_one())
        today_count = int((await self._session.execute(today_count_stmt)).scalar_one())

        return VoiceUsageStats(
            total=max(debit_count - refund_count, 0),
            coins_spent=max(debit_sum - refund_sum, 0),
            today=max(today_count, 0),
        )

    async def voice_today_count(self, user_id: int, *, now: datetime | None = None) -> int:
        """Net successful ``/voice`` syntheses on the current UTC day. PURE READ.

        Backs the L-90 daily voice quota. Counts ``type='tts'`` debit
        rows (``from_id=user_id``) whose ``date`` falls in today's UTC
        calendar day, MINUS the ``type='tts_refund'`` credit rows
        (``to_id=user_id``) landed the same day. The refund subtraction
        is what makes this safe to drive a quota off the ledger with no
        migration (per L-90 design guidance: "only add a migration if
        counting transactions is genuinely wrong — e.g. refunds skew"):
        a synthesis that failed after the pre-debit writes a ``tts``
        row AND a same-day ``tts_refund`` row, so the two cancel and a
        failed attempt does NOT burn a quota slot. The user is charged
        nothing for a failed call (``TtsService`` refunds), so it is
        correct that it also costs nothing against the daily allowance.

        Differs from :attr:`VoiceUsageStats.today` (which deliberately
        does NOT net same-day refunds because the *stats card* reports
        "voices attempted today" and nets refunds only into the
        lifetime ``total``). The quota gate has the opposite need:
        count only voices that actually produced audio today. Clamped
        at zero so a stray cross-day refund can never make the count
        negative and hand a user extra free synthesis.

        ``now`` is injectable for deterministic tests.
        """
        now = now or datetime.now(UTC)
        # ``date`` stores naive UTC (writer: ``now(UTC).replace(tzinfo=None)``),
        # so compare against naive UTC day bounds. Half-open interval
        # ``[day_start, tomorrow_start)`` avoids the 23:59:59.x edge.
        today = now.astimezone(UTC).date()
        day_start = datetime.combine(today, time.min)
        tomorrow_start = datetime.combine(today + timedelta(days=1), time.min)

        debit_stmt = (
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.from_id == user_id)
            .where(Transaction.type == _TTS_DEBIT)
            .where(Transaction.date >= day_start)
            .where(Transaction.date < tomorrow_start)
        )
        refund_stmt = (
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.to_id == user_id)
            .where(Transaction.type == _TTS_REFUND)
            .where(Transaction.date >= day_start)
            .where(Transaction.date < tomorrow_start)
        )
        debits = int((await self._session.execute(debit_stmt)).scalar_one())
        refunds = int((await self._session.execute(refund_stmt)).scalar_one())
        return max(debits - refunds, 0)

    async def message_reward_day_total(self, user_id: int, *, day: date, tz: tzinfo) -> int:
        """Coins ``user_id`` already earned from messages on ``day``. PURE READ.

        Backs the T-019 daily cap (#1789). The cap used to live only in
        the middleware's process memory, so every restart handed each
        user the full allowance again — and production deploys land
        dozens of restarts on a busy day, which made a "per calendar
        day" ceiling into a "per deploy interval" one. The ledger is the
        durable record of what was actually minted, so the cap is now
        re-derived from it once per user per day.

        ``day`` is a **local** calendar day in ``tz`` — the middleware
        derives its day from ``StatsConfig.timezone`` so the counter
        agrees with what ``/stats`` and ``/top`` report. ``date`` on the
        other hand stores naive UTC (writer: ``now(UTC).replace(
        tzinfo=None)``), so the local midnight bounds are converted
        explicitly rather than compared as-is: on the MSK production
        host the two differ by three hours, and comparing a local day
        against a UTC column would have moved the reset to 03:00 and
        double-counted the boundary. Half-open ``[day_start,
        next_day_start)`` avoids the 23:59:59.x edge.

        Sums ``amount`` rather than counting rows because a grant is
        clamped to the remaining allowance and boost-multiplied, so
        rows and coins are not interchangeable here. Clamped at zero:
        the column only ever holds positive magnitudes, and a bound
        that could go negative would hand out extra allowance.
        """
        day_start_local = datetime.combine(day, time.min, tzinfo=tz)
        next_day_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        day_start = day_start_local.astimezone(UTC).replace(tzinfo=None)
        next_day_start = next_day_local.astimezone(UTC).replace(tzinfo=None)

        stmt = (
            select(func.coalesce(func.sum(Transaction.amount), 0))
            .where(Transaction.to_id == user_id)
            .where(Transaction.type == _MESSAGE_REWARD)
            .where(Transaction.date >= day_start)
            .where(Transaction.date < next_day_start)
        )
        return max(int((await self._session.execute(stmt)).scalar_one()), 0)
