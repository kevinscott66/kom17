"""``EconomyRepo.get_or_create`` against a real SQLite file.

The Stage 7 surface is small — one method — so the test set is small
too. Schema parity (this model vs ``docs/prod_schemas.sql``) is
covered separately by Alembic + the dump diff, same as ``UsersRepo``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[EconomyRepo, AsyncSession]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield EconomyRepo(session), session


async def test_get_or_create_seeds_wallet_with_legacy_default(
    repo: RepoFixture,
) -> None:
    economy_repo, session = repo
    wallet = await economy_repo.get_or_create(user_id=42, now=datetime(2024, 1, 1, 12, 0, 0))
    await session.commit()

    # Legacy welcome credit — must NOT diverge from telebot's default.
    assert wallet.balance == 100
    assert wallet.user_id == 42
    assert wallet.daily_streak == 0
    assert wallet.last_daily is None
    assert wallet.language == "ru"
    assert wallet.total_earned == 0
    assert wallet.total_spent == 0


async def test_get_or_create_is_idempotent(repo: RepoFixture) -> None:
    economy_repo, session = repo
    first = await economy_repo.get_or_create(user_id=42)
    await session.commit()
    second = await economy_repo.get_or_create(user_id=42)
    await session.commit()

    assert first.balance == second.balance == 100
    # Same row, no duplicates: the count probe still finds exactly one.
    from sqlalchemy import func, select

    from telegram_invite_bot.db.models.economy import EconomyUser

    count = await session.execute(
        select(func.count()).select_from(EconomyUser).where(EconomyUser.user_id == 42)
    )
    assert count.scalar() == 1


async def test_get_or_create_does_not_clobber_existing_balance(
    repo: RepoFixture,
) -> None:
    """Re-seed must NOT reset balance to 100 if the row already exists."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=42)
    # Pretend the user earned some coins via legacy.
    from telegram_invite_bot.db.models.economy import EconomyUser

    row = await session.get(EconomyUser, 42)
    assert row is not None
    row.balance = 999
    await session.commit()

    refreshed = await economy_repo.get_or_create(user_id=42)
    assert refreshed.balance == 999  # untouched


async def test_get_returns_none_for_missing(repo: RepoFixture) -> None:
    economy_repo, _ = repo
    assert await economy_repo.get(99999) is None


