"""Async repository for the ``economy.users`` (wallet) table.

Surface:

* ``get_or_create`` (Stage 7) — idempotent wallet bootstrap. Legacy
  seeds a row with ``balance=100`` on first access (welcome credit);
  the new pipeline does the same so users created via the new code
  can't be distinguished from legacy-created ones.

* ``credit`` / ``debit`` / ``set_balance`` (Stage 8) — atomic
  single-roundtrip balance updates using ``UPDATE ... WHERE ...
  RETURNING``. The WHERE clause carries the safety guard (``balance
  >= amount`` for debit) so a concurrent debit between the read and
  the write physically cannot over-spend a wallet — the second one
  finds ``balance < amount`` and gets ``rowcount == 0``.

  The ``claim_daily`` helper lives on the service layer (UserService
  in Stage 9) because it composes balance writes with last_daily and
  streak bookkeeping that's policy, not arithmetic.

Why ``RETURNING`` over read-modify-write
----------------------------------------
SQLite 3.35+ supports ``RETURNING``. Using it gives us:

1. *One round-trip* instead of two (UPDATE then SELECT). With
   per-update WAL the cost difference is small in absolute terms but
   the freedom from "did anyone race me between the UPDATE and the
   SELECT?" reasoning is the real win.
2. *Atomic semantics with no SELECT-FOR-UPDATE.* SQLite serialises
   writes at the file level anyway, but ``RETURNING`` plus the WHERE
   guard means a single SQL statement is the entire transaction — no
   "two threads both pass the balance check, both deduct" failure
   mode that legacy ``add_coins`` defends against with a
   thread-local ``threading.Lock``.

Ledger writes (``Transaction`` rows) are NOT done here — they're a
separate concern that handlers / services compose on top. Legacy
also did them as a separate write after the UPDATE (bot.py:9743),
so this isn't a behavioural regression, and keeping the repo
single-table avoids forcing every caller to write a ledger row even
when (e.g. set_balance) it's the wrong semantics.

Math validation (``validate_credit_amount``, ``validate_balance_
target``) lives in :mod:`telegram_invite_bot.utils.economy`. Callers
should validate BEFORE invoking the write — the repo trusts its
arguments and returns ``None`` only on data-shape outcomes (wallet
missing, insufficient funds), not on caller mistakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.core.achievements import eligible_ids
from telegram_invite_bot.core.entities.wallet import Wallet
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    GameResult,
    UserAchievement,
)
from telegram_invite_bot.repositories._helpers import (
    reload_after_upsert,
    row_exists,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT

if TYPE_CHECKING:
    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


# Legacy reads this number from ``coins_start_balance`` in a live
# settings.json and can reload it at runtime (bot.py:30423), so in
# principle the two pipelines could seed new wallets differently. In
# practice they cannot (#1523, checked 2026-09-06): production carries
# no settings.json at all — the file exists nowhere under the app or
# /var/lib/telegram-bot, and the prod .env defines DATABASE_DIR with no
# SETTINGS_FILE — so legacy falls back to its own hardcoded default of
# 100 (bot.py:2545), which is exactly this constant. Should the owner
# ever introduce the file, this is the constant that has to become a
# setting.
_LEGACY_DEFAULT_BALANCE = 100

WELCOME_BALANCE: int = _LEGACY_DEFAULT_BALANCE
"""Public alias for the seed balance a brand-new wallet is created with.

