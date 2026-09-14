"""Atomic /use flow: read inventory entry → plan → consume + grant.

Stage 28 of 27/28/29. Composes :class:`InventoryRepo`,
:class:`VipRepo`, :class:`PrivilegesRepo`, :class:`ShopItemsRepo` and
the pure planner from Stage 27 into one transactional "consume one
inventory entry and apply its effect" step. The handler shell lands
in Stage 29; this stage only ships the service + integration tests
so the correctness story is pinned independently of the callback
plumbing.

Why a service (not a method on InventoryRepo)
---------------------------------------------
The operation crosses four tables (``inventory``, ``shop_items``,
``users``, ``user_privileges``) and three repos. The planner's
classification — UNKNOWN refuses to act, the two known kinds dispatch
to different repos — is a *taxonomy* decision; the consume + grant
sequencing is a *transaction* decision. Lumping them into one repo
method would tangle two abstraction levels and force a future
``color_nick`` planner kind to add its branch inside a "repo".
Mirrors the same shape as :class:`PurchaseService` (Stage 17) and
:class:`TransferService` (Stage 21): the repos stay table-shaped,
this service owns the multi-repo orchestration.

Atomicity contract
------------------
The four repos share ONE :class:`AsyncSession` (wired in
:class:`EconomyMiddleware`). The outer transaction boundary is the
middleware's per-update commit; this service issues statements and
relies on the boundary for "either consume + grant land together, or
neither does". Without that, a process crash between the
:meth:`InventoryRepo.consume` (which sets ``used = 1``) and the grant
write would leave the user's entry marked used with no grant in
return — visibly losing money.

The consume step itself is race-safe at the SQL level (see
``InventoryRepo.consume``): two concurrent /use clicks on the same
entry collapse to "exactly one wins, the other gets ALREADY_USED".
That guarantee survives even without the outer transaction, because
SQLite serialises writes and the rowcount guard is checked before
the grant write runs.

Order of operations
-------------------
1. Read the entry (``InventoryRepo.get_for_user`` — already
   constrains to the calling user, so cross-user attempts surface as
   NOT_FOUND without leaking row existence).
2. Cheap rejections from the entity's own state (``used``, expired
   ``expires``) — short-circuits before the planner and the consume
   UPDATE, so a re-click on a long-since-used row pays nothing.
3. Catalog lookup via ``ShopItemsRepo.get`` to feed the planner. The
   planner needs the *catalog* row (with the operator-current
   ``type`` and ``name``) — the inventory entry only carries
   denormalised display fields, not the type discriminator. A
   catalog row that was deleted between purchase and /use is rare
   but real (admin /admin_shop delete); we surface it as UNKNOWN_EFFECT
   because the planner can't classify a missing row, same as it
   would for an unrecognised type.
4. Plan via :func:`plan_effect_application`. UNKNOWN → return
   UNKNOWN_EFFECT *without* mutating anything. Nothing picks those
   item types up afterwards — legacy's auto-apply went with the
   legacy process — so an UNKNOWN row is an item its owner paid for
   and cannot use; #2005 rewrote the card that used to promise
   otherwise. Refusing to consume is the least-bad half of that: the
   entry at least stays in the inventory.
5. Race-safe consume via ``InventoryRepo.consume``. If it returns
   False, someone else won the race between step 1's read and this
   UPDATE — surface as ALREADY_USED.
6. Dispatch to the grant repo per kind. Step 5 winning means we own
   the entry; the grant write either succeeds or rolls back the
   consume via the outer transaction boundary.

Step 2's reads are intentionally informational rather than
authoritative — the ``consume`` UPDATE re-checks ``used = 0`` in its
WHERE clause and is the authoritative race guard. The Python-side
reject on ``used`` exists only to avoid running the planner + catalog
lookup for a row we'd reject anyway, and to surface ALREADY_USED for
the common case where a user double-taps a button on a slow network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.services.inventory_use_planner import (
    InventoryEffectKind,
    plan_effect_application,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
    from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
    from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.repositories.vip_repo import VipRepo


log = logger.bind(component="services.inventory_use")


class UseOutcome(StrEnum):
    """Mutually-exclusive results of a /use attempt.

    Each value maps to one user-facing branch in Stage 29's handler
    (success card, "not yours" toast, "already redeemed" toast, …).
    StrEnum mirrors the convention :class:`PurchaseStatus` and
    :class:`TransferOutcome` already follow — log lines render the
    name directly and a future audit log can store the outcome as
    plain text without an extra str() call.
    """

    SUCCESS = "success"
    """Entry consumed, grant written, both under one commit."""

    NOT_FOUND = "not_found"
    """Entry doesn't exist OR belongs to another user. Same value
    on purpose: the repo intentionally collapses the two cases so
    a hand-crafted callback with someone else's ``entry_id`` can't
    tell the attacker whether the id is real. See
    ``InventoryRepo.get_for_user`` for the auth posture."""

    ALREADY_USED = "already_used"
    """Either the pre-check found ``used = True`` on the entity, or
    the race-safe ``consume`` UPDATE returned rowcount 0 (someone
    else won between read and UPDATE). User-visible message is
    identical for both — it's the same condition timed differently."""

    EXPIRED = "expired"
    """The entry's own ``expires`` is past ``now``. Distinct from
    ALREADY_USED because the user never got value out of the item —
    a future "refund expired entry to coins" policy decision will
    need this branch to be its own outcome."""

    UNKNOWN_EFFECT = "unknown_effect"
    """The planner returned UNKNOWN. Service refuses to act; the
    Stage 29 handler surfaces a "this item doesn't apply
    automatically yet" toast and the legacy auto-apply path (still
    wired on the legacy code path) remains responsible. UNKNOWN is
    the safe default, not an error — adding a new kind is additive."""

    NEEDS_TITLE_INPUT = "needs_title_input"
    """The planner classified a ``custom_title`` item. Unlike every
    other kind, activation is two-step: the user must type the title
    text. The service does NOT consume the entry or write any grant —
    it surfaces the entry/expiry so the handler can hand off to the
    FSM flow (``handlers/custom_title.py``). The consume + grant happen
    at the end of the FSM step, mirroring legacy's
    ``register_next_step_handler`` → ``process_custom_title``. The entry
    is verified present/unused/unexpired before this outcome is
    returned, so the FSM can trust the entry id it's handed."""

    BUSTER_ALREADY_ACTIVE = "buster_already_active"
    """#1903: a ``double_daily`` buster is already armed for this user,
    so this one has nothing to arm. The service does NOT consume the
    entry — the item stays in ``/inventory`` for the next claim.

    Refuse-without-consume rather than stacking charges: the row is
    presence-only and carries ONE ``expires_at``, so five banked
    charges under one 3-day window would still expire four of them
    (``/daily`` spends at most one a day). Keeping the item is the
    only shape that loses nothing. ``buster_expires_at`` carries the
    STANDING row's expiry — when the user can spend what they already
    have — and is None only for a never-expiring legacy row."""

    NEEDS_MODERATION = "needs_moderation"
    """The planner classified an ``unwarn`` item. The effect removes a
    warning in ``moderation.db`` — a DB this service has no session
    for — so it does NOT consume the entry or touch moderation state.
    It surfaces the entry id so the callback handler can run the
    moderation read (does an active warning exist?) and, only if one is
    removed, consume the inventory entry. Refuse-without-consume on
    no-warning is the handler's responsibility; the entry is verified
    present/unused/unexpired before this outcome is returned."""