async def test_get_after_seed_returns_entity(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()
    fetched = await economy_repo.get(7)
    assert fetched is not None
    assert fetched.user_id == 7
    assert fetched.balance == 100


# ---------------------------------------------------------------------------
# Stage 8 — credit / debit / set_balance
# ---------------------------------------------------------------------------


async def test_credit_increases_balance_and_total_earned(repo: RepoFixture) -> None:
    """``credit`` is the canonical UPDATE-RETURNING path. Both
    ``balance`` and ``total_earned`` move together — they must
    not drift because legacy users see ``total_earned`` on their
    profile card and expect it to be the sum of every credit they
    ever received."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    updated = await economy_repo.credit(7, 250)
    await session.commit()

    assert updated is not None
    assert updated.balance == 350  # 100 welcome + 250 credit
    assert updated.total_earned == 250  # was 0, now 250


async def test_credit_returns_none_for_missing_wallet(repo: RepoFixture) -> None:
    """No wallet → no rows match the WHERE → ``None``. Caller must
    ``get_or_create`` first if creating-on-credit is the right
    semantics; here we don't auto-create because most credit paths
    are payouts to known users and an auto-create would mask
    "credit to a phantom user_id" bugs."""
    economy_repo, _ = repo
    assert await economy_repo.credit(99999, 100) is None


@pytest.mark.parametrize("amount", [0, -1, -50])
async def test_credit_refuses_a_non_positive_amount(repo: RepoFixture, amount: int) -> None:
    """#771: a negative ``amount`` must not reach the UPDATE.

    ``balance = balance + amount`` with a negative ``amount`` is a
    subtraction, and this statement carries no ``balance >= amount``
    guard — that check lives in :meth:`debit`. So without the backstop a
    caller bug would silently overdraw the wallet *and* walk
    ``total_earned`` backwards, which the profile card renders as the
    lifetime sum of every credit. Zero is refused with it: an UPDATE
    that changes nothing should not report success.
    """
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    assert await economy_repo.credit(7, amount) is None

    wallet = await economy_repo.get(7)
    assert wallet is not None
    assert wallet.balance == 100
    assert wallet.total_earned == 0


async def test_debit_subtracts_when_affordable(repo: RepoFixture) -> None:
    economy_repo, session = repo
    wallet = await economy_repo.get_or_create(user_id=7)
    await session.commit()
    assert wallet.balance == 100

    updated = await economy_repo.debit(7, 30)
    await session.commit()

    assert updated is not None
    assert updated.balance == 70
    assert updated.total_spent == 30


async def test_debit_returns_none_for_insufficient_funds(repo: RepoFixture) -> None:
    """The load-bearing race-safety guard: ``WHERE balance >= amount``.
    A debit larger than balance must NOT touch the row (no partial
    spend, no negative balance) and must signal failure to the caller."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    result = await economy_repo.debit(7, 9999)
    await session.commit()

    assert result is None

    # Confirm row was untouched — no partial spend, no spurious
    # total_spent bump.
    after = await economy_repo.get(7)
    assert after is not None
    assert after.balance == 100
    assert after.total_spent == 0


async def test_debit_at_exact_balance_succeeds_and_leaves_zero(repo: RepoFixture) -> None:
    """The ``>=`` guard (not ``>``) must allow debiting the full
    balance. A wallet sitting at exactly 0 is the legal post-state
    of "spent everything", per ``validate_balance_target``."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    updated = await economy_repo.debit(7, 100)
    await session.commit()

    assert updated is not None
    assert updated.balance == 0
    assert updated.total_spent == 100


async def test_debit_returns_none_for_missing_wallet(repo: RepoFixture) -> None:
    economy_repo, _ = repo
    assert await economy_repo.debit(99999, 1) is None


async def test_set_balance_overrides_value(repo: RepoFixture) -> None:
    """Admin-style direct set. ``total_earned`` / ``total_spent``
    deliberately UNTOUCHED — the counters need the *old* balance to
    compute a delta from, so the service composes this with
    ``bump_totals`` instead (#257). Keeping them apart is also what
    lets the suite use ``set_balance`` as a balance-seeding fixture
    without inflating anyone's lifetime earnings."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    updated = await economy_repo.set_balance(7, 5_000)
    await session.commit()

    assert updated is not None
    assert updated.balance == 5_000
    assert updated.total_earned == 0  # untouched
    assert updated.total_spent == 0  # untouched


async def test_set_balance_returns_none_for_missing(repo: RepoFixture) -> None:
    economy_repo, _ = repo
    assert await economy_repo.set_balance(99999, 100) is None


async def test_bump_totals_moves_counters_without_touching_balance(
    repo: RepoFixture,
) -> None:
    """#257: the mirror image of ``set_balance`` — history, not money."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.set_balance(7, 5_000)
    await session.commit()

    updated = await economy_repo.bump_totals(7, earned=300, spent=120)
    await session.commit()

    assert updated is not None
    assert updated.balance == 5_000, "balance is not this method's business"
    assert updated.total_earned == 300
    assert updated.total_spent == 120


async def test_bump_totals_accumulates_across_calls(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    await economy_repo.bump_totals(7, earned=100)
    updated = await economy_repo.bump_totals(7, spent=40)
    await session.commit()

    assert updated is not None
    assert updated.total_earned == 100
    assert updated.total_spent == 40


async def test_bump_totals_rejects_negative_magnitudes(repo: RepoFixture) -> None:
    """A lifetime counter only ever goes up. A negative argument means
    the caller passed a signed delta where a magnitude was expected,
    and letting it through would quietly rewrite history."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.bump_totals(7, earned=500)
    await session.commit()

    assert await economy_repo.bump_totals(7, earned=-1) is None
    assert await economy_repo.bump_totals(7, spent=-1) is None
    await session.commit()

    wallet = await economy_repo.get(7)
    assert wallet is not None
    assert wallet.total_earned == 500, "the rejected calls changed nothing"
    assert wallet.total_spent == 0


async def test_bump_totals_returns_none_for_missing_wallet(repo: RepoFixture) -> None:
    economy_repo, _ = repo
    assert await economy_repo.bump_totals(99999, earned=10) is None


async def test_credit_then_debit_round_trips_cleanly(repo: RepoFixture) -> None:
    """Sequential operations preserve invariants: balance ends up
    where the arithmetic says, total_earned/total_spent both grow
    monotonically. If a later refactor accidentally swapped one of
    the column updates this test would catch it."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    await economy_repo.credit(7, 500)
    await session.commit()
    await economy_repo.debit(7, 300)
    await session.commit()
    final = await economy_repo.credit(7, 50)
    await session.commit()

    assert final is not None
    assert final.balance == 350  # 100 + 500 - 300 + 50
    assert final.total_earned == 550  # 500 + 50
    assert final.total_spent == 300


async def test_credit_rejected_when_resulting_balance_exceeds_cap(
    repo: RepoFixture,
) -> None:
    """M-E-3: ``_MAX_AMOUNT`` is a *balance* ceiling, not a per-delta one.

    Pre-fix behaviour (audit 01_economy.md M-E-3):
    ``validate_credit_amount`` rejects deltas larger than
    ``_MAX_AMOUNT``, but the repo's UPDATE has no balance ceiling —
    so a wallet at ``_MAX_AMOUNT - 1`` could accept any number of
    further legal-sized credits and drift past the documented
    invariant indefinitely. The float-cast safety the cap claims to
    defend would then start losing low-order digits.

    Post-fix: the credit's WHERE clause carries the
    ``balance + amount <= _MAX_AMOUNT`` ceiling, so:

    * crediting from ``cap - 10`` by ``5`` succeeds and lands at
      ``cap - 5`` (under the cap);
    * a second credit by ``10`` would overshoot to ``cap + 5`` and
      is rejected with ``None`` (no mutation);
    * the wallet stays at ``cap - 5``, not at the overflowed value.
    """
    from telegram_invite_bot.utils.economy import _MAX_AMOUNT

    economy_repo, session = repo
    # Seed a wallet just under the cap using ``set_balance`` (it
    # bypasses ``credit``'s validation, mirroring the legacy
    # admin-set flow that could put any value into the column).
    await economy_repo.get_or_create(user_id=7)
    await session.commit()
    near_cap = _MAX_AMOUNT - 10
    await economy_repo.set_balance(7, near_cap)
    await session.commit()

    # First credit fits under the cap → succeeds.
    after_first = await economy_repo.credit(7, 5)
    assert after_first is not None
    assert after_first.balance == near_cap + 5

    # Second credit would push the post-write balance past the cap
    # → rejected at the SQL guard, no mutation.
    overflow = await economy_repo.credit(7, 10)
    assert overflow is None

    # Balance unchanged after the rejected credit.
    refetched = await economy_repo.get(7)
    assert refetched is not None
    assert refetched.balance == near_cap + 5


async def test_concurrent_debits_cannot_overspend(repo: RepoFixture) -> None:
    """The whole point of the ``WHERE balance >= amount`` guard.

    Single-threaded simulation: do two back-to-back debit calls of
    60 each against a wallet of 100. The first succeeds (balance →
    40); the second must see ``balance < 60`` and return None — no
    "we already validated, now write" race. The legacy code needed
    a threading.Lock for this; the SQL guard makes the lock
    unnecessary in the new pipeline."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    first = await economy_repo.debit(7, 60)
    await session.commit()
    second = await economy_repo.debit(7, 60)
    await session.commit()

    assert first is not None
    assert first.balance == 40
    assert second is None  # blocked by WHERE clause, not by Python check

    final = await economy_repo.get(7)
    assert final is not None
    assert final.balance == 40
    assert final.total_spent == 60  # only the first debit counted


# ---------------------------------------------------------------------------
# mark_daily_claimed — guarded UPDATE for /daily race-safety
# ---------------------------------------------------------------------------


async def test_mark_daily_claimed_first_ever_claim_succeeds(repo: RepoFixture) -> None:
    """``last_daily IS NULL`` short-circuits the julianday guard so
    the first-ever claim always passes."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    now = datetime(2024, 1, 1, 12, 0, 0)
    updated = await economy_repo.mark_daily_claimed(7, now=now, new_streak=1)
    await session.commit()

    assert updated is not None
    assert updated.last_daily == now
    assert updated.daily_streak == 1


async def test_mark_daily_claimed_second_attempt_within_24h_rejected(
    repo: RepoFixture,
) -> None:
    """The load-bearing race guard: a second claim 23h59m after the
    first must NOT pass — even if the caller's Python cooldown check
    said it should. ``julianday(now) - julianday(last) >= 1`` rejects
    the UPDATE; rowcount=0 means None."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    first = datetime(2024, 1, 1, 12, 0, 0)
    await economy_repo.mark_daily_claimed(7, now=first, new_streak=1)
    await session.commit()

    second = datetime(2024, 1, 2, 11, 59, 59)  # 23h 59m 59s later
    result = await economy_repo.mark_daily_claimed(7, now=second, new_streak=2)
    await session.commit()

    assert result is None
    # last_daily UNCHANGED — caller can compute correct cooldown
    # remaining without worrying about a partial write.
    wallet = await economy_repo.get(7)
    assert wallet is not None
    assert wallet.last_daily == first
    assert wallet.daily_streak == 1


async def test_mark_daily_claimed_after_24h_succeeds_with_new_streak(
    repo: RepoFixture,
) -> None:
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    first = datetime(2024, 1, 1, 12, 0, 0)
    await economy_repo.mark_daily_claimed(7, now=first, new_streak=1)
    await session.commit()

    next_day = datetime(2024, 1, 2, 12, 0, 1)  # just past 24h
    updated = await economy_repo.mark_daily_claimed(7, now=next_day, new_streak=2)
    await session.commit()

    assert updated is not None
    assert updated.last_daily == next_day
    assert updated.daily_streak == 2


async def test_mark_daily_claimed_returns_none_for_missing_wallet(
    repo: RepoFixture,
) -> None:
    economy_repo, _ = repo
    assert (
        await economy_repo.mark_daily_claimed(99999, now=datetime(2024, 1, 1), new_streak=1) is None
    )


# ---------------------------------------------------------------------------
# Stage 21 — top_by_balance (read for /top balance handler)
# ---------------------------------------------------------------------------


async def test_top_by_balance_empty_table_returns_empty_list(
    repo: RepoFixture,
) -> None:
    """No wallets → ``[]``, NOT an exception. The /top handler treats
    an empty list as the empty-state copy; if this raised, every
    fresh deployment would 500 on the first ``/top balance`` call."""
    economy_repo, _ = repo
    assert await economy_repo.top_by_balance() == []


async def test_top_by_balance_returns_single_wallet(repo: RepoFixture) -> None:
    """Single positive-balance wallet → single (uid, balance) tuple.
    Exercises the happy path and confirms the tuple shape that the
    handler unpacks."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=42)
    await session.commit()

    rows = await economy_repo.top_by_balance()
    assert rows == [(42, 100)]


async def test_top_by_balance_orders_by_balance_then_user_id(
    repo: RepoFixture,
) -> None:
    """Sort key: ``balance DESC, user_id ASC``. The user_id tiebreaker
    is the load-bearing bit — without it, two equal-balance wallets
    can swap places between calls and any test asserting an ordering
    on a tied seed becomes flaky. Seed three wallets where two share
    a balance to pin both keys at once."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=1)
    await economy_repo.get_or_create(user_id=2)
    await economy_repo.get_or_create(user_id=3)
    # Wallets 2 and 3 share balance=500; wallet 1 sits below at 200.
    await economy_repo.set_balance(2, 500)
    await economy_repo.set_balance(3, 500)
    await economy_repo.set_balance(1, 200)
    await session.commit()

    rows = await economy_repo.top_by_balance()
    # Tie broken by ``user_id ASC`` → uid 2 before uid 3.
    assert rows == [(2, 500), (3, 500), (1, 200)]


async def test_top_by_balance_excludes_zero_and_negative(repo: RepoFixture) -> None:
    """``WHERE balance > 0`` filters ghost wallets. A user who spent
    every coin is technically still a row in ``economy.users`` but
    shouldn't fill the leaderboard with rows tied at zero — that's
    the noise this filter removes (see docstring on the repo method
    for the legacy parity note)."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=1)
    await economy_repo.get_or_create(user_id=2)
    await economy_repo.get_or_create(user_id=3)
    await economy_repo.set_balance(1, 500)
    await economy_repo.set_balance(2, 0)
    # Negative shouldn't be reachable via validated paths, but the
    # column is signed and a legacy admin override could land one —
    # the filter has to catch ``<= 0``, not just ``== 0``.
    await economy_repo.set_balance(3, -10)
    await session.commit()

    rows = await economy_repo.top_by_balance()
    assert rows == [(1, 500)]


async def test_top_by_balance_respects_limit(repo: RepoFixture) -> None:
    """``limit`` honoured: seed 5 wallets, ask for 2, get exactly the
    top 2. The clamp lives in the repo (``1..50``); a value inside
    that range must pass through unchanged."""
    economy_repo, session = repo
    for uid, bal in [(10, 100), (20, 200), (30, 300), (40, 400), (50, 500)]:
        await economy_repo.get_or_create(user_id=uid)
        await economy_repo.set_balance(uid, bal)
    await session.commit()

    rows = await economy_repo.top_by_balance(limit=2)
    assert rows == [(50, 500), (40, 400)]


async def test_top_by_balance_clamps_oversize_limit(repo: RepoFixture) -> None:
    """``limit > 50`` clamps to 50, ``limit < 1`` clamps to 1. The
    clamp is defensive — a caller passing ``limit=0`` would otherwise
    produce an empty list indistinguishable from "no wallets", and
    ``limit=1000`` would bust Telegram's 4096-char body cap on a busy
    server. Both are caller bugs we fail soft on."""
    economy_repo, session = repo
    for uid in range(1, 4):
        await economy_repo.get_or_create(user_id=uid)
        await economy_repo.set_balance(uid, 100 + uid)
    await session.commit()

    # Below clamp: ``limit=0`` → 1 row, not 0.
    rows_zero = await economy_repo.top_by_balance(limit=0)
    assert len(rows_zero) == 1
    # Above clamp: huge limit returns at most ``min(rows, 50)``.
    rows_huge = await economy_repo.top_by_balance(limit=10_000)
    assert len(rows_huge) == 3  # only 3 seeded; clamp didn't add ghosts


# ---------------------------------------------------------------------------
# T-016 — top_by_games / top_by_wins / top_by_streak
# ---------------------------------------------------------------------------


async def _seed_stats(
    economy_repo: EconomyRepo,
    session: AsyncSession,
    rows: list[tuple[int, int, int, int]],
) -> None:
    """Seed (user_id, games_played, games_won, daily_streak) rows.

    ``get_or_create`` only sets the bootstrap shape; the three
    aggregate columns get set directly so the test owns the values
    end-to-end without going through legacy writers.
    """
    from telegram_invite_bot.db.models.economy import EconomyUser

    for uid, gp, gw, ds in rows:
        await economy_repo.get_or_create(user_id=uid)
        obj = await session.get(EconomyUser, uid)
        assert obj is not None
        obj.games_played = gp
        obj.games_won = gw
        obj.daily_streak = ds
    await session.commit()


async def test_top_by_games_orders_desc_with_tiebreaker(
    repo: RepoFixture,
) -> None:
    economy_repo, session = repo
    await _seed_stats(
        economy_repo,
        session,
        [(1, 5, 0, 0), (2, 5, 0, 0), (3, 10, 0, 0)],
    )
    rows = await economy_repo.top_by_games()
    # 10 first, then tied-5 in user_id ASC order (1 before 2).
    assert rows == [(3, 10), (1, 5), (2, 5)]


async def test_top_by_games_excludes_zero(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await _seed_stats(
        economy_repo,
        session,
        [(1, 0, 0, 0), (2, 3, 0, 0)],
    )
    rows = await economy_repo.top_by_games()
    assert rows == [(2, 3)]


async def test_top_by_wins_orders_desc(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await _seed_stats(
        economy_repo,
        session,
        [(1, 0, 4, 0), (2, 0, 8, 0), (3, 0, 1, 0)],
    )
    rows = await economy_repo.top_by_wins()
    assert rows == [(2, 8), (1, 4), (3, 1)]


async def test_top_by_streak_orders_desc(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await _seed_stats(
        economy_repo,
        session,
        [(1, 0, 0, 7), (2, 0, 0, 30), (3, 0, 0, 2)],
    )
    rows = await economy_repo.top_by_streak()
    assert rows == [(2, 30), (1, 7), (3, 2)]


async def test_top_by_streak_excludes_zero(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await _seed_stats(
        economy_repo,
        session,
        [(1, 0, 0, 0), (2, 0, 0, 5)],
    )
    rows = await economy_repo.top_by_streak()
    assert rows == [(2, 5)]


async def test_top_by_games_respects_limit_and_clamp(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await _seed_stats(
        economy_repo,
        session,
        [(i, i * 10, 0, 0) for i in range(1, 6)],
    )
    assert len(await economy_repo.top_by_games(limit=2)) == 2
    # Below-clamp: ``limit=0`` → 1.
    assert len(await economy_repo.top_by_games(limit=0)) == 1
    # Above-clamp: huge → bounded by actual rows.
    assert len(await economy_repo.top_by_games(limit=10_000)) == 5


# ── record_game (A-11) ───────────────────────────────────────────────


async def test_record_game_inserts_row_and_bumps_counters(repo: RepoFixture) -> None:
    """A win: one games row + games_played=1, games_won=1."""
    from sqlalchemy import select

    from telegram_invite_bot.db.models.economy import EconomyUser, GameResult

    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.record_game(
        7, game="roulette", bet=100, won=True, profit=89, result='{"shot": 4}'
    )
    await session.commit()

    user = await session.get(EconomyUser, 7)
    assert user is not None
    assert user.games_played == 1
    assert user.games_won == 1

    result_rows = await session.execute(select(GameResult).where(GameResult.user_id == 7))
    rows = result_rows.scalars().all()
    assert len(rows) == 1
    assert rows[0].game == "roulette"
    assert rows[0].bet == 100
    assert rows[0].win is True
    assert rows[0].profit == 89
    assert rows[0].result == '{"shot": 4}'


async def test_record_game_loss_bumps_played_not_won(repo: RepoFixture) -> None:
    from telegram_invite_bot.db.models.economy import EconomyUser

    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=8)
    await economy_repo.record_game(8, game="duel", bet=50, won=False, profit=-50)
    await session.commit()

    user = await session.get(EconomyUser, 8)
    assert user is not None
    assert user.games_played == 1
    assert user.games_won == 0


async def test_record_game_accumulates_signed_profit(repo: RepoFixture) -> None:
    """Three plays: SUM(profit) is the user's net P&L; counters accumulate."""
    from sqlalchemy import func, select

    from telegram_invite_bot.db.models.economy import EconomyUser, GameResult

    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=9)
    await economy_repo.record_game(9, game="rps", bet=100, won=True, profit=100)
    await economy_repo.record_game(9, game="rps", bet=100, won=False, profit=-100)
    await economy_repo.record_game(9, game="rps", bet=200, won=True, profit=200)
    await session.commit()

    user = await session.get(EconomyUser, 9)
    assert user is not None
    assert user.games_played == 3
    assert user.games_won == 2

    net = await session.execute(
        select(func.coalesce(func.sum(GameResult.profit), 0)).where(GameResult.user_id == 9)
    )
    assert net.scalar() == 200  # +100 -100 +200


# ── achievements awarding (A-12) ─────────────────────────────────────


async def test_record_game_awards_first_game_and_is_idempotent(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=70)
    awarded = await economy_repo.record_game(70, game="roulette", bet=10, won=False, profit=-10)
    await session.commit()
    assert "first_game" in awarded

    # A second play: games_played=2, no NEW threshold crossed → no re-award.
    again = await economy_repo.record_game(70, game="roulette", bet=10, won=False, profit=-10)
    await session.commit()
    assert "first_game" not in again


async def test_record_game_win_awards_first_win(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=71)
    awarded = await economy_repo.record_game(71, game="duel", bet=10, won=True, profit=10)
    await session.commit()
    # First game AND first win AND first duel win all unlock together.
    assert {"first_game", "first_win", "duel_winner"} <= set(awarded)


async def test_award_achievements_rich_on_balance(repo: RepoFixture) -> None:
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=72)
    await economy_repo.set_balance(72, 10_000)
    awarded = await economy_repo.award_achievements(72)
    await session.commit()
    assert {"rich_1000", "rich_10000"} <= set(awarded)