The ``/start`` onboarding card (RR-6 #60) quotes this number as the
signup gift, so the copy and the seed can never drift apart. Kept as an
alias rather than a rename because several docstrings across the
repositories package already reference ``_LEGACY_DEFAULT_BALANCE`` by
name as the legacy-parity anchor.
"""


@dataclass(frozen=True, slots=True)
class EconomySnapshot:
    """Whole-economy aggregate behind the ``/chatstats`` 💰 block.

    A named record rather than the legacy ``dict[str, Any]`` so the
    renderer can't typo a key into a silent ``KeyError`` at card-build
    time — the failure mode legacy's ``get_economy_stats`` returning
    ``{}`` on error produced.
    """

    total_users: int
    total_coins: int
    avg_balance: float
    max_balance: int
    total_games: int
    total_wins: int


def _bindable(amount: int) -> bool:
    """Whether ``amount`` may be handed to a wallet UPDATE at all.

    ``credit`` and ``debit`` both decide affordability inside SQL — the
    balance ceiling and the ``balance >= amount`` guard are WHERE
    clauses, which is what makes them atomic. But a WHERE clause only
    runs once the driver has bound the parameter, and sqlite3 raises
    ``OverflowError`` outright for a Python int wider than 64 bits. So
    the further an amount was from legal, the more likely it bypassed
    the guard entirely and surfaced as an unhandled error.

    Several amounts are products of two user-typed numbers with no
    ceiling of their own (``/check``: per-claim amount × activation
    count), so this is reachable from ordinary input, not just from a
    crafted payload. Screening in Python first keeps the SQL guards
    authoritative for everything they can actually see, and collapses
    the rest into the ``None`` both methods already return for "this
    wallet can't do that" — no caller learns a new failure mode.

    ``_MAX_AMOUNT`` rather than ``2**63`` is the bound on purpose: it is
    the documented ceiling on a balance, so an amount above it is
    unaffordable by construction, and pinning to the driver's range
    instead would leave a silent band where a debit is refused for the
    wrong reason.

    Deliberately a *magnitude* test, not ``0 < amount``: whether a zero
    or negative amount is legal is the caller contract each method
    already documents, and quietly turning today's no-op debit of 0 into
    a ``None`` would change branches this fix has no business touching.
    The sign is screened here only because a wide negative overflows the
    driver exactly like a wide positive.
    """
    return -_MAX_AMOUNT <= amount <= _MAX_AMOUNT


def _to_entity(row: EconomyUser) -> Wallet:
    return Wallet(
        user_id=row.user_id,
        balance=row.balance,
        total_earned=row.total_earned,
        total_spent=row.total_spent,
        daily_streak=row.daily_streak,
        last_daily=row.last_daily,
        language=row.language,
        games_played=row.games_played,
        games_won=row.games_won,
        registered=row.registered,
    )


class EconomyRepo:
    """``economy.users`` access. Constructed per request with an open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> Wallet | None:
        row = await self._session.get(EconomyUser, user_id)
        return _to_entity(row) if row is not None else None

    async def get_or_create(
        self,
        user_id: int,
        *,
        language: str = "ru",
        now: datetime | None = None,
    ) -> Wallet:
        """Return the wallet for ``user_id``, seeding a fresh row if absent.

        Uses ``ON CONFLICT DO NOTHING`` so two concurrent insert
        attempts (legacy + new code racing on the first ever access)
        never explode — only the loser sees ``rowcount == 0`` and
        re-selects the winner's row.
        """
        # Fast path: row already present → cheap session.get, skip the
        # UPSERT round-trip. A delete between the probe and the get
        # would return None — fall through to the insert path rather
        # than asserting, so a deleted wallet self-heals (Stage 36
        # audit follow-up; not a real concern in this codebase but the
        # cost of being robust here is one ``if``).
        if await row_exists(self._session, EconomyUser.user_id, user_id):
            row = await self._session.get(EconomyUser, user_id)
            if row is not None:
                return _to_entity(row)

        now = now or datetime.now(UTC).replace(tzinfo=None)
        stmt = (
            sqlite_insert(EconomyUser)
            .values(
                user_id=user_id,
                balance=_LEGACY_DEFAULT_BALANCE,
                total_earned=0,
                total_spent=0,
                games_played=0,
                games_won=0,
                daily_streak=0,
                registered=now,
                last_seen=now,
                language=language,
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        await self._session.execute(stmt)
        row = await reload_after_upsert(self._session, EconomyUser.user_id, user_id)
        return _to_entity(row)

    async def get_display_currency(self, user_id: int) -> str | None:
        """Raw ``display_currency`` for ``user_id`` — ``None`` if unset/absent.

        Returns the stored bytes untouched. Interpretation (unknown code,
        locale default, the English RUB→USD rule) belongs to
        :func:`~telegram_invite_bot.services.currency_service.effective_currency`,
        not here. The column used to be co-owned with the legacy process
        (removed in T-011) and still holds everything that writer put
        there, so the repo's job is unchanged: hand back exactly what is
        on disk and let the reader decide what it means.
        """
        stmt = select(EconomyUser.display_currency).where(EconomyUser.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def set_display_currency(self, user_id: int, code: str) -> bool:
        """Store ``code`` as the user's display currency. ``True`` if a row matched.

        Trusts its argument, like every other write here — the caller
        validates the code against ``AVAILABLE_CURRENCIES`` before
        getting this far, and importing that table into the repo layer
        would drag a service constant under the persistence layer.

        ``False`` means no wallet row exists; callers pair this with
        ``get_or_create`` (legacy did the same via ``register_user``,
        bot.py:3392) rather than treating it as an error.

        No explicit commit — ``EconomyMiddleware`` commits the shared
        session once at request end, which is what keeps the wallet seed
        and the preference write in one transaction.
        """
        stmt = (
            update(EconomyUser).where(EconomyUser.user_id == user_id).values(display_currency=code)
        )
        # Same ``CursorResult`` cast checks_repo / inventory_repo use — the
        # async session stubs widen an UPDATE result to ``Result[Any]``,
        # which loses ``rowcount`` under mypy --strict.
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def credit(self, user_id: int, amount: int) -> Wallet | None:
        """Atomically add ``amount`` to ``balance`` and ``total_earned``.

        Returns the updated wallet, or ``None`` if no row matched —
        which now collapses three reasons into one:

        * the user has no wallet (caller should ``get_or_create``
          first if that's the wrong semantics);
        * the resulting balance would exceed
          :data:`telegram_invite_bot.utils.economy._MAX_AMOUNT`
          (M-E-3 in audits/01_economy.md: the
          ``validate_credit_amount`` cap only fenced the credit
          *delta*, not the post-credit balance; repeated credits
          could push a wallet above the documented invariant);
        * a future schema constraint blocks the update.

        Callers that care about the "would overflow the cap"
        case specifically should ``get`` the wallet first and
        check ``balance + amount <= _MAX_AMOUNT`` before this call;
        otherwise the single ``None`` matches the existing
        debit-failure semantic and surfaces the same way at the
        service layer.

        ``amount`` must be positive. Every caller already runs
        :func:`telegram_invite_bot.utils.economy.validate_credit_amount`
        first, so the guard below is a backstop rather than a policy: a
        non-positive amount reaching a *credit* would silently debit the
        wallet — ``balance + amount`` with a negative ``amount`` is a
        subtraction that skips the ``balance >= amount`` check
        :meth:`debit` exists to enforce — and would move
        ``total_earned`` backwards at the same time. Collapsing it to
        ``None`` matches the other refusals here and keeps a caller bug
        loud in tests instead of silent in the ledger (#771).

        Mirrors legacy ``add_coins`` (bot.py:9695) minus the
        cache-invalidation and WAL-checkpoint side effects, which
        belong to a higher layer if they're still wanted under
        SQLAlchemy's connection pooling.
        """
        if amount <= 0 or not _bindable(amount):
            return None
        stmt = (
            update(EconomyUser)
            .where(
                EconomyUser.user_id == user_id,
                # M-E-3: enforce the balance ceiling at write time so
                # repeated credits cannot drift a wallet past the
                # documented ``_MAX_AMOUNT`` invariant. Pure-SQL guard
                # so the check participates in the same atomic UPDATE
                # as the credit itself; a concurrent credit that
                # would push the post-write balance over the cap
                # collapses to ``rowcount == 0`` just like an
                # insufficient-funds debit does.
                EconomyUser.balance + amount <= _MAX_AMOUNT,
            )
            .values(
                balance=EconomyUser.balance + amount,
                total_earned=EconomyUser.total_earned + amount,
            )
            .returning(EconomyUser)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_entity(row) if row is not None else None

    async def debit(self, user_id: int, amount: int) -> Wallet | None:
        """Atomically subtract ``amount`` from ``balance`` if affordable.

        Returns the updated wallet on success, ``None`` if the wallet
        does not exist OR has insufficient funds. Callers that need
        to distinguish those two cases should ``get`` the wallet
        first — but for most use sites the answer is the same
        ("show the not-enough-coins message"), so the single
        ``None`` is the cheaper API.

        The ``WHERE balance >= amount`` guard is the load-bearing
        bit: two concurrent debits cannot both succeed because the
        second one sees the post-first balance and skips its UPDATE
        (``rowcount == 0``). Legacy needed a ``threading.Lock`` to
        get the same property because its read-then-write structure
        had a TOCTOU window; we don't, because the entire decision
        is one SQL statement.

        ``amount`` must be non-negative, and that is now enforced HERE
        rather than only asserted: a negative flips the sign of the
        inequality (``balance >= -100`` is true for every wallet) and
        the UPDATE then *credits* the balance while *lowering*
        ``total_spent``. Every service caller does screen the sign
        (``services/economy_service.py:145``,
        ``handlers/moderation.handle_fine``), so this is defence in
        depth — but the repo is public API and one unscreened caller
        would mint coins silently, with the ledger reading like a spend.
        Zero is still accepted and still a no-op UPDATE, deliberately:
        see :func:`_bindable`.
        """
        if amount < 0 or not _bindable(amount):
            return None
        stmt = (
            update(EconomyUser)
            .where(
                EconomyUser.user_id == user_id,
                EconomyUser.balance >= amount,
            )
            .values(
                balance=EconomyUser.balance - amount,
                total_spent=EconomyUser.total_spent + amount,
            )
            .returning(EconomyUser)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_entity(row) if row is not None else None

    async def hold(self, user_id: int, amount: int) -> Wallet | None:
        """Debit ``balance`` for an escrow hold, leaving ``total_spent`` alone.

        Same atomic ``WHERE balance >= amount`` guard as :meth:`debit`
        and the same ``None``-on-shortfall contract; the single
        difference is the missing ``total_spent`` bump.

        Why the counter must not move (#238)
        ------------------------------------
        An escrow is a *hold*, not a spend. The coins are parked, and
        the caller may hand every one of them straight back. Legacy
        moved the balance column and nothing else on all three of its
        escrow paths — ``UPDATE users SET balance = balance - ?`` at
        bot.py:20425 (crypto), bot.py:20488 (card/RUB) and
        bot.py:20625 (instant buyout) — and there is not one
        ``total_spent`` / ``total_earned`` write anywhere in the whole
        withdrawal/P2P region of bot.py (19000-21200). So a
        hold-then-release round trip left the lifetime counters exactly
        where it found them.

        :meth:`debit` would add ``amount`` to ``total_spent`` and
        :meth:`credit` would add it to ``total_earned``, permanently
        inflating *both* halves of the ``/balance`` card
        (handlers/economy.py:72-73) after a create/reject cycle that
        moved no money at all — repeatable up to the daily quota.

        A hold that later turns into a genuine spend pairs this with
        :meth:`bump_totals` at settlement time. The withdrawal path
        never does, because legacy never did: ``admin_confirm_withdrawal``
        (bot.py:20717) flips the row to ``completed`` and issues no
        balance UPDATE whatsoever.

        Negative amounts are refused for the same reason as in
        :meth:`debit`: ``WHERE balance >= amount`` cannot screen them,
        so a negative hold is an unlogged credit.
        """
        if amount < 0 or not _bindable(amount):
            return None
        stmt = (
            update(EconomyUser)
            .where(
                EconomyUser.user_id == user_id,
                EconomyUser.balance >= amount,
            )
            .values(balance=EconomyUser.balance - amount)
            .returning(EconomyUser)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_entity(row) if row is not None else None

    async def release(self, user_id: int, amount: int) -> Wallet | None:
        """Credit escrowed ``amount`` back, leaving ``total_earned`` alone.

        The mirror of :meth:`hold`, and exactly :meth:`credit` minus the
        ``total_earned`` bump. The ``balance + amount <= _MAX_AMOUNT``
        guard is kept verbatim: a release is still a write that must not
        drift a wallet past the documented ceiling, and collapsing to
        ``rowcount == 0`` is what lets the caller keep the request
        refundable instead of silently swallowing the coins.

        Handing back coins the user already owned is not income (#238).
        Legacy's refund is ``UPDATE users SET balance = balance + ?`` at
        bot.py:20755 — the column and nothing else.

        Negative amounts are refused: the ceiling guard only screens the
        upward direction, so a negative release would *destroy* coins the
        caller believes it is handing back, and report success.
        """
        if amount < 0 or not _bindable(amount):
            return None
        stmt = (
            update(EconomyUser)
            .where(
                EconomyUser.user_id == user_id,
                EconomyUser.balance + amount <= _MAX_AMOUNT,
            )
            .values(balance=EconomyUser.balance + amount)
            .returning(EconomyUser)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_entity(row) if row is not None else None

    async def set_balance(self, user_id: int, new_balance: int) -> Wallet | None:
        """Force-set ``balance`` to ``new_balance`` (admin override).

        Returns the updated wallet, or ``None`` if the wallet does
        not exist. Writes the ``balance`` column and nothing else —
        legacy ``set_balance`` also bumps the lifetime counters from
        the delta (bot.py:9676-9679), but that needs the *old*
        balance, which only the service layer has read. The service
        composes this call with :meth:`bump_totals`; see
        :meth:`telegram_invite_bot.services.economy_service.EconomyService.set_balance`.

        Keeping the counters out of this statement is also what lets
        the test suite use ``set_balance`` as a plain balance-seeding
        fixture without silently inflating ``total_earned``.

        Validate with :func:`validate_balance_target` before
        calling — negative targets reach the database otherwise and
        would corrupt the wallet (the balance column is signed).
        """
        stmt = (
            update(EconomyUser)
            .where(EconomyUser.user_id == user_id)
            .values(balance=new_balance)
            .returning(EconomyUser)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_entity(row) if row is not None else None

    async def bump_totals(self, user_id: int, *, earned: int = 0, spent: int = 0) -> Wallet | None:
        """Add to the lifetime counters without touching ``balance`` (#257).

        The mirror image of :meth:`set_balance`: that one moves the
        wallet, this one moves the history. Legacy keeps the two
        together inside ``set_balance`` (bot.py:9676-9679 calling
        ``update_total_earned`` / ``update_total_spent`` at
        bot.py:11034 / bot.py:11056); here they are separate
        statements because only the service knows the delta.

        Both arguments are magnitudes and must be non-negative — a
        negative ``earned`` would *reduce* a lifetime counter, which
        is not a thing that can happen to a "total ever earned" and
        would quietly rewrite history. Callers pass ``max(delta, 0)``
        and ``max(-delta, 0)``.

        Deliberately NOT named ``credit``/``debit``/``set_balance``:
        those names are what ``tests/regression/test_money_call_sites``
        sweeps for, and this method moves no coins, so sweeping it in
        would dilute a guard that exists to catch unchecked *wallet*
        writes.

        Returns the refreshed wallet, or ``None`` if the row does not
        exist or either argument is negative / out of 64-bit range.
        """
        if earned < 0 or spent < 0:
            return None
        if not _bindable(earned) or not _bindable(spent):
            return None
        if earned == 0 and spent == 0:
            return await self.get(user_id)
        stmt = (
            update(EconomyUser)
            .where(EconomyUser.user_id == user_id)
            .values(
                total_earned=EconomyUser.total_earned + earned,
                total_spent=EconomyUser.total_spent + spent,
            )
            .returning(EconomyUser)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_entity(row) if row is not None else None

    async def record_game(
        self,
        user_id: int,
        *,
        game: str,
        bet: int,
        won: bool,
        profit: int,
        result: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """Append a ``games`` row and bump the player's game counters (A-11).

        One play → one :class:`GameResult` row (the append-only
        result-of-a-game ledger ``/duel_stats`` and ``/chatstats`` read)
        + ``users.games_played += 1`` and ``games_won += 1`` iff ``won``.
        Mirrors legacy ``result.save()`` + ``update_game_stats``
        (bot.py:14251) which every game called after settling balances.

        Runs on the SAME shared economy session as the caller's
        debit/credit (EconomyMiddleware commits once at request end), so
        the counters can never drift from the wallet — both land in one
        atomic commit, or neither does on rollback.

        ``profit`` is the SIGNED net P&L for this player (positive on a
        net win, negative on a net loss, 0 on a refunded tie) so
        ``SUM(profit)`` over a user's rows is their lifetime net directly
        — matching the column's documented convention. ``bet`` is the
        stake (always positive). ``result`` is an optional opaque detail
        blob (e.g. a JSON ``{"shot": 4}``) stored verbatim. The caller is
        trusted to have already settled the wallet; this method writes
        only the stats, never the balance.

        Counters are bumped with an in-SQL ``games_played + 1`` (not a
        read-modify-write) so two concurrent plays by the same user both
        count — same atomic-UPDATE posture as ``credit``/``debit``. If the
        wallet row is missing the UPDATE is a no-op (``rowcount == 0``);
        the games row is still inserted, matching legacy which logged the
        play regardless of the stats-row state.

        Returns the list of achievement ids newly unlocked by this play
        (A-12) — empty when none. Awarding runs on this same session so it
        commits atomically with the game row; the caller can surface the
        ids (e.g. /roulette's "new achievements" line) or ignore them
        (duel/rps award silently, matching legacy's PvP path).
        """
        now = now or datetime.now(UTC).replace(tzinfo=None)
        self._session.add(
            GameResult(
                user_id=user_id,
                game=game,
                bet=bet,
                result=result,
                win=won,
                profit=profit,
                date=now,
            )
        )
        await self._session.execute(
            update(EconomyUser)
            .where(EconomyUser.user_id == user_id)
            .values(
                games_played=EconomyUser.games_played + 1,
                games_won=EconomyUser.games_won + (1 if won else 0),
            )
        )
        return await self.award_achievements(user_id, now=now)

    async def award_achievements(self, user_id: int, *, now: datetime | None = None) -> list[str]:
        """Award every achievement the user now qualifies for (A-12).

        Reads the user's current stats (``games_played`` / ``games_won`` /
        ``balance`` / ``daily_streak`` from ``users`` + a ``duel`` win
        count from ``games``), asks :func:`core.achievements.eligible_ids`
        which definitions those stats satisfy, subtracts the ids already
        in ``user_achievements``, and inserts the remainder. Returns the
        newly-inserted ids (sorted), empty when nothing new.

        Idempotent + race-safe: the INSERT carries
        ``ON CONFLICT (user_id, achievement_id) DO NOTHING`` so a
        concurrent award of the same id can't raise or duplicate. Runs on
        the shared session, so awards commit atomically with whatever
        settlement triggered the check (a game row, a /daily claim).

        Safe to call from anywhere a tracked stat changed; a user with no
        wallet row (stats unavailable) awards nothing.
        """
        now = now or datetime.now(UTC).replace(tzinfo=None)
        stats_row = (
            await self._session.execute(
                select(
                    EconomyUser.games_played,
                    EconomyUser.games_won,
                    EconomyUser.balance,
                    EconomyUser.daily_streak,
                ).where(EconomyUser.user_id == user_id)
            )
        ).first()
        if stats_row is None:
            return []
        games_played, games_won, balance, daily_streak = stats_row

        duel_wins = (
            await self._session.execute(
                select(func.count())
                .select_from(GameResult)
                .where(
                    GameResult.user_id == user_id,
                    GameResult.game == "duel",
                    GameResult.win.is_(True),
                )
            )
        ).scalar() or 0

        eligible = eligible_ids(
            games_played=int(games_played),
            games_won=int(games_won),
            duel_wins=int(duel_wins),
            daily_streak=int(daily_streak),
            balance=int(balance),
        )
        if not eligible:
            return []

        earned = set(
            (
                await self._session.execute(
                    select(UserAchievement.achievement_id).where(UserAchievement.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        new_ids = sorted(eligible - earned)
        if not new_ids:
            return []

        await self._session.execute(
            sqlite_insert(UserAchievement)
            .values(
                [
                    {
                        "user_id": user_id,
                        "achievement_id": aid,
                        "earned_date": now,
                        "notified": False,
                    }
                    for aid in new_ids
                ]
            )
            .on_conflict_do_nothing(index_elements=["user_id", "achievement_id"])
        )
        return new_ids

    async def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        """Record ``referrer_id`` as the inviter of ``user_id`` — once.

        Mirrors legacy ``set_referrer`` (bot.py:9789)::

            UPDATE users SET referred_by = ?
             WHERE user_id = ? AND (referred_by IS NULL OR referred_by = 0)

        The ``referred_by IS NULL OR referred_by = 0`` guard makes the
        write *first-attribution-wins*: a user's inviter is locked in on
        their very first ``/start ref_<id>`` and a later deep-link from a
        different referrer cannot overwrite it. Doing the guard in the
        WHERE clause (rather than read-then-write) means two concurrent
        ``/start`` taps can't both claim the attribution — the loser sees
        ``rowcount == 0``.

        Returns ``True`` iff a row was actually updated (fresh
        attribution recorded). ``False`` covers every no-op: the user
        already has a referrer, the wallet doesn't exist, or the caller
        passed a self/invalid referrer (guarded here defensively, same as
        legacy which refused ``user_id == referrer_id`` and
        ``referrer_id <= 0``).
        """
        if referrer_id <= 0 or user_id == referrer_id:
            return False
        stmt = (
            update(EconomyUser)
            .where(
                EconomyUser.user_id == user_id,
                (EconomyUser.referred_by.is_(None)) | (EconomyUser.referred_by == 0),
            )
            .values(referred_by=referrer_id)
            .returning(EconomyUser.user_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def top_by_balance(self, *, limit: int = 10) -> list[tuple[int, int]]:
        """Return the top wallets by ``balance``, descending.

        Returns ``[(user_id, balance), ...]`` — same shape as
        :meth:`MessageStatsRepo.top_users_by_messages` so the ``/top``
        handler can render either source through one path.

        Ordering: ``balance DESC, user_id ASC``. The ``user_id``
        tiebreaker is *deterministic* — without it, two wallets with
        equal balance can swap places between calls (SQLite gives no
        ordering guarantee for ties), which makes the e2e test for
        "expected leaderboard order" flaky on a small seed. The legacy
        ``get_top_richest`` (bot.py:11242) has no tiebreaker — it can
        flicker on prod for tied balances, and that's exactly the bug
        we don't want to inherit.

        Zero-balance wallets are filtered out. Legacy doesn't apply
        this filter explicitly (bot.py:11242 has no WHERE clause), but
        every wallet starts at 100 via :meth:`get_or_create`, so the
        only way to reach ``balance == 0`` is to actively spend
        everything. Such rows are noise on the leaderboard — they sit
        below any active user and just consume slots. Filtering at
        the SQL level (rather than the handler) keeps the cap-of-50
        clamp meaningful: with 200 zero-balance ghost wallets in the
        DB and no filter, ``LIMIT 50`` could return all-zero rows and
        the handler would render a leaderboard nobody asked for.

        ``limit`` is clamped to ``1..50`` defensively — same range as
        the messages leaderboard (``handlers/top.py:_DEFAULT_LIMIT``).
        A caller passing ``limit=0`` would otherwise produce an empty
        list that's indistinguishable from "no wallets", and a huge
        ``limit`` would bust Telegram's 4096-char message cap. Both
        are caller bugs we want to fail soft on, not propagate.
        """
        clamped = max(1, min(limit, 50))
        stmt = (
            select(EconomyUser.user_id, EconomyUser.balance)
            .where(EconomyUser.balance > 0)
            .order_by(EconomyUser.balance.desc(), EconomyUser.user_id.asc())
            .limit(clamped)
        )
        result = await self._session.execute(stmt)
        return [(int(uid), int(bal)) for uid, bal in result.all()]

    async def top_by_games(self, *, limit: int = 10) -> list[tuple[int, int]]:
        """Top wallets by ``games_played``, descending.

        Legacy: ``bot.py:get_top_by_games`` —
        ``SELECT user_id, games_played FROM users WHERE games_played > 0
         ORDER BY games_played DESC LIMIT ?``. Same shape as
        :meth:`top_by_balance` so :mod:`handlers.top` renders all four
        modes through one path. ``user_id`` tiebreaker for determinism,
        zero-row filter to keep the cap meaningful — see
        :meth:`top_by_balance` for the rationale.
        """
        clamped = max(1, min(limit, 50))
        stmt = (
            select(EconomyUser.user_id, EconomyUser.games_played)
            .where(EconomyUser.games_played > 0)
            .order_by(EconomyUser.games_played.desc(), EconomyUser.user_id.asc())
            .limit(clamped)
        )
        result = await self._session.execute(stmt)
        return [(int(uid), int(n)) for uid, n in result.all()]

    async def top_by_wins(self, *, limit: int = 10) -> list[tuple[int, int]]:
        """Top wallets by ``games_won``, descending.

        Legacy: ``bot.py:get_top_by_wins``. Identical shape /
        determinism trade-off as :meth:`top_by_games`.
        """
        clamped = max(1, min(limit, 50))
        stmt = (
            select(EconomyUser.user_id, EconomyUser.games_won)
            .where(EconomyUser.games_won > 0)
            .order_by(EconomyUser.games_won.desc(), EconomyUser.user_id.asc())
            .limit(clamped)
        )
        result = await self._session.execute(stmt)
        return [(int(uid), int(n)) for uid, n in result.all()]

    async def economy_snapshot(self) -> EconomySnapshot:
        """Whole-economy aggregate for the ``/chatstats`` 💰 block.

        Legacy ``get_economy_stats`` (bot.py:11462) pulled every wallet
        row into Python and summed there. That is a full table read on
        every card render; here the five aggregates are one SQL pass and
        the mean is computed from ``total_coins / total_users`` in Python
        rather than ``AVG(balance)`` so it matches legacy exactly on the
        empty table (legacy returns ``0``, ``AVG`` returns ``NULL``) and
        cannot disagree with the total printed on the line above it.

        ``avg_balance`` is a float on purpose — the caller decides the
        rounding, because the ru and en cards format it differently.

        Bot exclusion is NOT applied. Legacy filtered through
        ``is_excluded_bot_user``, which needs a live ``get_chat_member``
        probe plus a process-wide id cache; :mod:`handlers.top` already
        documents why that isn't ported yet, and this snapshot inherits
        the same known gap rather than inventing a second, differently
        wrong filter.
        """
        stmt = select(
            func.count(),
            func.coalesce(func.sum(EconomyUser.balance), 0),
            func.coalesce(func.max(EconomyUser.balance), 0),
            func.coalesce(func.sum(EconomyUser.games_played), 0),
            func.coalesce(func.sum(EconomyUser.games_won), 0),
        ).select_from(EconomyUser)
        users, coins, richest, games, wins = (await self._session.execute(stmt)).one()
        total_users = int(users)
        total_coins = int(coins)
        return EconomySnapshot(
            total_users=total_users,
            total_coins=total_coins,
            avg_balance=(total_coins / total_users) if total_users else 0.0,
            max_balance=int(richest),
            total_games=int(games),
            total_wins=int(wins),
        )

    async def count_games_between(self, *, start: datetime, end: datetime) -> int:
        """Rows in ``economy.games`` with ``start <= date < end``.

        Backs the "games today" counter on the ``/chatstats`` card.
        Legacy asked SQLite for ``WHERE date(date) = date('now')``.
        SQLite's ``'now'`` is UTC unless the ``'localtime'`` modifier is
        passed, and legacy never passed it — so that counter was pinned
        to the UTC day while every other window on the same card used
        ``StatsConfig.timezone``. Two blocks, two different "todays".

        The bounds are therefore passed in, and they must be **naive
        UTC** — :meth:`record_game` writes ``datetime.now(UTC).replace(
        tzinfo=None)``, so a tz-aware bound would compare an offset
        string against an offset-free one in SQLite and match nothing.
        Converting the caller's tz-local midnight into UTC is the
        caller's job; it is the only layer that knows the timezone.

        Half-open ``[start, end)`` so consecutive days tile without
        double-counting a game logged exactly at midnight.
        """
        if end < start:
            raise ValueError("end must not precede start")
        stmt = (
            select(func.count())
            .select_from(GameResult)
            .where(GameResult.date >= start)
            .where(GameResult.date < end)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def top_by_streak(self, *, limit: int = 10) -> list[tuple[int, int]]:
        """Top wallets by ``daily_streak``, descending.

        Legacy: ``bot.py:get_top_by_streak`` reads the live ``daily_streak``
        column (NOT ``last_daily_streak`` — that's a separate timestamp
        used to reset stale streaks). Same shape / determinism trade-off
        as :meth:`top_by_games`.
        """
        clamped = max(1, min(limit, 50))
        stmt = (
            select(EconomyUser.user_id, EconomyUser.daily_streak)
            .where(EconomyUser.daily_streak > 0)
            .order_by(EconomyUser.daily_streak.desc(), EconomyUser.user_id.asc())
            .limit(clamped)
        )
        result = await self._session.execute(stmt)
        return [(int(uid), int(n)) for uid, n in result.all()]

    async def mark_daily_claimed(
        self,
        user_id: int,
        *,
        now: datetime,
        new_streak: int,
    ) -> Wallet | None:
        """Atomically record a successful ``/daily`` claim.

        Sets ``last_daily = now`` and ``daily_streak = new_streak`` —
        but only if the cooldown is genuinely up at the SQL level, so
        two concurrent claims can't both succeed even if they passed
        the Python cooldown check moments apart.

        The WHERE guard mirrors legacy ``bot.py:12109``::

            WHERE user_id = ?
              AND (last_daily IS NULL
                   OR (julianday(?) - julianday(last_daily)) >= 1)

        SQLite's ``julianday`` returns a fractional day; ``>= 1``
        catches "24 real hours have passed" rather than "calendar day
        rolled over", matching legacy's window. Both sides of the
        subtraction carry microseconds, so the guard admits exactly
        what the Python check admits — see ``now_iso`` below. Without this guard
        two near-simultaneous claims would both write a new
        ``last_daily`` and both pass through to credit() — doubling
        the payout. The guard returning ``rowcount == 0`` to the
        losing claim is the race fix; the service layer reads the
        ``None`` and skips the ledger write.

        Returns the updated wallet on success, ``None`` if the guard
        rejected (race lost OR no wallet OR cooldown not yet up — all
        three collapse into "user doesn't get the payout this call").

        Note: this method does NOT credit the wallet. The service
        composes this guarded UPDATE with
        :meth:`EconomyService.credit` so the ledger row carries
        ``type='daily'`` for the read-side handlers — keeping the
        repo single-responsibility per Stage 8's split.
        """
        # Full microsecond precision — the same value ``.values()``
        # writes below, and the same precision
        # :func:`utils.daily.daily_cooldown_remaining` compares in
        # Python. Truncating to whole seconds here made the SQL guard
        # *stricter* than the Python check by up to one second: a claim
        # landing in ``[24h, 24h + frac(now))`` passed the service's
        # cooldown check and was then rejected by this UPDATE,
        # surfacing to the user as a bogus RACE_LOST card telling them
        # to wait another 24 hours (#465). SQLite's ``julianday``
        # parses six fractional digits (verified on prod 3.46.1).
        now_iso = now.replace(tzinfo=None).isoformat(sep=" ")
        stmt = (
            update(EconomyUser)
            .where(
                EconomyUser.user_id == user_id,
                # SQLAlchemy renders ``or_(col.is_(None), <expr>)`` to
                # ``(col IS NULL OR <expr>)`` which the guard needs to
                # admit the first-ever claim (last_daily NULL).
                (EconomyUser.last_daily.is_(None))
                | (func.julianday(now_iso) - func.julianday(EconomyUser.last_daily) >= 1),
            )
            .values(
                last_daily=now.replace(tzinfo=None),
                daily_streak=new_streak,
            )
            .returning(EconomyUser)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_entity(row) if row is not None else None