@dataclass(frozen=True, slots=True)
class UseResult:
    """What a /use call produced. Read ``outcome`` first.

    The success-path fields (``kind``, ``granted_till``,
    ``buster_expires_at``) are None on failure so the handler can
    branch on ``outcome`` and feel safe accessing the relevant field
    for SUCCESS without per-kind ``is not None`` guards inside that
    branch. ``kind`` is populated on SUCCESS only — failure paths
    carry None because no plan was applied (UNKNOWN_EFFECT *did*
    run the planner, but the kind in that case is always
    :attr:`InventoryEffectKind.UNKNOWN` and surfacing it on the
    result would let a future caller branch on it where it should
    branch on the outcome enum instead).
    """

    outcome: UseOutcome
    kind: InventoryEffectKind | None = None
    granted_till: datetime | None = None
    """For VIP_GRANT success: the user's ACTUAL resulting ``vip_till``.

    Authoritative, not an estimate — :meth:`VipRepo.grant_global`
    returns the expiry the database resolved and the dispatch below
    hands that value straight through. So "VIP extended to
    <granted_till>" in the handler is exactly what the row holds.

    This docstring used to say the opposite: that the field was
    ``now + duration_days``, that the real expiry could be later
    because MAX semantics might pick an existing longer grant, and
    that the service stayed write-only with no read-after-write.
    All three were true of the #192 port and none survived its fix.
    The repo STACKS now (``vip_till = MAX(IFNULL(vip_till, 0), :now)
    + :duration_seconds``), so a second purchase adds its days
    instead of collapsing into a no-op, and the value IS read back
    inside the caller's transaction because only the database knows
    what the row held."""
    buster_expires_at: datetime | None = None
    """For DOUBLE_DAILY_BUSTER success: ``now + duration_seconds`` —
    the safety TTL this call asked for, NOT necessarily the row's.

    Unlike ``granted_till`` above (the cross-reference this comment
    used to make is dead): :meth:`PrivilegesRepo.grant_buster` keeps
    MAX semantics and returns nothing, so an existing longer buster's
    expiry wins and we do not re-read it. Today every buster is the
    same 3-day TTL, which makes the difference unobservable."""
    coin_payout_amount: int | None = None
    """For LUCK_COIN_PAYOUT success: the exact number of coins the
    service CREDITED to the wallet (rolled by
    :meth:`CoinPayoutSpec.generate_payout`). Authoritative — it's the
    value that went into the atomic ``EconomyRepo.credit`` UPDATE, so
    the handler renders "🍀 ты получил <coin_payout_amount> 🪙" against
    the exact amount the user's balance moved by. None on every other
    kind (and on failure, where no credit landed)."""
    color_nick_expires_at: datetime | None = None
    """For COLOR_NICK success: ``now + duration_days`` — the exact
    expiry the privilege row was written with. Unlike ``granted_till``
    above, this value is AUTHORITATIVE (no MAX shadowing): the
    color_nick write uses REPLACE semantics (see
    :meth:`PrivilegesRepo.grant_with_value`), so the stored
    ``expires_at`` is exactly what the spec computed. Handler renders
    "color active until <color_nick_expires_at>" against it."""
    mute_protection_expires_at: datetime | None = None
    """For MUTE_PROTECTION success: ``now + duration_hours`` — the
    exact expiry the privilege row was written with. Authoritative
    (same REPLACE-not-MAX posture as color_nick: see
    :meth:`PrivilegesRepo.grant_with_value`), so the stored
    ``expires_at`` is exactly what the spec computed. Handler renders
    "защита от мута активна до <mute_protection_expires_at>"."""
    xp_boost_expires_at: datetime | None = None
    """For XP_BOOST success: ``now + duration_minutes`` — the exact
    expiry the privilege row was written with. Authoritative (REPLACE
    semantics via :meth:`PrivilegesRepo.grant_with_value`). Handler
    renders "ускорение x<N> активно до <xp_boost_expires_at>"."""
    xp_boost_multiplier: int | None = None
    """For XP_BOOST success: the integer multiplier stored in the
    grant payload (legacy default 2). Surfaced so the success card can
    render the exact factor the user activated."""
    pending_item_id: int | None = None
    """For NEEDS_TITLE_INPUT / NEEDS_MODERATION: the catalog ``item_id``
    of the entry the handler must finish redeeming (FSM title step, or
    moderation unwarn). None on every other outcome. The entry itself
    is NOT consumed for these outcomes — the handler owns the consume
    so the refuse-without-consume / abandon-without-consume contracts
    hold."""