async def test_award_achievements_streak(repo: RepoFixture) -> None:
    from telegram_invite_bot.db.models.economy import EconomyUser

    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=73)
    row = await session.get(EconomyUser, 73)
    assert row is not None
    row.daily_streak = 30
    await session.commit()
    awarded = await economy_repo.award_achievements(73)
    await session.commit()
    assert {"streak_7", "streak_30"} <= set(awarded)


async def test_award_achievements_no_wallet_is_empty(repo: RepoFixture) -> None:
    economy_repo, _session = repo
    assert await economy_repo.award_achievements(9999) == []


# ── economy_snapshot / count_games_between (RR-1 #6 /chatstats) ──────


async def _wallet(
    repo_fixture: RepoFixture, user_id: int, *, balance: int, played: int, won: int
) -> None:
    from telegram_invite_bot.db.models.economy import EconomyUser

    economy_repo, session = repo_fixture
    await economy_repo.get_or_create(user_id=user_id)
    row = await session.get(EconomyUser, user_id)
    assert row is not None
    row.balance, row.games_played, row.games_won = balance, played, won
    await session.commit()


async def test_economy_snapshot_aggregates_every_wallet(repo: RepoFixture) -> None:
    economy_repo, _session = repo
    await _wallet(repo, 1, balance=1000, played=4, won=3)
    await _wallet(repo, 2, balance=500, played=2, won=1)

    snapshot = await economy_repo.economy_snapshot()
    assert snapshot.total_users == 2
    assert snapshot.total_coins == 1500
    assert snapshot.avg_balance == 750
    assert snapshot.max_balance == 1000
    assert snapshot.total_games == 6
    assert snapshot.total_wins == 4


