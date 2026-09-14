"""``/check`` (coin-code voucher) create + claim, composed atomically.

The CREATE flow debits the creator, writes the check row and a ledger
row; the CLAIM flow runs an ordered set of gates (mirroring legacy
``_activate_check`` at ``bot.py:10081``), reserves coins via the
race-safe :meth:`ChecksRepo.claim_decrement`, records the claim under
the ``UNIQUE(check_id, user_id)`` double-claim guard, and credits the
claimer — all on ONE shared ``economy`` session so each op is a single
atomic transaction.

Money invariants this service upholds (this is #26, real coins):

* CREATE never mints: the creator is debited by exactly
  ``total_amount`` (the sum of what claimers can ever redeem) BEFORE the
  check row exists; if the debit fails (insufficient funds) no check is
  written.
* CLAIM never double-credits: the decrement guard stops a drain race,
  and the ``UNIQUE`` constraint stops a double-claim race. The credit
  (step 12) happens only AFTER both guards pass and the claim row is
  durably inserted.

Atomicity / rollback contract
-----------------------------
The shared session is owned by :class:`EconomyMiddleware`
(:class:`BaseSessionMiddleware`), which commits on handler success and
rolls back only on a RAISED exception. So a claim that fails the
``UNIQUE`` guard cannot simply ``return`` ALREADY_CLAIMED — the
middleware would then COMMIT the partial work (the decrement) without
the matching claim row, leaking coins out of the check. We therefore
catch the :class:`IntegrityError` and call ``session.rollback()``
ourselves before returning, which undoes the decrement in the same
statement-group. The credit has not happened yet at that point, so no
coins were created or destroyed.

Legacy claim-time filter gates (L-85..L-88)
-------------------------------------------
``blocked_users``, ``min_age``, ``min_activity`` and
``allowed_countries`` ARE enforced, as Gate 8b of :meth:`claim_check`
— see :meth:`_filter_gate` for the legacy order and the per-gate
semantics. Two of the four are best-effort by construction:
``min_activity`` falls back to an economy-side proxy and is SKIPPED
when no count is available, and ``allowed_countries`` is a PASS unless
the caller supplies ``claimant_country`` (the economy schema stores no
country and there is no IP resolution). A malformed JSON column fails
open for that single gate, as in legacy.

``required_subscription`` is enforced too (#26) — but at the
CLAIM-SIDE HANDLER, not here. The membership verification needs the
Telegram ``bot.get_chat_member`` call this service can't make, so the
handler runs that PRE-CHECK (``_gate_blocked`` at
``handlers/checks.py:421``, called at ``handlers/checks.py:540``)
before calling :meth:`claim_check`; the service only answers the data
question via :meth:`requires_subscription`. Money logic stays entirely
in :meth:`claim_check`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.utils.rng import money_rng

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db.models.economy import Check
    from telegram_invite_bot.repositories.checks_repo import ChecksRepo
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


class CreateOutcome(StrEnum):
    """Mutually-exclusive results the create flow can produce."""

    OK = "ok"
    INVALID_AMOUNT = "invalid_amount"
    """Computed ``total_amount`` (or a per-claim amount) was non-positive."""
    INSUFFICIENT_FUNDS = "insufficient_funds"
    """Creator could not afford ``total_amount`` — no check was written."""


class ClaimOutcome(StrEnum):
    """Mutually-exclusive results the claim flow can produce.

    StrEnum so log lines / a future audit log render the name as plain
    text — same posture as :class:`TransferOutcome`.
    """

    OK = "ok"
    EMPTY_CODE = "empty_code"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    MAX_REACHED = "max_reached"
    WRONG_USER = "wrong_user"
    ALREADY_CLAIMED = "already_claimed"
    WRONG_LANG = "wrong_lang"
    PREMIUM_ONLY = "premium_only"
    NO_FUNDS = "no_funds"
    RACE_LOST = "race_lost"
    # Crediting the claimer failed (balance cap overflow / vanished
    # wallet) AFTER the check was decremented — the whole claim is rolled
    # back so the check's coins are not consumed without reaching the
    # claimer (SEC audit: silent payout-failure money leak).
    CREDIT_FAILED = "credit_failed"
    """Lost the drain race (check exhausted between read and decrement)."""
    BLOCKED_USER = "blocked_user"
    """Claimer's id is in the check's ``blocked_users`` blacklist (L-88)."""
    MIN_AGE = "min_age"
    """Claimer's account is younger than the check's ``min_age`` days (L-85)."""
    MIN_ACTIVITY = "min_activity"
    """Claimer's activity count is below the check's ``min_activity`` (L-86)."""
    COUNTRY_BLOCKED = "country_blocked"
    """Claimer's country isn't in the check's ``allowed_countries`` (L-87)."""


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """Parsed parameters for a check the handler wants to create.

    ``type`` is one of ``"random"`` / ``"fixed"`` / ``"individual"``.
    ``now`` is injected so tests are deterministic and the created_at /
    expires_at math is pinned at the call site.
    """

    type: str
    now: datetime
    min_amount: int | None = None
    max_amount: int | None = None
    fixed_amount: int | None = None
    max_claims: int = 0
    target_user_id: int | None = None
    required_language: str | None = None
    required_premium: bool = False
    required_subscription: bool = False
    expires_at: datetime | None = None
    # Claim-time gates (L-85..L-88). Stored on the check row and
    # enforced in :meth:`claim_check`. ``min_age`` is in DAYS (account
    # age via ``EconomyUser.registered``); ``min_activity`` is a raw
    # action/games-played threshold; ``allowed_countries`` /
    # ``blocked_users`` are JSON-encoded lists persisted in the
    # existing TEXT columns (no migration needed).
    min_age: int | None = None
    min_activity: int | None = None
    allowed_countries: list[str] | None = None
    blocked_users: list[int] | None = None