class InventoryUseService:
    """Atomic /use execution: consume one entry + apply its grant."""

    def __init__(
        self,
        inventory_repo: InventoryRepo,
        vip_repo: VipRepo,
        privileges_repo: PrivilegesRepo,
        shop_items_repo: ShopItemsRepo,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        *,
        random_int_inclusive: Callable[[int, int], int] | None = None,
        group_rebate_percent: int = 0,
    ) -> None:
        self._inventory = inventory_repo
        self._vip = vip_repo
        self._privileges = privileges_repo
        self._shop = shop_items_repo
        # EconomyRepo is bound to the SAME session as the other repos
        # (wired in EconomyMiddleware) so the LUCK_COIN_PAYOUT credit
        # commits atomically with the ``inventory.consume`` UPDATE —
        # the "consume + grant land together or neither" contract this
        # module's docstring promises also covers the coin payout.
        self._economy = economy_repo
        # Same session again: the LUCK_COIN_PAYOUT ledger row is the
        # companion to that credit and must roll back with it.
        self._ledger = transactions_repo
        # Injected only for deterministic LUCK_COIN_PAYOUT tests: the
        # planner rolls the RNG amount, so pinning it here makes the
        # payout reproducible. None → planner defaults to
        # ``random.randint`` in production.
        self._random_int_inclusive = random_int_inclusive
        # #1930: what a group-scoped purchase of this item pays back to
        # the chosen group, whose creator can be the buyer. The planner
        # needs it to decide whether a luck row is still a coin sink;
        # see its ``luck`` branch. Default 0 keeps every call site that
        # does not sell group-scoped items behaviour-identical.
        self._group_rebate_percent = group_rebate_percent

    async def use(
        self,
        *,
        user_id: int,
        entry_id: int,
        now: datetime,
    ) -> UseResult:
        """Try to redeem entry ``entry_id`` for ``user_id``.

        ``now`` is required (not optional with a default) so the
        service stays clock-free — Stage 29's handler injects the
        middleware's per-update instant, and tests pin a fixed
        moment. The same instant flows to the planner, the consume
        ``used_date`` and the grant's expiry, so the audit trail
        and the grant TTL agree to the microsecond.
        """
        # Step 1: read entry with ownership baked into the SELECT.
        entry = await self._inventory.get_for_user(user_id, entry_id)
        if entry is None:
            return UseResult(outcome=UseOutcome.NOT_FOUND)

        # Step 2: cheap rejects from the entity's own state.
        if entry.used:
            return UseResult(outcome=UseOutcome.ALREADY_USED)
        if entry.expires is not None and entry.expires <= now:
            return UseResult(outcome=UseOutcome.EXPIRED)

        # Step 3: catalog lookup for the planner. ``None`` here means
        # the catalog row was deleted between purchase and /use —
        # rare (admin /admin_shop delete) but real. Treat the same
        # as an unclassified type: refuse to act rather than guess.
        item = await self._shop.get(entry.item_id)
        if item is None:
            return UseResult(outcome=UseOutcome.UNKNOWN_EFFECT)

        # Step 4: pure planning. The injected RNG (if any) flows to
        # the LUCK_COIN_PAYOUT spec so tests can pin the rolled amount.
        plan = plan_effect_application(
            item,
            now=now,
            random_int_inclusive=self._random_int_inclusive,
            group_rebate_percent=self._group_rebate_percent,
        )
        if plan.kind is InventoryEffectKind.UNKNOWN:
            # #1790: the only place an unusable catalog row becomes
            # visible to whoever can fix it. Every route to UNKNOWN is a
            # catalog problem the buyer cannot see and did not cause —
            # a ``type`` with no effect implementation, a ``luck`` row
            # with no usable bounds, or a ``luck`` row whose expected
            # payout now exceeds its price — and all three end the same
            # way: he paid and cannot redeem. Warning rather than info
            # because it is never normal, and carrying the row's own
            # identity because the fix is one /admin_shop edit and the
            # operator needs to be told WHICH row.
            log.bind(uid=user_id, item_id=item.id, item_type=item.type, price=item.price).warning(
                "inventory /use refused: catalog row plans to UNKNOWN"
            )
            return UseResult(outcome=UseOutcome.UNKNOWN_EFFECT)

        # Step 4b: two-step / cross-DB kinds that this service must NOT
        # consume. ``custom_title`` needs the user to type the title via
        # FSM; ``unwarn`` needs a ``moderation.db`` session this service
        # doesn't hold. Both surface the entry id so the callback handler
        # can finish the redemption (and, crucially, own the consume) —
        # the refuse-without-consume (unwarn, no warning) and
        # abandon-without-consume (custom_title, never typed) contracts
        # both require the consume to happen AFTER the handler-side step,
        # not here. The entry has already passed the used/expired
        # pre-checks above, so the handler can trust the id.
        if plan.kind is InventoryEffectKind.CUSTOM_TITLE:
            return UseResult(
                outcome=UseOutcome.NEEDS_TITLE_INPUT,
                kind=plan.kind,
                pending_item_id=entry.item_id,
            )
        if plan.kind is InventoryEffectKind.UNWARN:
            return UseResult(
                outcome=UseOutcome.NEEDS_MODERATION,
                kind=plan.kind,
                pending_item_id=entry.item_id,
            )

        # Step 4c: #1903 — refuse a second buster while one is armed.
        # The shop card promises the NEXT ``/daily`` will be doubled, so
        # one item buys one doubled claim; but the row that carries it
        # is presence-only (``PrivilegesRepo.grant_buster`` upserts one
        # ``(user, 'double_daily', 0)`` slot with MAX semantics on the
        # expiry) and ``/daily`` deletes it on a successful claim. There
        # is no "two" to write. Before this check, step 5 consumed the
        # entry and step 6 re-upserted the row the user already had —
        # the second item was paid for and destroyed. Same shape #192
        # fixed for VIP, where MAX-on-expiry made a repeat purchase a
        # paid no-op; the conservative half of that fix applies here,
        # because banking charges would need a second clock (one
        # ``expires_at`` cannot keep five charges alive when only one
        # ``/daily`` a day can spend them).
        #
        # This sits with the other refuse-without-consume kinds above,
        # BEFORE step 5, which is the whole point: the entry survives.
        # ``PurchaseService`` never writes ``InventoryItem.expires``, and
        # ``InventoryRepo.cleanup_expired`` only deletes rows with a
        # non-NULL past expiry, so a refused buster waits indefinitely.
        if plan.kind is InventoryEffectKind.DOUBLE_DAILY_BUSTER:
            assert plan.privilege_grant is not None  # noqa: S101 — planner invariant
            # Take the writer lock before the gate read. Everything
            # above this line is a SELECT, and this project opens the
            # transaction lazily and only for writes
            # (``db/engines.py:204-210``), so without it the read below
            # and the consume + grant below that are not serialised:
            # two taps on two different spare busters both see "nothing
            # armed", and neither existing guard catches them —
            # ``consume`` is keyed on ``inventory_id`` (different for
            # each tap) and ``grant_buster`` collapses the two grants
            # into one row by MAX. Two items in, one buster out. The
            # lock writes nothing, so a refusal releases it having
            # changed nothing.
            await self._privileges.lock_writer()
            # ``now`` is naive LOCAL here (the handler passes
            # ``datetime.now()`` to match the legacy inventory
            # convention), and ``get_active`` guards against exactly
            # that with :func:`unix_ts`, which assumes UTC for a naive
            # input — three hours off on the MSK host, in the direction
            # that keeps an expired buster looking armed. ``astimezone``
            # on a naive value attaches the LOCAL zone, which is the
            # clock ``grant_buster`` wrote the row with, so the epochs
            # line up and the guard has nothing to correct.
            armed = await self._privileges.get_active(
                user_id,
                plan.privilege_grant.privilege_type,
                now=now.astimezone() if now.tzinfo is None else now,
            )
            if armed is not None:
                return UseResult(
                    outcome=UseOutcome.BUSTER_ALREADY_ACTIVE,
                    kind=plan.kind,
                    # ``expires_at <= 0`` is legacy's "never expires"
                    # slot; this pipeline never writes it, but reading
                    # one back as 1970 on the card would be worse than
                    # saying nothing, so the date is dropped instead.
                    buster_expires_at=(
                        datetime.fromtimestamp(armed.expires_at)  # noqa: DTZ006 — naive local, see above
                        if armed.expires_at > 0
                        else None
                    ),
                )

        # Step 5: race-safe consume. False here means another /use
        # click won between step 1's read and this UPDATE — surface
        # ALREADY_USED. The outer transaction stays clean because
        # nothing was written yet.
        consumed = await self._inventory.consume(user_id=user_id, inventory_id=entry_id, now=now)
        if not consumed:
            return UseResult(outcome=UseOutcome.ALREADY_USED)

        # Step 6: dispatch the grant. Pattern-match on kind; the
        # planner's invariant guarantees the matching spec field is
        # populated, so the assertion + access is safe (and mypy
        # gets the narrowing through the explicit ``is not None``
        # check rather than via the enum branch alone).
        #
        # #286 audit — why ``assert`` is allowed to stay in a service
        # here when the /duel, /cpc and /pvp payouts had to give theirs
        # up (#263). Those seven stood between a FAILED wallet write and
        # a wrong balance: swallow one and coins were minted or burned.
        # These stand in front of a field the planner filled in two steps
        # ago, and every one of them is followed by more work inside the
        # same transaction — so if one ever fires, the rollback undoes
        # the ``consume`` above and nothing moved. What is being narrowed
        # is a shape, not an outcome. Each carries its own ``noqa`` with
        # a reason rather than resting on a blanket permission.
        if plan.kind is InventoryEffectKind.VIP_GRANT:
            assert plan.vip_grant is not None  # noqa: S101 — planner invariant, see step 6
            # #192: the repo stacks (legacy ``bot.py:13442-13444``), so
            # the resulting expiry is whatever the row already held plus
            # this item's duration — only the database can say. Passing
            # ``now + duration`` and trusting it back as the answer is
            # what made a second VIP purchase silently free of days.
            till = await self._vip.grant_global(
                user_id=user_id,
                now=now,
                duration=timedelta(days=plan.vip_grant.duration_days),
            )
            return UseResult(
                outcome=UseOutcome.SUCCESS,
                kind=plan.kind,
                granted_till=till,
            )

        if plan.kind is InventoryEffectKind.COLOR_NICK:
            assert plan.color_nick_grant is not None  # noqa: S101 — planner invariant
            cspec = plan.color_nick_grant
            expires_at = cspec.granted_till_from(now)
            # JSON-encode the {"color": "<name>"} payload here (not in
            # the repo) — the repo column is TEXT, the JSON shape is a
            # service-level convention shared with legacy
            # ``set_privilege`` (``bot.py:13280``) which also calls
            # ``json.dumps`` at the call site. ``ensure_ascii=False`` is
            # not needed: color names are ASCII; not asking for it
            # matches the default and avoids the import-bikeshed.
            value_json = json.dumps({"color": cspec.color})
            # #1950: the repo decides the resulting expiry — a repeat of
            # the SAME color extends the active window instead of
            # replacing it, so ``expires_at`` computed above is only the
            # duration this item contributes. The copy renders what the
            # row actually ends up holding.
            expires_at = await self._privileges.grant_with_value(
                user_id=user_id,
                privilege_type="color_nick",
                value=value_json,
                now=now,
                duration=expires_at - now,
            )
            return UseResult(
                outcome=UseOutcome.SUCCESS,
                kind=plan.kind,
                color_nick_expires_at=expires_at,
            )

        if plan.kind is InventoryEffectKind.MUTE_PROTECTION:
            assert plan.mute_protection_grant is not None  # noqa: S101 — planner invariant
            mspec = plan.mute_protection_grant
            expires_at = mspec.granted_till_from(now)
            # Legacy ``apply_mute_protection`` (``bot.py:13638``) pins
            # ``value={}`` unconditionally — the read-side check
            # (``has_mute_protection`` at ``bot.py:13647``) inspects
            # nothing inside the payload, so the empty object is purely
            # a schema placeholder for the TEXT column. Encode inline
            # rather than via :func:`json.dumps` of ``{}`` — the result
            # is a fixed two-byte literal and the spec deliberately
            # carries no ``value`` field (see the spec docstring).
            # #1950: presence-only payload, so every repeat redemption
            # matches and extends — two 24 h items are 48 h of cover.
            expires_at = await self._privileges.grant_with_value(
                user_id=user_id,
                privilege_type="mute_protection",
                value="{}",
                now=now,
                duration=expires_at - now,
            )
            return UseResult(
                outcome=UseOutcome.SUCCESS,
                kind=plan.kind,
                mute_protection_expires_at=expires_at,
            )

        if plan.kind is InventoryEffectKind.XP_BOOST:
            assert plan.xp_boost_grant is not None  # noqa: S101 — planner invariant
            xspec = plan.xp_boost_grant
            expires_at = xspec.granted_till_from(now)
            # JSON-encode the {"multiplier": N} payload here (not in the
            # repo) — same convention as color_nick above and legacy
            # ``set_privilege`` (``bot.py:13280``). REPLACE semantics:
            # a fresh boost overwrites any active one (matches legacy's
            # unconditional cache write in ``apply_xp_boost``).
            value_json = json.dumps({"multiplier": xspec.multiplier})
            # #1950: same multiplier extends, a different one replaces —
            # two x2 boosts cannot be folded into one row, so the only
            # honest merge for equal payloads is to add their windows.
            expires_at = await self._privileges.grant_with_value(
                user_id=user_id,
                privilege_type="xp_boost",
                value=value_json,
                now=now,
                duration=expires_at - now,
            )
            return UseResult(
                outcome=UseOutcome.SUCCESS,
                kind=plan.kind,
                xp_boost_expires_at=expires_at,
                xp_boost_multiplier=xspec.multiplier,
            )

        if plan.kind is InventoryEffectKind.DOUBLE_DAILY_BUSTER:
            assert plan.privilege_grant is not None  # noqa: S101 — planner invariant
            spec = plan.privilege_grant
            # ``duration_seconds`` is non-None for the double_daily
            # buster (planner pins the 3-day safety TTL); the
            # assertion documents that contract for future readers
            # who might wonder why the timedelta math is safe.
            assert spec.duration_seconds is not None  # noqa: S101 — pinned by planner
            expires_at = now + timedelta(seconds=spec.duration_seconds)
            await self._privileges.grant_buster(
                user_id=user_id,
                privilege_type=spec.privilege_type,
                expires_at=expires_at,
            )
            return UseResult(
                outcome=UseOutcome.SUCCESS,
                kind=plan.kind,
                buster_expires_at=expires_at,
            )

        if plan.kind is InventoryEffectKind.LUCK_COIN_PAYOUT:
            assert plan.coin_payout is not None  # noqa: S101 — planner invariant; the
            # credit BELOW is the money guard, and it is a real ``if``
            amount = plan.coin_payout.generate_payout()
            credited = await self._economy.credit(user_id, amount)
            if credited is None:
                # SEC class (same posture as TransferService /
                # WithdrawService unchecked-credit hardening): a None
                # return means the credit UPDATE matched no row — the
                # wallet vanished, or the post-credit balance would
                # breach the ``_MAX_AMOUNT`` ceiling. The entry is
                # already consumed (step 5 set ``used = 1``); leaving
                # it that way would lose the payout with no item to
                # retry. Raise so the OUTER transaction rolls back the
                # consume too, honouring this module's "consume + grant
                # land together or neither" contract — the user keeps
                # an unused item and can /use again (e.g. after spending
                # down past the cap). The handler surfaces a crash-y
                # toast, but no money/item is lost, same as every other
                # grant path's rollback-on-failure story.
                msg = (
                    f"InventoryUseService: LUCK_COIN_PAYOUT credit failed for "
                    f"user_id={user_id} amount={amount} (None — wallet missing "
                    f"or balance-cap overflow); rolling back the consume."
                )
                raise RuntimeError(msg)
            # #225: only reachable once the credit is known to have
            # landed (the branch above raises otherwise). Without this
            # row a gift payout was money appearing from nowhere as far
            # as /balance's cashflow and any supply audit were
            # concerned; legacy booked it through ``add_coins``
            # (bot.py:13048, reason "Подарок из магазина").
            await self._ledger.record(
                from_id=None,
                to_id=user_id,
                amount=amount,
                reason="shop gift payout",
                # #1905: this row and the ``inventory.used_date``
                # written a few lines up are two halves of one
                # atomic payout, so they have to share one clock.
                # Left to its default the repo stamps naive UTC
                # (transactions_repo.py:376) while the consume
                # stamps the injected naive LOCAL instant, and on
                # MSK the audit trail shows the two halves three
                # hours apart. ``PurchaseService`` threads its own
                # ``now`` for the same reason
                # (purchase_service.py:223).
                date=now,
                type="gift_payout",
            )
            return UseResult(
                outcome=UseOutcome.SUCCESS,
                kind=plan.kind,
                coin_payout_amount=amount,
            )

        # Defensive: a future planner kind reaching here without a
        # matching branch above would silently consume the entry with
        # no grant. Refuse loudly — the test suite's "every kind is
        # handled" assertion will catch it before prod.
        msg = f"InventoryUseService: unhandled plan kind {plan.kind!r}"
        raise RuntimeError(msg)