async def test_economy_snapshot_on_an_empty_table_is_all_zeros(repo: RepoFixture) -> None:
    """Legacy returned ``avg_balance = 0`` here; ``AVG()`` would say NULL.

    The mean is derived in Python from the same total the card prints on
    the line above, so the two can never disagree — and the zero-wallet
    case can't raise ZeroDivisionError on a fresh deploy.
    """
    economy_repo, _session = repo
    snapshot = await economy_repo.economy_snapshot()
    assert (snapshot.total_users, snapshot.total_coins, snapshot.max_balance) == (0, 0, 0)
    assert snapshot.avg_balance == 0.0


async def test_count_games_between_is_half_open(repo: RepoFixture) -> None:
    """``[start, end)`` — so consecutive days tile without double-counting."""
    economy_repo, session = repo
    start = datetime(2024, 6, 10, 0, 0, 0)
    end = datetime(2024, 6, 11, 0, 0, 0)
    for stamp in (start, datetime(2024, 6, 10, 23, 59, 59), end, start - timedelta(seconds=1)):
        await economy_repo.record_game(1, game="duel", bet=10, won=True, profit=5, now=stamp)
    await session.commit()

    assert await economy_repo.count_games_between(start=start, end=end) == 2
    # The row stamped exactly at ``end`` belongs to the NEXT day's window.
    assert (
        await economy_repo.count_games_between(start=end, end=datetime(2024, 6, 12, 0, 0, 0)) == 1
    )