@dataclass(frozen=True, slots=True)
class CreateResult:
    """What a create call produced. Read ``outcome`` first."""

    outcome: CreateOutcome
    code: str = ""
    total_amount: int = 0


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """What a claim call produced. Read ``outcome`` first.

    Success fields (``amount`` / ``remaining`` / ``creator_id`` /
    ``code``) stay zero/empty on the failure paths so the handler can
    render a uniform receipt without per-field guards.
    """

    outcome: ClaimOutcome
    amount: int = 0
    remaining: int = 0
    creator_id: int = 0
    code: str = ""
    required_language: str = ""
    """Set on WRONG_LANG so the handler can tell the user which language
    the check requires."""
    claims_left: int | None = None
    """Activations still available AFTER this claim (RR-2 #18).

    ``None`` means *unlimited* — the check has ``max_claims = 0``, which
    the receipt renders as ``∞`` (legacy bot.py:10188 shows exactly that
    ternary). Only meaningful on OK; every failure path leaves it
    ``None``, which the handler never reads because it renders an error
    line instead. Kept distinct from ``remaining`` on purpose: that one
    counts COINS left in the check, this one counts PEOPLE who can still
    claim it, and legacy's card showed the activations counter — the
    port had silently swapped in the coin figure."""


class CheckService:
    """Atomic create + claim execution against the shared economy session."""

    def __init__(
        self,
        checks_repo: ChecksRepo,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        session: AsyncSession,
    ) -> None:
        self._checks = checks_repo
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._session = session

    async def create_check(self, *, creator_id: int, spec: CheckSpec) -> CreateResult:
        """Create a funded check, debiting the creator atomically.

        Order (the order matters for the money invariant):

        1. Compute ``total_amount`` from the spec and validate it is
           positive. ``random`` → ``avg(min,max) * max_claims``;
           ``fixed`` → ``fixed_amount * max_claims``; ``individual`` →
           ``fixed_amount`` (single claim). A non-positive total (or a
           ``random`` spec with ``min > max`` yielding ``<= 0``) returns
           INVALID_AMOUNT before any DB write.
        2. Debit the creator by ``total_amount`` via the race-safe
           ``EconomyRepo.debit`` (WHERE ``balance >= amount``). ``None``
           → INSUFFICIENT_FUNDS, and NO check row is written — the
           coins never leave a wallet that can't afford them, and no
           orphan voucher is funded by thin air.
        3. Generate a unique code and INSERT the check with
           ``remaining_amount = total_amount``.
        4. Write the ledger row (``type='check_create'``,
           ``from_id=creator``, ``to_id=None``).

        Steps 2-4 land in one transaction (the shared session); a flush
        failure on the INSERT raises and the middleware rolls the debit
        back, so a failed create cannot debit-without-voucher.
        """
        total_amount = self._compute_total(spec)
        if total_amount <= 0:
            return CreateResult(outcome=CreateOutcome.INVALID_AMOUNT)

        # Atomic debit. None == insufficient funds (or no wallet): no
        # check is written, so we never fund a voucher from coins that
        # don't exist.
        debited = await self._economy.debit(creator_id, total_amount)
        if debited is None:
            return CreateResult(outcome=CreateOutcome.INSUFFICIENT_FUNDS)

        # Serialise the optional list-valued gates into the existing TEXT
        # columns as JSON (no migration needed). ``None`` / empty lists
        # stay NULL so an ungated check reads exactly as before.
        allowed_countries_json = (
            json.dumps([c.upper() for c in spec.allowed_countries])
            if spec.allowed_countries
            else None
        )
        blocked_users_json = json.dumps(spec.blocked_users) if spec.blocked_users else None

        code = await self._checks.generate_unique_code()
        await self._checks.create(
            code=code,
            creator_id=creator_id,
            type=spec.type,
            total_amount=total_amount,
            remaining_amount=total_amount,
            min_amount=spec.min_amount,
            max_amount=spec.max_amount,
            fixed_amount=spec.fixed_amount,
            target_user_id=spec.target_user_id,
            max_claims=spec.max_claims,
            required_language=spec.required_language,
            required_premium=1 if spec.required_premium else 0,
            required_subscription=1 if spec.required_subscription else 0,
            min_age=spec.min_age,
            min_activity=spec.min_activity,
            allowed_countries=allowed_countries_json,
            blocked_users=blocked_users_json,
            expires_at=spec.expires_at,
            now=spec.now,
        )
        await self._ledger.record(
            from_id=creator_id,
            to_id=None,
            amount=total_amount,
            reason="check_create",
            type="check_create",
            date=spec.now,
        )
        return CreateResult(outcome=CreateOutcome.OK, code=code, total_amount=total_amount)

    @staticmethod
    def _compute_total(spec: CheckSpec) -> int:
        """Derive ``total_amount`` from the spec; 0 on a malformed spec.

        Returning 0 (rather than raising) on a bad spec lets
        :meth:`create_check` funnel every "amount doesn't make sense"
        case through the single INVALID_AMOUNT outcome.
        """
        if spec.type == "random":
            if spec.min_amount is None or spec.max_amount is None:
                return 0
            if spec.min_amount <= 0 or spec.max_amount < spec.min_amount:
                return 0
            avg = (spec.min_amount + spec.max_amount) // 2
            return avg * max(spec.max_claims, 0)
        if spec.type == "fixed":
            if spec.fixed_amount is None or spec.fixed_amount <= 0:
                return 0
            return spec.fixed_amount * max(spec.max_claims, 0)
        if spec.type == "individual":
            if spec.fixed_amount is None or spec.fixed_amount <= 0:
                return 0
            return spec.fixed_amount
        return 0

    async def requires_subscription(self, *, code: str) -> bool:
        """Return True iff an ACTIVE check ``code`` has its
        ``required_subscription`` flag set.

        A lightweight read used by the handler's claim-side subscription
        PRE-CHECK (#26): the channel-membership verification lives in the
        handler (it needs the Telegram ``bot`` this economy-scoped
        service has no handle on), so the handler asks the service only
        the data question — "does this check want a subscription gate?" —
        and runs the membership check itself BEFORE calling
        :meth:`claim_check`. No money moves here; an unknown / inactive /
        normalised-empty code returns ``False`` (the claim call will then
        produce the authoritative NOT_FOUND / EMPTY_CODE outcome).
        """
        normalized = (code or "").strip().upper()
        if not normalized:
            return False
        check = await self._checks.get_active_by_code(normalized)
        if check is None:
            return False
        return bool(check.required_subscription)

    async def claim_check(
        self,
        *,
        user_id: int,
        code: str,
        is_premium: bool,
        user_lang: str | None,
        now: datetime,
        claimant_activity: int | None = None,
        claimant_country: str | None = None,
    ) -> ClaimResult:
        """Redeem ``code`` for ``user_id``, crediting them atomically.

        Gate order mirrors legacy ``_activate_check`` (``bot.py:10081``):

        1. Normalise ``code`` (strip + upper); empty → EMPTY_CODE.
        2. Load the active check; missing → NOT_FOUND.
        3. Expiry: ``expires_at and now > expires_at`` → deactivate +
           EXPIRED.
        4. ``max_claims > 0 and claims_count >= max_claims`` →
           deactivate + MAX_REACHED.
        5. ``individual`` check targeted at someone else → WRONG_USER.
        6. Already claimed (fast friendly pre-check) → ALREADY_CLAIMED.
        7. Language gate → WRONG_LANG.
        8. Premium gate → PREMIUM_ONLY.
        9. Compute amount (random → ``randint(min,max)``; else
           ``fixed_amount``), clamp to ``remaining_amount``; ``<= 0`` →
           NO_FUNDS.
        10. Atomic decrement guard; ``False`` → RACE_LOST.
        11. Insert the claim row under the ``UNIQUE`` guard. On
            IntegrityError (concurrent double-claim) → rollback +
            ALREADY_CLAIMED.
        12. Seed the claimer's wallet if absent, then credit them and
            write the ledger row.

        ``required_subscription`` is enforced UPSTREAM in the handler
        (claim-side gate, #26): by the time this method runs the claimer
        has already been verified as a channel member, so no
        subscription gate appears in the order below.

        L-85..L-88 claim-time gates run as Gate 8b (after premium,
        before the amount compute) in legacy's order:

        * ``blocked_users`` (L-88) → BLOCKED_USER. Pure data read from
          the check row's JSON column — fully enforceable here.
        * ``min_age`` (L-85) → MIN_AGE. Account age in days derived from
          ``EconomyUser.registered``. Legacy's branch was an explicit
          NO-OP (bot.py:10066-10067 — the Telegram API gives no
          account-creation date), so enforcing it at all is an
          improvement; note that it also FAILS CLOSED on a missing
          ``registered`` stamp, which is strictly stricter than legacy.
        * ``min_activity`` (L-86) → MIN_ACTIVITY. ``claimant_activity``
          is a parameter no production call site fills: both claim entry
          points (``handlers/checks.py:548-554`` and ``:637-643``) call
          this method without it, so what actually runs is the fallback
          — ``EconomyUser.games_played``. That diverges from legacy,
          which counted rows in ``user_history``
          (``SELECT COUNT(*) FROM user_history WHERE user_id = ?``,
          bot.py:10071-10074): a "minimum N activities" threshold here
          really means "minimum N games played". The parameter stays
          because ``activity.db`` holds the closer count and a handler
          can start passing it. When neither source yields a number the
          gate is skipped rather than rejecting a claim it can't
          evaluate.
        * ``allowed_countries`` (L-87) → COUNTRY_BLOCKED. This gate has
          no legacy counterpart at all — the column is never even
          selected there (bot.py:10090) — and it is inert here too:
          there is NO IP resolution and no stored user country in the
          economy schema, and no production call site supplies
          ``claimant_country`` (only
          ``tests/unit/services/test_check_gates.py:113``), so it always
          PASSES. Kept wired so that a future country source only has to
          fill the argument; when a country IS supplied it is matched
          case-insensitively against the stored allow-list.
        """
        # Gate 1: normalise.
        normalized = (code or "").strip().upper()
        if not normalized:
            return ClaimResult(outcome=ClaimOutcome.EMPTY_CODE)

        # Gate 2: load active check.
        check = await self._checks.get_active_by_code(normalized)
        if check is None:
            return ClaimResult(outcome=ClaimOutcome.NOT_FOUND)

        check_id = int(check.id)
        creator_id = int(check.creator_id)

        # Gate 3: expiry.
        if check.expires_at is not None and now > check.expires_at:
            await self._checks.deactivate(check_id)
            return ClaimResult(outcome=ClaimOutcome.EXPIRED, creator_id=creator_id)

        # Gate 4: max claims reached.
        max_claims = int(check.max_claims or 0)
        claims_count = int(check.claims_count or 0)
        if max_claims > 0 and claims_count >= max_claims:
            await self._checks.deactivate(check_id)
            return ClaimResult(outcome=ClaimOutcome.MAX_REACHED, creator_id=creator_id)

        # Gate 5: individual target mismatch.
        if (
            check.type == "individual"
            and check.target_user_id is not None
            and int(check.target_user_id) != user_id
        ):
            return ClaimResult(outcome=ClaimOutcome.WRONG_USER, creator_id=creator_id)

        # Gate 6: already claimed (friendly fast path; UNIQUE is the
        # authoritative guard at step 11).
        if await self._checks.has_claimed(check_id, user_id):
            return ClaimResult(outcome=ClaimOutcome.ALREADY_CLAIMED, creator_id=creator_id)

        # Gate 7: language.
        if check.required_language and (user_lang or "") != check.required_language:
            return ClaimResult(
                outcome=ClaimOutcome.WRONG_LANG,
                creator_id=creator_id,
                required_language=check.required_language,
            )

        # Gate 8: premium.
        if check.required_premium and not is_premium:
            return ClaimResult(outcome=ClaimOutcome.PREMIUM_ONLY, creator_id=creator_id)

        # Gate 8b: claim-time filter gates (L-85..L-88), legacy's order.
        gate = await self._filter_gate(
            check,
            user_id=user_id,
            now=now,
            claimant_activity=claimant_activity,
            claimant_country=claimant_country,
        )
        if gate is not None:
            return ClaimResult(outcome=gate, creator_id=creator_id)

        # NOTE: required_subscription is enforced at the CLAIM-SIDE
        # HANDLER (#26) — the claimer is verified as a channel member
        # BEFORE this method is called, so there is no subscription gate
        # here.

        # Gate 9: compute + clamp amount.
        remaining = int(check.remaining_amount)
        if check.type == "random" and check.min_amount is not None and check.max_amount is not None:
            amount = money_rng.randint(int(check.min_amount), int(check.max_amount))
        else:
            amount = int(check.fixed_amount or 0)
        amount = min(amount, remaining)
        # DIVERGENCE (documented, unreachable): legacy clamped UPWARD here
        # — ``if amount <= 0 or amount > remaining: amount = remaining``
        # (bot.py:10161-10162) — so a zero roll still paid out the rest of
        # the check. The port refuses instead. Unreachable for any check
        # this service created: creation rejects a non-positive
        # ``min_amount``/``fixed_amount`` (:meth:`create_check`, the three
        # validation gates below the spec parse), and an active check
        # always has ``remaining >= 1`` because :meth:`ChecksRepo.
        # claim_decrement` flips ``is_active`` the moment it drains. Left
        # as a refusal on purpose: paying ``remaining`` on an unlucky roll
        # would silently drain a random check in one claim.
        if amount <= 0:
            return ClaimResult(outcome=ClaimOutcome.NO_FUNDS, creator_id=creator_id)

        # Gates 10-12 are one SAVEPOINT (#1986): the decrement, the claim
        # row and the credit either all land or none do. It used to be
        # ``self._session.rollback()``, which undid the whole update —
        # this session is shared, ``middlewares/base.py`` hands out
        # exactly ONE per update, so an unlucky claimer also erased
        # everything else already written on it. Same argument as
        # :meth:`P2pService.cancel_order` (#1985) and #209's
        # ``expire_pending``.
        async with self._session.begin_nested() as savepoint:
            # Gate 10: atomic decrement (the drain-race fix). False means the
            # check drained / hit its cap between our read and this write.
            decremented = await self._checks.claim_decrement(check_id, amount, max_claims)
            if not decremented:
                return ClaimResult(outcome=ClaimOutcome.RACE_LOST, creator_id=creator_id)

            # Gate 11: durable claim row under the UNIQUE double-claim guard.
            try:
                await self._checks.insert_claim(check_id, user_id, amount, now)
            except IntegrityError:
                # A concurrent claim from the SAME user beat us to the
                # UNIQUE(check_id, user_id) row. The middleware only rolls
                # back on a RAISED exception — if we returned normally it
                # would COMMIT our decrement without the matching claim row,
                # leaking coins from the check. Undo the decrement ourselves,
                # then report ALREADY_CLAIMED. The credit (step 12) hasn't
                # run, so no coins were minted. Rolling the SAVEPOINT back
                # (#1986) rather than the session also clears the failed
                # IntegrityError — which is what makes the session usable
                # again — without discarding whatever else the update had
                # already written to the same shared session.
                await savepoint.rollback()
                return ClaimResult(outcome=ClaimOutcome.ALREADY_CLAIMED, creator_id=creator_id)

            # Gate 12: credit the claimer + ledger row. The credit MUST be
            # checked: ``EconomyRepo.credit`` returns ``None`` on failure
            # (balance-cap overflow or a vanished wallet). If we ignored that,
            # the decrement (Gate 10) + claim row (Gate 11) would still COMMIT
            # at request end — the check's coins consumed, but never delivered
            # to the claimer: a silent money leak (SEC audit). On failure roll
            # back the whole claim so nothing is consumed, mirroring the
            # IntegrityError path above.
            # ``credit`` is a guarded UPDATE: it matches zero rows when the
            # claimer has no wallet yet, and both claim entry points reach us
            # WITHOUT passing through the ``/start`` bootstrap — ``/чек <code>``
            # and the ``check_<code>`` deep link are both owned by the checks
            # router. A first-time claimer would therefore have got
            # CREDIT_FAILED for a perfectly valid check. Legacy seeded the row
            # for exactly this reason: ``add_coins`` opens with
            # ``register_user(user_id)`` / "Гарантируем, что пользователь
            # существует (иначе UPDATE ничего не изменит)" (bot.py:9724).
            await self._economy.get_or_create(user_id)
            credited = await self._economy.credit(user_id, amount)
            if credited is None:
                await savepoint.rollback()
                return ClaimResult(outcome=ClaimOutcome.CREDIT_FAILED, creator_id=creator_id)
            await self._ledger.record(
                from_id=None,
                to_id=user_id,
                amount=amount,
                reason="check_claim",
                type="check_claim",
                date=now,
            )
        # Activations left after THIS claim (RR-2 #18). ``max_claims == 0``
        # is legacy's "unlimited" sentinel → ``None`` → rendered as ∞.
        # Clamped at 0: the read at Gate 4 and the decrement at Gate 10 are
        # not one statement, so a concurrent claimer can push the real
        # count past ours — a receipt must never show a negative counter.
        claims_left = max(0, max_claims - (claims_count + 1)) if max_claims > 0 else None
        return ClaimResult(
            outcome=ClaimOutcome.OK,
            amount=amount,
            remaining=remaining - amount,
            creator_id=creator_id,
            code=normalized,
            claims_left=claims_left,
        )

    async def _filter_gate(
        self,
        check: Check,
        *,
        user_id: int,
        now: datetime,
        claimant_activity: int | None,
        claimant_country: str | None,
    ) -> ClaimOutcome | None:
        """Run the L-85..L-88 claim-time filter gates; ``None`` == pass.

        Order: blocked_users → min_age → min_activity →
        allowed_countries. Only the first three exist in legacy's
        ``_check_user_filters_for_check`` (bot.py:10052-10078), and they
        do run in that order there; ``allowed_countries`` is this port's
        own gate appended at the end (see :meth:`claim_check`). Two of
        the three also diverge in substance rather than order — legacy's
        ``min_age`` branch was a no-op and its ``min_activity`` counted
        ``user_history`` rows, both documented on the bullets in
        :meth:`claim_check`. All of it is latent on production today:
        the ``checks`` table is empty, and ``economy.users`` has no row
        with ``registered IS NULL`` for the fail-closed ``min_age`` to
        catch.

        A malformed JSON column FAILS OPEN for that single gate (legacy
        swallowed the parse error too) so a corrupt row never bricks an
        otherwise-valid claim.

        Reads the claimer's ``economy.users`` row at most ONCE (only
        when a gate that needs it is configured) directly off the shared
        session — no new method on the non-owned ``EconomyRepo``.
        """
        # L-88 blocked_users: pure data, no extra read.
        if check.blocked_users:
            blocked = _parse_int_list(check.blocked_users)
            if blocked is not None and user_id in blocked:
                return ClaimOutcome.BLOCKED_USER

        needs_user_row = bool(check.min_age) or (
            bool(check.min_activity) and claimant_activity is None
        )
        econ_user = await self._session.get(EconomyUser, user_id) if needs_user_row else None

        # L-85 min_age: account age in DAYS via ``registered`` — legacy
        # never evaluated this gate at all. A row with no ``registered``
        # timestamp can't be aged → fail the gate (conservative: an
        # unknown-age account doesn't satisfy a minimum-age requirement,
        # and stricter than legacy's unconditional pass).
        if check.min_age:
            registered = econ_user.registered if econ_user is not None else None
            if registered is None:
                return ClaimOutcome.MIN_AGE
            age_days = (now - registered).days
            if age_days < int(check.min_age):
                return ClaimOutcome.MIN_AGE

        # L-86 min_activity: ``claimant_activity`` is accepted but no
        # production caller passes it, so in practice this IS the
        # ``games_played`` proxy — not legacy's ``user_history`` count.
        # See the divergence note on :meth:`claim_check`'s bullet. When
        # neither source is available the gate is skipped rather than
        # rejecting a claim we can't evaluate.
        if check.min_activity:
            activity = claimant_activity
            if activity is None and econ_user is not None:
                activity = int(econ_user.games_played)
            if activity is not None and activity < int(check.min_activity):
                return ClaimOutcome.MIN_ACTIVITY

        # L-87 allowed_countries: BEST-EFFORT. No IP resolution and no
        # stored country in the economy schema, so we only enforce when
        # the handler supplies ``claimant_country``; otherwise PASS.
        if check.allowed_countries and claimant_country:
            allowed = _parse_str_list(check.allowed_countries)
            if allowed is not None and claimant_country.upper() not in allowed:
                return ClaimOutcome.COUNTRY_BLOCKED

        return None


def _parse_int_list(raw: str) -> list[int] | None:
    """Decode a JSON ``list[int]`` from a check column; ``None`` on any
    parse error (fail-open for that single gate, matching legacy)."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        out.append(item)
    return out


def _parse_str_list(raw: str) -> list[str] | None:
    """Decode a JSON ``list[str]`` (upper-cased) from a check column;
    ``None`` on any parse error (fail-open for that single gate)."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        out.append(item.upper())
    return out
