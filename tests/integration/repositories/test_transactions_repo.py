"""``TransactionsRepo`` read-only ledger aggregates.

Two of them, sharing a ledger fixture: ``voice_usage_stats`` (below) and
``lifetime_deposits`` (at the end of the file, backing the T-019 R2
withdrawal gate).

## ``voice_usage_stats``


The /voice (TTS) pipeline writes one ``type='tts'`` debit row per
attempt (``from_id=user``, positive ``amount``) and, when synthesis
fails after the pre-debit, undoes it with a ``type='tts_refund'`` credit
(``to_id=user``, same magnitude). A *successful* voice is therefore a
``tts`` row NOT cancelled by a matching ``tts_refund`` — so the truthful
count nets refunds against debits, and the coin total does the same.

What we pin here:

* Three debits, one refunded → ``total == 2`` and ``coins_spent`` nets
  the refunded magnitude out.
* Unrelated rows (a ``shop`` spend, another user's ``tts``) never leak
  into the aggregate.
* The zero-state (no rows) returns all-zeros, not a crash.
* ``today`` counts only debits on the injected UTC calendar day; a
  debit from a previous day is in ``total`` but not ``today``.
* A pathological refund-heavy history clamps at zero (never negative).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import ProcessedWebhook, Transaction
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from tests.integration.repositories._session import build_session


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


# The ledger stores naive UTC datetimes (the writer does
# ``datetime.now(UTC).replace(tzinfo=None)``); seed naive to match.
_TODAY = datetime(2026, 6, 5, 12, 0, 0)
_NOW = datetime(2026, 6, 5, 18, 0, 0, tzinfo=UTC)


async def _seed(session: AsyncSession, rows: list[Transaction]) -> None:
    session.add_all(rows)
    await session.commit()


def _tts_debit(user_id: int, amount: int, when: datetime = _TODAY) -> Transaction:
    return Transaction(
        from_id=user_id, amount=amount, type="tts", reason="vip_emoji_voice", date=when
    )


def _tts_refund(user_id: int, amount: int, when: datetime = _TODAY) -> Transaction:
    return Transaction(to_id=user_id, amount=amount, type="tts_refund", reason="fail", date=when)


async def test_zero_state_returns_all_zeros(session: AsyncSession) -> None:
    repo = TransactionsRepo(session)
    stats = await repo.voice_usage_stats(42, now=_NOW)
    assert stats.total == 0
    assert stats.coins_spent == 0
    assert stats.today == 0


async def test_nets_refunds_and_ignores_unrelated_rows(session: AsyncSession) -> None:
    """3 debits @10 − 1 refund @10 → total 2, coins 20. shop + other-user
    rows must not leak."""
    await _seed(
        session,
        [
            _tts_debit(42, 10),
            _tts_debit(42, 10),
            _tts_debit(42, 10),
            _tts_refund(42, 10),
            # Unrelated spend by the same user.
            Transaction(from_id=42, amount=99, type="shop", reason="hat", date=_TODAY),
            # Another user's voice.
            _tts_debit(999, 10),
        ],
    )
    repo = TransactionsRepo(session)
    stats = await repo.voice_usage_stats(42, now=_NOW)
    assert stats.total == 2
    assert stats.coins_spent == 20  # (10*3) - 10
    assert stats.today == 3  # today counts gross debits, refunds excluded


async def test_today_excludes_previous_day_debits(session: AsyncSession) -> None:
    yesterday = datetime(2026, 6, 4, 23, 30, 0)
    await _seed(
        session,
        [
            _tts_debit(7, 5, when=yesterday),
            _tts_debit(7, 5, when=_TODAY),
            _tts_debit(7, 5, when=_TODAY),
        ],
    )
    repo = TransactionsRepo(session)
    stats = await repo.voice_usage_stats(7, now=_NOW)
    assert stats.total == 3  # lifetime
    assert stats.today == 2  # only the two on 2026-06-05
    assert stats.coins_spent == 15


# --- recent() — the #1/#2 profile finances panel reader --------------------


async def test_recent_signs_by_direction_newest_first(session: AsyncSession) -> None:
    """A credit (``to_id``) reads positive, a debit (``from_id``) negative;
    rows come back newest-first and capped at ``limit``."""
    day = datetime(2026, 6, 5, 10, 0, 0)
    await _seed(
        session,
        [
            Transaction(
                to_id=7,
                amount=100,
                type="daily",
                reason="daily",
                date=day.replace(hour=9),
            ),
            Transaction(
                from_id=7,
                amount=40,
                type="shop",
                reason="hat",
                date=day.replace(hour=10),
            ),
            Transaction(
                from_id=7,
                to_id=99,
                amount=25,
                type="transfer",
                reason="gift",
                date=day.replace(hour=11),
            ),
            # Another user's row — must not leak.
            Transaction(to_id=999, amount=500, type="daily", date=day),
        ],
    )
    repo = TransactionsRepo(session)
    rows = await repo.recent(7, limit=5)
    assert [r.signed_amount for r in rows] == [-25, -40, +100]  # newest first
    assert rows[0].reason == "gift"


async def test_recent_respects_limit(session: AsyncSession) -> None:
    day = datetime(2026, 6, 5, 10, 0, 0)
    await _seed(
        session,
        [
            Transaction(to_id=7, amount=i, type="daily", date=day.replace(minute=i))
            for i in range(1, 8)
        ],
    )
    repo = TransactionsRepo(session)
    rows = await repo.recent(7, limit=3)
    assert len(rows) == 3
    # Newest (highest minute → highest amount) first.
    assert [r.signed_amount for r in rows] == [7, 6, 5]


async def test_recent_empty_for_unknown_user(session: AsyncSession) -> None:
    repo = TransactionsRepo(session)
    assert await repo.recent(12345) == []


async def test_refund_heavy_history_clamps_at_zero(session: AsyncSession) -> None:
    """More refunds than debits (shouldn't happen, but defensive): the
    card must never render a negative count or negative coins."""
    await _seed(
        session,
        [
            _tts_debit(3, 10),
            _tts_refund(3, 10),
            _tts_refund(3, 10),
        ],
    )
    repo = TransactionsRepo(session)
    stats = await repo.voice_usage_stats(3, now=_NOW)
    assert stats.total == 0
    assert stats.coins_spent == 0


# ── lifetime_deposits — the R2 withdrawal gate's read side (T-019) ────
#
# The gate refuses a withdrawal unless real money once entered on this
# user's behalf. Everything below pins what counts as "real money": only
# ``purchase_*`` credits *to* this user, and nothing that merely looks
# like one.


def _credit(user_id: int, amount: int, type_: str) -> Transaction:
    return Transaction(to_id=user_id, amount=amount, type=type_, reason="t", date=_TODAY)


async def test_lifetime_deposits_zero_for_an_account_that_never_paid(
    session: AsyncSession,
) -> None:
    """A wallet grown purely from minted coins reads as zero deposits —
    which is what makes the ecosystem un-drainable by grinding."""
    await _seed(
        session,
        [
            _credit(7, 5_000, "daily"),
            _credit(7, 900, "referral"),
            _credit(7, 10_000, "promo"),
            _credit(7, 4_320, "message_reward"),
        ],
    )
    assert await TransactionsRepo(session).lifetime_deposits(7) == 0


async def test_lifetime_deposits_sums_every_provider(session: AsyncSession) -> None:
    """``PaymentsService`` writes ``purchase_<provider>``; all providers
    count toward the same lifetime total."""
    await _seed(
        session,
        [
            _credit(7, 4_500, "purchase_crypto"),
            _credit(7, 9_000, "purchase_stars"),
            _credit(7, 900, "purchase_yookassa"),
            _credit(7, 450, "purchase_stripe"),
        ],
    )
    assert await TransactionsRepo(session).lifetime_deposits(7) == 14_850


async def test_lifetime_deposits_ignores_another_users_purchase(
    session: AsyncSession,
) -> None:
    """Someone else's top-up must not unlock this account's payouts."""
    await _seed(session, [_credit(8, 90_000, "purchase_crypto")])
    assert await TransactionsRepo(session).lifetime_deposits(7) == 0


async def test_lifetime_deposits_ignores_a_purchase_the_user_paid_out(
    session: AsyncSession,
) -> None:
    """Only credits count. A ``purchase_*`` row where the user is the
    *sender* is money leaving, and must not read as a deposit."""
    await _seed(
        session,
        [Transaction(from_id=7, amount=9_000, type="purchase_crypto", reason="t", date=_TODAY)],
    )
    assert await TransactionsRepo(session).lifetime_deposits(7) == 0


async def test_lifetime_deposits_does_not_match_one_char_wider(
    session: AsyncSession,
) -> None:
    """The prefix ends in ``_``, LIKE's single-character wildcard. Without
    ``autoescape`` this row would be counted as a deposit and open the
    withdrawal gate on a type nobody audited."""
    await _seed(session, [_credit(7, 90_000, "purchaseX_not_a_real_topup")])
    assert await TransactionsRepo(session).lifetime_deposits(7) == 0


# ── lifetime_deposits vs chargebacks (#1400) ──────────────────────────
#
# A reversal stamps ``processed_webhooks.reversed_at`` and writes
# nothing to the ledger, so the ``purchase_`` sum above cannot see it on
# its own. Left alone that made the gate self-refilling: top up, charge
# back, keep the raised R2 threshold and R6 cap, repeat. These pin the
# subtraction and, just as importantly, everything it must NOT subtract.


def _webhook(
    user_id: int,
    amount: int,
    *,
    external_id: str,
    reversed_: bool,
) -> ProcessedWebhook:
    return ProcessedWebhook(
        provider="crypto",
        external_id=external_id,
        user_id=user_id,
        credited_amount=amount,
        processed_at=_TODAY,
        reversed_at=_TODAY if reversed_ else None,
        reversed_event="invoice_reversed" if reversed_ else None,
    )


async def _seed_webhooks(session: AsyncSession, rows: list[ProcessedWebhook]) -> None:
    session.add_all(rows)
    await session.commit()


async def test_lifetime_deposits_subtracts_a_charged_back_top_up(
    session: AsyncSession,
) -> None:
    """The money came back out, so it stops funding the payout gates."""
    await _seed(
        session,
        [_credit(7, 9_000, "purchase_crypto"), _credit(7, 4_500, "purchase_crypto")],
    )
    await _seed_webhooks(session, [_webhook(7, 4_500, external_id="inv-2", reversed_=True)])
    assert await TransactionsRepo(session).lifetime_deposits(7) == 9_000


async def test_lifetime_deposits_keeps_a_settled_top_up(
    session: AsyncSession,
) -> None:
    """Only ``reversed_at`` subtracts. An ordinary idempotency row is the
    record of a payment that stuck, and must leave the total alone."""
    await _seed(session, [_credit(7, 9_000, "purchase_crypto")])
    await _seed_webhooks(session, [_webhook(7, 9_000, external_id="inv-1", reversed_=False)])
    assert await TransactionsRepo(session).lifetime_deposits(7) == 9_000


async def test_lifetime_deposits_ignores_another_users_chargeback(
    session: AsyncSession,
) -> None:
    """Someone else's chargeback must not close this account's gate."""
    await _seed(session, [_credit(7, 9_000, "purchase_crypto")])
    await _seed_webhooks(session, [_webhook(8, 9_000, external_id="inv-3", reversed_=True)])
    assert await TransactionsRepo(session).lifetime_deposits(7) == 9_000


async def test_lifetime_deposits_is_unmoved_by_a_tombstone(
    session: AsyncSession,
) -> None:
    """#226 writes ``user_id=0, credited_amount=0`` when a reversal
    arrives before the credit it reverses. It is stamped reversed, so it
    is inside the subtraction's filter — and must still be worth zero."""
    await _seed(session, [_credit(7, 9_000, "purchase_crypto")])
    await _seed_webhooks(
        session,
        [
            _webhook(0, 0, external_id="inv-orphan", reversed_=True),
            _webhook(7, 0, external_id="inv-zero", reversed_=True),
        ],
    )
    assert await TransactionsRepo(session).lifetime_deposits(7) == 9_000


async def test_lifetime_deposits_floors_at_zero(session: AsyncSession) -> None:
    """Stars settle without a webhook row, so reversals on record can
    legitimately exceed ledger purchases. A negative total would read to
    the R2 gate as "less than nothing paid" and to R6 as a negative cap;
    zero is the honest floor."""
    await _seed(session, [_credit(7, 900, "purchase_crypto")])
    await _seed_webhooks(session, [_webhook(7, 9_000, external_id="inv-4", reversed_=True)])
    assert await TransactionsRepo(session).lifetime_deposits(7) == 0


async def test_reversed_credits_sums_only_reversed_rows_of_this_user(
    session: AsyncSession,
) -> None:
    """The figure the withdrawal desk shows, pinned on its own so a
    regression in the subtraction cannot hide behind the purchase sum."""
    await _seed_webhooks(
        session,
        [
            _webhook(7, 9_000, external_id="inv-a", reversed_=True),
            _webhook(7, 450, external_id="inv-b", reversed_=True),
            _webhook(7, 90_000, external_id="inv-c", reversed_=False),
            _webhook(8, 90_000, external_id="inv-d", reversed_=True),
        ],
    )
    repo = TransactionsRepo(session)
    assert await repo.reversed_credits(7) == 9_450
    assert await repo.reversed_credits(9) == 0


# ---------------------------------------------------------------------------
# ``window_stats`` — /balance's weekly cashflow block.
#
# Prod's ledger is mixed: legacy bot.py wrote spends negative, the new
# pipeline writes every row positive with the direction in from/to. The
# aggregate must read the same magnitude out of both.
# ---------------------------------------------------------------------------

_WINDOW_SINCE = datetime(2026, 6, 1, 0, 0, 0)


async def test_window_stats_mixed_signs_do_not_cancel(session: AsyncSession) -> None:
    """A legacy negative spend used to cancel a new positive one.

    ``sent`` was ``abs(SUM(amount))`` — one abs over the total, not per
    row — so a legacy ``-100`` and a new ``+100`` in the same week summed
    to zero and /balance reported "sent: 0" to a user who spent 200.
    """
    await _seed(
        session,
        [
            Transaction(  # legacy /buy row: spend stored negative
                from_id=7, to_id=0, amount=-100, type="shop", reason="legacy", date=_TODAY
            ),
            Transaction(  # new pipeline: same spend, stored positive
                from_id=7, amount=100, type="couple_activity", reason="new", date=_TODAY
            ),
        ],
    )
    stats = await TransactionsRepo(session).window_stats(7, since=_WINDOW_SINCE)
    assert stats.sent == 200
    assert stats.tx_count == 2


async def test_window_stats_counts_both_directions(session: AsyncSession) -> None:
    """Received and sent are read off ``to_id``/``from_id``, and a row
    outside the window is excluded from all three numbers."""
    await _seed(
        session,
        [
            Transaction(to_id=7, amount=250, type="transfer", reason="in", date=_TODAY),
            Transaction(from_id=7, amount=40, type="tts", reason="out", date=_TODAY),
            Transaction(  # before the window → invisible
                from_id=7,
                amount=999,
                type="shop",
                reason="old",
                date=datetime(2026, 5, 1, 12, 0, 0),
            ),
        ],
    )
    stats = await TransactionsRepo(session).window_stats(7, since=_WINDOW_SINCE)
    assert stats.received == 250
    assert stats.sent == 40
    assert stats.tx_count == 2


async def test_voice_stats_coins_spent_counts_a_legacy_negative_row(
    session: AsyncSession,
) -> None:
    """Same fix on the /voice card: a legacy negative ``tts`` row must
    add to what the user spent, not subtract from it."""
    await _seed(
        session,
        [
            Transaction(from_id=7, amount=-10, type="tts", reason="legacy", date=_TODAY),
            _tts_debit(7, 10),
        ],
    )
    stats = await TransactionsRepo(session).voice_usage_stats(7, now=_NOW)
    assert stats.total == 2
    assert stats.coins_spent == 20


async def test_recent_reads_a_legacy_negative_spend_as_a_spend(
    session: AsyncSession,
) -> None:
    """#291: the shape legacy actually wrote, pinned against a re-file.

    #291 claimed the finances panel renders legacy negative rows as
    *income*. It does not, and the reason is worth a test rather than a
    verdict: every one of the 224 negative rows on production is
    ``from_id=<spender>, to_id=0``, so the direction is already correct
    and ``abs()`` only strips a redundant sign. The one row shape that
    would misread — a negative amount credited to ``to_id=user`` — has
    never been written by either pipeline.

    The temptation the ticket describes (flip the sign whenever
    ``amount < 0``) is the thing this test exists to block: applied to a
    legacy *transfer* written negative, it would tell the recipient they
    had paid money they in fact received.
    """
    await _seed(
        session,
        [
            # Legacy spend, verbatim prod shape (economy.db id=3).
            Transaction(
                from_id=7,
                to_id=0,
                amount=-500,
                type="shop",
                reason="Покупка: 🌈 Цветной ник",
                date=_TODAY,
            ),
            # Port spend, same event, modern shape.
            Transaction(from_id=7, to_id=0, amount=500, type="shop", reason="new", date=_TODAY),
            # A credit, so a blanket sign flip would be visibly wrong.
            Transaction(from_id=0, to_id=7, amount=250, type="daily", reason="in", date=_TODAY),
        ],
    )
    rows = await TransactionsRepo(session).recent(7, limit=10)
    assert sorted(r.signed_amount for r in rows) == [-500, -500, 250]


# --------------------------------------------------------------------------
# ``message_reward_day_total`` — the durable half of the T-019 daily cap
# (#1789). The middleware's calendar day comes from ``StatsConfig.timezone``
# (MSK on production) while ``Transaction.date`` is naive UTC, so the
# boundary cases below are the point of the method, not decoration.
# --------------------------------------------------------------------------

_MSK = timezone(timedelta(hours=3))


def _reward(user_id: int, amount: int, when: datetime) -> Transaction:
    return Transaction(
        from_id=None,
        to_id=user_id,
        amount=amount,
        type="message_reward",
        reason="message reward",
        date=when,
    )


async def test_message_reward_day_total_is_zero_without_rows(session: AsyncSession) -> None:
    repo = TransactionsRepo(session)
    assert await repo.message_reward_day_total(7, day=date(2026, 6, 5), tz=UTC) == 0


async def test_message_reward_day_total_sums_coins_not_rows(session: AsyncSession) -> None:
    """Grants are boost-multiplied and remainder-clamped, so rows != coins."""
    await _seed(
        session,
        [
            _reward(7, 3, datetime(2026, 6, 5, 9, 0)),
            _reward(7, 6, datetime(2026, 6, 5, 10, 0)),
            _reward(7, 1, datetime(2026, 6, 5, 23, 59, 59)),
        ],
    )
    repo = TransactionsRepo(session)
    assert await repo.message_reward_day_total(7, day=date(2026, 6, 5), tz=UTC) == 10


async def test_message_reward_day_total_ignores_other_users_and_types(
    session: AsyncSession,
) -> None:
    """Only this user's own message rewards count towards this user's cap."""
    await _seed(
        session,
        [
            _reward(7, 5, _TODAY),
            _reward(8, 90, _TODAY),
            Transaction(to_id=7, amount=500, type="daily", reason="d", date=_TODAY),
            Transaction(to_id=7, amount=250, type="promo", reason="p", date=_TODAY),
            # Legacy monolith rows: same human meaning, different ``type``.
            # They predate the port and must not eat today's allowance.
            Transaction(
                to_id=7, amount=898, type="system", reason="За сообщение в чате", date=_TODAY
            ),
        ],
    )
    repo = TransactionsRepo(session)
    assert await repo.message_reward_day_total(7, day=date(2026, 6, 5), tz=UTC) == 5


async def test_message_reward_day_total_excludes_neighbouring_days(
    session: AsyncSession,
) -> None:
    """Half-open ``[start, next_start)``: no double-count at the boundary."""
    await _seed(
        session,
        [
            _reward(7, 11, datetime(2026, 6, 4, 23, 59, 59)),
            _reward(7, 22, datetime(2026, 6, 5, 0, 0, 0)),
            _reward(7, 33, datetime(2026, 6, 5, 23, 59, 59)),
            _reward(7, 44, datetime(2026, 6, 6, 0, 0, 0)),
        ],
    )
    repo = TransactionsRepo(session)
    assert await repo.message_reward_day_total(7, day=date(2026, 6, 5), tz=UTC) == 55


async def test_message_reward_day_total_reads_the_local_day_not_the_utc_one(
    session: AsyncSession,
) -> None:
    """The whole reason the timezone is a parameter (#1789).

    On the MSK production host the local day starts at 21:00 UTC the
    day before. Comparing the middleware's local date against the naive
    UTC column without converting would move the reset to 03:00 local
    and count three hours of the wrong day — handing a capped grinder a
    second allowance every night, which is the defect in miniature.
    """
    await _seed(
        session,
        [
            # 20:59 UTC on the 4th = 23:59 MSK on the 4th — yesterday.
            _reward(7, 100, datetime(2026, 6, 4, 20, 59, 0)),
            # 21:00 UTC on the 4th = 00:00 MSK on the 5th — today.
            _reward(7, 7, datetime(2026, 6, 4, 21, 0, 0)),
            # 20:59 UTC on the 5th = 23:59 MSK on the 5th — today.
            _reward(7, 8, datetime(2026, 6, 5, 20, 59, 0)),
            # 21:00 UTC on the 5th = 00:00 MSK on the 6th — tomorrow.
            _reward(7, 200, datetime(2026, 6, 5, 21, 0, 0)),
        ],
    )
    repo = TransactionsRepo(session)
    assert await repo.message_reward_day_total(7, day=date(2026, 6, 5), tz=_MSK) == 15
    # The same rows read as a UTC day pick a different set entirely.
    assert await repo.message_reward_day_total(7, day=date(2026, 6, 5), tz=UTC) == 208