async def test_count_games_between_rejects_an_inverted_range(repo: RepoFixture) -> None:
    economy_repo, _session = repo
    with pytest.raises(ValueError, match="end must not precede start"):
        await economy_repo.count_games_between(
            start=datetime(2024, 6, 11), end=datetime(2024, 6, 10)
        )


async def test_debit_beyond_sqlite_range_is_refused_not_crashed(
    repo: RepoFixture,
) -> None:
    """An amount wider than 64 bits must fail closed, not raise.

    ``amount`` reaches this method straight from user text on several
    paths, and none of them bounds the product they compute. ``/check``
    is the shortest: the create interview accepts any all-digit amount
    and any all-digit activation count, multiplies them, and hands the
    product to :meth:`debit`. ``9999999999 × 9999999999`` is a plausible
    keyboard mash and is already ~10**20.

    Below 2**63 the ``WHERE balance >= amount`` guard rejects it as
    unaffordable — correct, and what the caller expects. Above it, the
    driver cannot bind the parameter at all: sqlite3 raises
    ``OverflowError: Python int too large to convert to SQLite INTEGER``
    *before* any comparison happens. So the one input that is most
    obviously nonsense was the only one that escaped the guard and
    surfaced as an unhandled 500 with a generic error toast.

    The cap the repo already documents makes the answer unambiguous:
    ``_MAX_AMOUNT`` is the highest balance a wallet may hold, so
    anything above it is unaffordable by construction and collapses
    into the same ``None`` every caller already handles.
    """
    from telegram_invite_bot.utils.economy import _MAX_AMOUNT

    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    assert await economy_repo.debit(7, 9_999_999_999 * 9_999_999_999) is None
    # Just past the documented ceiling and just past the driver's range:
    # both must take the same path, so the fix can't be a 64-bit clamp
    # that quietly re-opens the gap between the cap and 2**63.
    assert await economy_repo.debit(7, _MAX_AMOUNT + 1) is None
    assert await economy_repo.debit(7, 2**63) is None

    refetched = await economy_repo.get(7)
    assert refetched is not None
    assert refetched.balance == 100  # untouched by every rejected debit


async def test_credit_beyond_sqlite_range_is_refused_not_crashed(
    repo: RepoFixture,
) -> None:
    """Same hole on the credit side, where the cap guard lives in SQL.

    ``credit`` already refuses anything that would push the balance past
    ``_MAX_AMOUNT`` — but that check is a WHERE clause, so it only runs
    once the parameter is bound. An amount above 2**63 never gets that
    far. The ceiling has to be re-stated in Python for the guard to
    cover the range it claims to.
    """
    from telegram_invite_bot.utils.economy import _MAX_AMOUNT

    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await session.commit()

    assert await economy_repo.credit(7, 2**63) is None
    assert await economy_repo.credit(7, _MAX_AMOUNT + 1) is None

    refetched = await economy_repo.get(7)
    assert refetched is not None
    assert refetched.balance == 100


async def test_hold_moves_balance_and_leaves_lifetime_counters_alone(
    repo: RepoFixture,
) -> None:
    """#238: an escrow is a hold, not a spend.

    Legacy parks withdrawal coins with a bare
    ``UPDATE users SET balance = balance - ?`` on all three of its escrow
    paths (bot.py:20425 crypto, bot.py:20488 card/RUB, bot.py:20625
    instant buyout) and never touches a lifetime counter anywhere in the
    whole withdrawal region.
    """
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.set_balance(7, 5_000)
    await economy_repo.bump_totals(7, earned=900, spent=400)
    await session.commit()

    held = await economy_repo.hold(7, 1_200)
    await session.commit()

    assert held is not None
    assert held.balance == 3_800
    assert held.total_spent == 400, "an escrow is not a spend"
    assert held.total_earned == 900, "and it is certainly not income"


async def test_release_returns_balance_and_leaves_lifetime_counters_alone(
    repo: RepoFixture,
) -> None:
    """The mirror: legacy's refund is ``balance = balance + ?`` (bot.py:20755)."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.set_balance(7, 5_000)
    await economy_repo.bump_totals(7, earned=900, spent=400)
    await session.commit()

    released = await economy_repo.release(7, 1_200)
    await session.commit()

    assert released is not None
    assert released.balance == 6_200
    assert released.total_earned == 900, "handing back own coins is not income"
    assert released.total_spent == 400


async def test_hold_then_release_round_trip_is_a_complete_no_op(
    repo: RepoFixture,
) -> None:
    """The whole point of #238: create → reject must change nothing.

    With ``debit``/``credit`` this cycle added ``amount`` to *both*
    lifetime totals while moving no coins at all — repeatable up to the
    daily withdrawal quota, permanently inflating the ``/balance`` card.
    """
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.set_balance(7, 5_000)
    await economy_repo.bump_totals(7, earned=900, spent=400)
    await session.commit()

    for _ in range(5):
        assert await economy_repo.hold(7, 1_000) is not None
        assert await economy_repo.release(7, 1_000) is not None
    await session.commit()

    wallet = await economy_repo.get(7)
    assert wallet is not None
    assert wallet.balance == 5_000
    assert wallet.total_earned == 900
    assert wallet.total_spent == 400


async def test_hold_refuses_to_overdraw_and_changes_nothing(
    repo: RepoFixture,
) -> None:
    """Same atomic ``WHERE balance >= amount`` contract as ``debit``."""
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.set_balance(7, 500)
    await session.commit()

    assert await economy_repo.hold(7, 501) is None
    await session.commit()

    wallet = await economy_repo.get(7)
    assert wallet is not None
    assert wallet.balance == 500


async def test_release_refuses_to_breach_the_balance_ceiling(
    repo: RepoFixture,
) -> None:
    """Same ``balance + amount <= _MAX_AMOUNT`` cap as ``credit``.

    A release is still a write, and it must not be the one that drifts a
    wallet past the documented ceiling — the caller keeps the request
    refundable instead of the coins being swallowed.
    """
    from telegram_invite_bot.utils.economy import _MAX_AMOUNT

    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.set_balance(7, _MAX_AMOUNT - 10)
    await session.commit()

    assert await economy_repo.release(7, 11) is None
    assert await economy_repo.release(7, 2**63) is None
    await session.commit()

    wallet = await economy_repo.get(7)
    assert wallet is not None
    assert wallet.balance == _MAX_AMOUNT - 10


async def test_hold_and_release_return_none_for_missing_wallet(
    repo: RepoFixture,
) -> None:
    economy_repo, _ = repo
    assert await economy_repo.hold(99999, 10) is None
    assert await economy_repo.release(99999, 10) is None


async def test_negative_amounts_cannot_mint_or_destroy_coins(repo: RepoFixture) -> None:
    """A negative ``amount`` must be refused by every balance mover.

    None of the three ``WHERE`` guards can screen the sign on its own.
    ``debit(-100)`` and ``hold(-100)`` pass ``balance >= -100`` for every
    wallet in existence and then subtract a negative, i.e. CREDIT the
    wallet — and ``debit`` additionally lowers ``total_spent``, so the
    minted coins read as a spend in the lifetime counters.
    ``release(-100)`` passes the ceiling guard and destroys coins the
    caller believes it is handing back, reporting success either way.

    Every service caller screens the sign today, so this is defence in
    depth at the public repo boundary — the layer where one unscreened
    call site would be silent, unlogged inflation.
    """
    economy_repo, session = repo
    await economy_repo.get_or_create(user_id=7)
    await economy_repo.set_balance(7, 500)
    await session.commit()

    assert await economy_repo.debit(7, -100) is None
    assert await economy_repo.hold(7, -100) is None
    assert await economy_repo.release(7, -100) is None
    await session.commit()

    wallet = await economy_repo.get(7)
    assert wallet is not None
    assert wallet.balance == 500
    assert wallet.total_spent == 0

    # Zero stays a no-op that reports success, unchanged by this guard:
    # ``_bindable``'s contract deliberately leaves that to each caller.
    assert await economy_repo.debit(7, 0) is not None
