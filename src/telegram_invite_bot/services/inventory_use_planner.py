"""Pure planner for the "🎁 Use" inventory callback (Stage 27 of 27/28/29).

Stage 27 — pure planner only. Stage 28 will wire an ``InventoryUseService``
that consumes :class:`EffectPlan` and performs the actual repo writes;
Stage 29 ships the callback handler + e2e coverage. Keeping the
classification logic in a dependency-free module means Stage 28's
service is mostly "given a plan, do the writes" — easy to review on its
own — and means this stage is unit-testable without touching the DB,
the Telegram client, or even an event loop.

Why a separate "planner" instead of letting the service make decisions
inline? Two reasons:

* The classification is fundamentally a *taxonomy* decision (what does
  this catalog row mean?) and the legacy code at ``bot.py:13141-13180``
  dispatches on ``item.type`` with multiple branches per type. Pulling
  the branching out lets the service stay a flat sequence of writes,
  and lets Stage 28 cover "the service refuses to apply UNKNOWN" with
  one test instead of N "the service does not call repo X" cross-
  product tests.
* Stage 29's handler eventually wants to render different reply text
  per :class:`InventoryEffectKind` ("VIP extended until ...", "Buster
  armed for your next /daily"). A planner output naturally carries
  the kind discriminator the handler will branch on.

Classification key: ``ShopItemEntity.type``
-------------------------------------------
The legacy bot (``bot.py:13144`` and ``bot.py:13759``) dispatches on
the ``type`` column of ``shop_items``, not on ``name`` and not on
``id``. ``type`` is also the only stable column for this — names are
human-edited in the catalog (the four VIP rows are literally named
"👑 VIP (1 месяц)" / "(3 месяца)" / "(6 месяцев)" / "(1 год)") and
ids shift across environments (local vs server economy.db). So the
planner is type-keyed too.

The wrinkle: legacy pulls every per-item parameter out of the
``shop_items.data`` JSON blob — ``init_default_items`` seeds it at
``bot.py:12329`` and the dispatch reads it back at
``bot.py:13145-13195``. :class:`ShopItemEntity` used to drop ``data``,
which is what forced this planner to key off item NAMES; #192 put the
blob back on the entity (``core/entities/shop.py:71-87``) after three
paid-for SKUs turned out to be named nothing like the tables they were
matched against.

So every parameterised kind now resolves in legacy's own order:
``item.data`` first, then the curated name table, then legacy's own
default. The name tables survive only as that middle step, kept because
``handlers/vip.py`` renders its plan cards off
:data:`VIP_NAME_DURATIONS` and shop copy must not drift from what
:func:`plan_effect_application` actually grants. No row of a known type
classifies as UNKNOWN for want of a matching name any more: the buyer
paid, legacy would have paid them the default, and a default beats an
item that is permanently unusable with no refund path.

Effects still deferred to UNKNOWN:
    Note: ``legend`` — permanent grant, needs a sentinel for "no
        expiry" on PrivilegesRepo first.
    Note: ``custom_color`` and ``ad`` — legacy itself calls these
        deprecated and zeroes their stock (``bot.py:12748-12754``), so
        there is no live row to classify. The same tuple also lists
        ``color_nick`` and ``custom_title``, which this planner does
        handle: zero stock is a catalog decision about what may still
        be BOUGHT, not a reason to brick an item somebody already owns.

Kinds shipped:
    * DOUBLE_DAILY_BUSTER — single PrivilegesRepo write, no payload,
      3-day TTL (matches legacy ``apply_double_daily`` exactly).
    * VIP_GRANT — single VipRepo write with computed expiry; duration
      from ``data["duration"]``, then :data:`VIP_NAME_DURATIONS`, then
      legacy's 30 days (``bot.py:13155``).
    * COLOR_NICK — single PrivilegesRepo write under privilege_type
      ``color_nick`` with a ``{"color": "<name>"}`` JSON payload and
      a time-bounded expiry. Mirrors legacy ``apply_color_nick``
      (``bot.py:13406``): duration and colour from ``data``, then the
      canonical operator-shipped name (``"🌈 Цветной ник"`` → 7 days,
      matching the /admin_shop help example at ``bot.py:34095``), then
      legacy's own 7 days / ``"rainbow"`` (``bot.py:13149-13150``).
    * LUCK_COIN_PAYOUT — immediate coin payout via RNG, mirrors legacy
      ``apply_luck_gift`` (``bot.py:13582``, dispatched from
      ``bot.py:13159-13173``). Supports canonical weighted
      distributions for "Большой подарок" (100-500 coins, weighted
      toward 200) and "Секретный подарок" (10-100 coins, weighted
      toward 50), plus uniform RNG over the ``data.min``/``data.max``
      span the operator configured. RNG injection enables
      deterministic testing. A row with neither a known shape nor a
      usable span is the one place UNKNOWN survives — there is no
      payout figure we could name that would not be invented.
    * MUTE_PROTECTION — single PrivilegesRepo write under privilege_type
      ``mute_protection`` with an EMPTY JSON object payload (``"{}"``)
      and a time-bounded expiry. Mirrors legacy ``apply_mute_protection``
      (``bot.py:13633``) exactly: ``value={}``, duration in HOURS from
      ``data["duration"]``, then the seeded catalog row
      (``bot.py:12386-12393``), then legacy's 24 h (``bot.py:13180``).
      The presence + non-expired ``expires_at`` is the entire signal —
      the read-side ``has_mute_protection`` (``bot.py:13647``) inspects
      nothing inside the payload, so the empty object is purely a schema
      placeholder for the TEXT column. That read stays in legacy until
      the moderation flow ports; the two halves are independent (writer
      uses ``privileges`` rows; reader is also ``privileges`` rows +
      cache, so legacy reads continue to see the grants this kind
      writes).
    * XP_BOOST — single PrivilegesRepo write; duration in MINUTES and
      multiplier from ``data``, then the seeded ``"⚡ Ускорение"`` row
      (``bot.py:12402-12409``), then legacy's 60 min / x2
      (``bot.py:13185``).
    * CUSTOM_TITLE — classified for ANY ``type='custom_title'`` row: the
      7-day duration is fixed in legacy regardless of the catalog name,
      and the variable part (the title text) is collected from the user
      via FSM rather than read off the row. The service returns a
      "needs title input" outcome WITHOUT consuming; the FSM handler
      does the consume + grant once the title lands.
    * UNWARN — moderation side-effect living in ``moderation.db``, so
      the service returns a NEEDS_MODERATION outcome and the callback
      handler performs the moderation write plus the consume.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from telegram_invite_bot.utils.rng import money_rng

if TYPE_CHECKING:
    from telegram_invite_bot.core.entities.shop import ShopItemEntity


class InventoryEffectKind(StrEnum):
    """Taxonomy discriminator for :class:`EffectPlan`.

    String-valued (StrEnum) so loguru-bound plans render readably
    and so a future "log every applied effect" telemetry surface
    can group by kind without an extra str() call. Matches the
    convention :class:`PurchaseStatus` already follows.
    """

    DOUBLE_DAILY_BUSTER = "double_daily_buster"
    """Single-use buster armed on PrivilegesRepo; consumed by the
    next /daily claim. No payload beyond the row's presence."""

    VIP_GRANT = "vip_grant"
    """Time-bounded global VIP grant on VipRepo. ``duration_days``
    is item-specific (30 / 90 / 180 / 365)."""

    COLOR_NICK = "color_nick"
    """Time-bounded PrivilegesRepo grant under privilege_type
    ``color_nick`` carrying a ``{"color": "<name>"}`` JSON payload.
    7-day duration for the canonical operator catalog row; other
    color_nick rows defer to UNKNOWN until the planner learns their
    durations."""

    MUTE_PROTECTION = "mute_protection"
    """Time-bounded PrivilegesRepo grant under privilege_type
    ``mute_protection`` carrying a fixed empty JSON payload (``"{}"``).
    24-hour duration for the canonical seeded catalog row; other
    mute_protection rows defer to UNKNOWN. The legacy read-side
    (``has_mute_protection``) still lives in the moderation flow and
    consumes the grants this kind writes — both sides share the
    ``privileges`` table."""

    LUCK_COIN_PAYOUT = "luck_coin_payout"
    """One-shot coin payout via RNG; NOT a grant-based effect.
    Supports both simple uniform RNG (min_coins to max_coins) and
    weighted distributions for canonical name-based luck items
    (Большой подарок, Секретный подарок). RNG seeding injected
    via random_int_inclusive callback for deterministic testing."""

    XP_BOOST = "xp_boost"
    """Time-bounded PrivilegesRepo grant under privilege_type
    ``xp_boost`` carrying a ``{"multiplier": <int>}`` JSON payload and a
    minute-scoped expiry. Mirrors legacy ``apply_xp_boost``
    (``bot.py:13561``) and the canonical ``init_default_items`` seed
    (``bot.py:12402-12409``): 60-minute duration, x2 multiplier. The
    per-message earn path (``middlewares/message_activity.py``) reads
    this grant back via :func:`services.xp_boost.active_xp_multiplier`
    and multiplies the coins it credits. Operator-seeded variants with
    a non-canonical name defer to UNKNOWN — same posture VIP /
    color_nick / mute_protection take for unrecognised names, because
    the legacy seed reads both ``duration`` and ``multiplier`` off the
    ``data`` JSON blob the entity drops."""

    CUSTOM_TITLE = "custom_title"
    """Two-step grant: activation prompts the user for a free-text
    title via FSM, then writes a PrivilegesRepo row under
    privilege_type ``custom_title`` carrying ``{"title": "<text>"}``
    with a 7-day expiry (legacy ``apply_custom_title`` at
    ``bot.py:13681``, duration 7). Unlike every other kind, the planner
    classifies it but :class:`InventoryUseService` does NOT consume or
    grant — it returns a "needs title input" outcome so the Stage 29
    callback handler can hand off to the FSM flow
    (``handlers/custom_title.py``). The consume + grant happen at the
    end of the FSM step, mirroring legacy's
    ``register_next_step_handler`` → ``process_custom_title``
    (``bot.py:13847`` / ``bot.py:23970``)."""

    UNWARN = "unwarn"
    """Moderation side-effect: removes one active warning in the main
    chat. Mirrors legacy ``apply_unwarn`` (``bot.py:13626``), which
    calls ``remove_warning(user_id, CHAT_ID)`` against the configured
    main chat. Lives in ``moderation.db`` (a different DB than every
    other kind), so :class:`InventoryUseService` does NOT apply it —
    the callback handler performs the moderation read/write against a
    moderation session and only consumes the inventory entry if an
    active warning was actually removed (refuse-without-consume on
    no-warning, per the atomicity requirement)."""

    UNKNOWN = "unknown"
    """Catalog row whose type isn't classified yet, or a 'vip' row
    whose name doesn't match the canonical four. The service layer
    (Stage 28) refuses to apply UNKNOWN plans — the user sees a
    "this item doesn't apply automatically yet" toast and the legacy
    auto-apply path (still firing on the legacy code path) remains
    responsible for it. UNKNOWN is the safe default, not an error
    — adding a new kind in a future stage should be additive."""


@dataclass(frozen=True, slots=True)
class VipGrantSpec:
    """Carrier for a VIP_GRANT plan's parameters.

    Separate from :class:`EffectPlan` so the optional-field invariant
    on the plan stays a simple "at most one is non-None" check, and
    so a hypothetical future "grant both VIP and a privilege"
    composite item is a new field on the plan rather than reshaping
    the existing spec.

    #192: this spec deliberately carries NO ``granted_till_from``
    helper, unlike its siblings. VIP is the one effect that STACKS
    (legacy ``bot.py:13442-13444`` adds the new duration to the
    existing expiry), so "now + duration_days" is not the resulting
    expiry — it is only this item's contribution. The real answer
    depends on the stored row and is returned by
    :meth:`VipRepo.grant_global`. A convenience method here would
    have been right at exactly the moments nobody has VIP yet, and
    quietly wrong for every repeat buyer.
    """

    duration_days: int


@dataclass(frozen=True, slots=True)
class PrivilegeGrantSpec:
    """Carrier for a privilege-row grant on PrivilegesRepo.

    Covers single-use busters (``duration_seconds=None`` — the row
    exists until consumed, with a generous safety TTL applied by the
    service) and future time-bounded grants (``duration_seconds`` set).

    ``value`` is the JSON payload column on ``privileges``; ``None``
    is legitimately "no payload" — this pipeline signals a double_daily
    by the row's presence alone, where legacy carried ``{"active":
    True}`` (``bot.py:13662``, read back truthily at ``:13673``). See
    :class:`~telegram_invite_bot.db.models.economy.UserPrivilege` for
    why that divergence matters. The service is responsible for
    JSON-encoding ``value`` if it's set.
    """

    privilege_type: str
    duration_seconds: int | None
    value: str | None


@dataclass(frozen=True, slots=True)
class ColorNickGrantSpec:
    """Carrier for a COLOR_NICK plan's parameters.

    Separate from :class:`PrivilegeGrantSpec` so the planner's
    invariant on the plan stays one-spec-per-kind rather than a
    polymorphic "privilege_grant means double_daily XOR color_nick
    XOR …" overload. Stage 28's service pattern-matches on the kind
    and accesses the matching spec field; a new dataclass means a
    new field on :class:`EffectPlan` rather than tag-discriminating
    inside ``PrivilegeGrantSpec``.

    ``duration_days`` is the user-visible TTL legacy ``apply_color_nick``
    accepts (default 7); ``color`` is the JSON payload value (default
    ``"rainbow"`` per legacy). ``granted_till_from`` mirrors
    :class:`VipGrantSpec` — clock-free, takes ``now`` from the planner
    so the same instant flows into the audit trail and the privilege
    row's ``expires_at``.
    """

    duration_days: int
    color: str

    def granted_till_from(self, now: datetime) -> datetime:
        """Compute the privilege row's ``expires_at`` from ``now``.

        Legacy ``apply_color_nick`` does ``time.time() + duration * 86400``
        — an UNCONDITIONAL upsert that replaces any existing color_nick
        row regardless of the active expiry. Stage 30 keeps that
        replace-not-MAX posture (see ``PrivilegesRepo.grant_with_value``
        for the rationale). This method only answers "what expiry does
        THIS item set?"; the service writes it directly without a
        read-existing round-trip.
        """
        return now + timedelta(days=self.duration_days)


@dataclass(frozen=True, slots=True)
class MuteProtectionGrantSpec:
    """Carrier for a MUTE_PROTECTION plan's parameters.

    Separate dataclass (not reusing :class:`PrivilegeGrantSpec`) for
    the same one-spec-per-kind reason :class:`ColorNickGrantSpec` is
    separate: the service pattern-matches on the kind and accesses
    the matching spec field; a new kind means a new field on
    :class:`EffectPlan`, never a polymorphic overload of an existing
    spec.

    No ``value`` field — legacy ``apply_mute_protection``
    (``bot.py:13638``) pins ``value={}`` unconditionally and the
    read-side check (``has_mute_protection`` at ``bot.py:13647``)
    inspects nothing inside the payload. The empty object is a schema
    placeholder for the TEXT column, NOT a user-facing knob, so it
    doesn't belong on the spec — the service writes the literal
    ``"{}"`` JSON inline. If a future variant ships per-instance
    payload data (e.g. "protect from N mutes"), add the field then;
    today, omitting it keeps the spec from suggesting a knob exists.

    ``duration_hours`` (not days, not seconds) because the legacy seed
    is 24h and the read-side cache TTL is computed in hours
    (``bot.py:13643``); keeping the unit consistent with the legacy
    surface makes future debugging "why does this row expire at X?"
    answerable without unit conversion.
    """

    duration_hours: int

    def granted_till_from(self, now: datetime) -> datetime:
        """Compute the privilege row's ``expires_at`` from ``now``.

        Legacy ``apply_mute_protection`` does
        ``time.time() + duration * 3600`` — an UNCONDITIONAL upsert
        (REPLACE semantics, same as color_nick: see
        :meth:`PrivilegesRepo.grant_with_value`). This method only
        answers "what expiry does THIS item set?"; the service writes
        it directly with no read-existing round-trip, and a fresh
        24h grant on top of an active 24h one resets the window to
        24h from ``now`` — matches legacy and the user's mental model
        ("I just activated this, so this is what's active").
        """
        return now + timedelta(hours=self.duration_hours)


@dataclass(frozen=True, slots=True)
class XpBoostGrantSpec:
    """Carrier for an XP_BOOST plan's parameters.

    Separate dataclass (not reusing :class:`PrivilegeGrantSpec`) for the
    same one-spec-per-kind reason the other timed grants carry their
    own: the service pattern-matches on the kind and accesses the
    matching spec field.

    ``duration_minutes`` (not days, not seconds) because the legacy seed
    and ``apply_xp_boost`` (``bot.py:13561``) both express duration in
    minutes (``time.time() + duration * 60``); keeping the unit
    consistent with the legacy surface keeps "why does this expire at
    X?" answerable without conversion. ``multiplier`` is the integer
    factor (legacy default 2) the per-message earn path multiplies
    coins by.
    """

    duration_minutes: int
    multiplier: int

    def granted_till_from(self, now: datetime) -> datetime:
        """Compute the privilege row's ``expires_at`` from ``now``.

        Legacy ``apply_xp_boost`` does ``time.time() + duration * 60``
        — an unconditional cache write with no read-existing check. The
        new write path goes through :meth:`PrivilegesRepo.grant_with_value`
        (REPLACE semantics), so a fresh boost replaces any active one,
        matching the user's "I just activated this" mental model.
        """
        return now + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True, slots=True)
class CustomTitleSpec:
    """Carrier for a CUSTOM_TITLE plan's parameters.

    Carries no ``title`` — the title is collected from the user via FSM
    *after* this plan is produced, so the planner can't know it. The
    only planner-decidable parameter is ``duration_days`` (legacy
    ``apply_custom_title`` default 7, ``bot.py:13681``); the FSM handler
    threads it into :meth:`PrivilegesRepo.grant_with_value` once the
    user supplies the text.
    """

    duration_days: int

    def granted_till_from(self, now: datetime) -> datetime:
        """Compute the privilege row's ``expires_at`` from ``now``.

        Mirrors legacy ``apply_custom_title``'s
        ``time.time() + duration * 86400``. The FSM handler calls this
        with the instant the user's title message lands (NOT the /use
        click instant) so the 7-day window starts when the title is
        actually set — matching legacy, where the privilege is only
        written inside ``process_custom_title``.
        """
        return now + timedelta(days=self.duration_days)


# Hand-tuned payout shapes, stated over the spans they were authored
# for (legacy ``bot.py:13362-13397``). Module-level rather than rebuilt
# per call: :meth:`CoinPayoutSpec._scale_into_range` needs the table's
# own endpoints to remap a draw, and a list rebuilt inside the method
# would have to be passed back out anyway.
_BIG_GIFT_BANDS: list[tuple[int, int, int]] = [
    (100, 150, 22),  # 100–150 — often
    (151, 200, 38),  # 151–200 — most frequent
    (201, 250, 22),  # 201–250 — still often
    (251, 300, 10),  # 251–300 — less frequent
    (301, 350, 4),  # 301–350
    (351, 400, 2),  # 351–400
    (401, 500, 2),  # 401–500 — rare
]
_SECRET_GIFT_BANDS: list[tuple[int, int, int]] = [
    (10, 25, 22),  # 10–25 — often
    (26, 40, 35),  # 25–40 — very often
    (41, 55, 28),  # 40–55 — peak
    (56, 70, 10),  # 55–70 — less frequent
    (71, 85, 3),  # 70–85
    (86, 100, 2),  # 85–100 — rare
]


@dataclass(frozen=True, slots=True)
class CoinPayoutSpec:
    """Carrier for a LUCK_COIN_PAYOUT plan's parameters.

    Unlike grants, luck items provide immediate coin payouts via RNG.
    Supports both simple uniform distribution (min_coins to max_coins)
    and canonical weighted distributions for legacy-compatible items.

    ``random_int_inclusive`` is injected for deterministic testing —
    the service will bind ``random.randint`` in production but tests
    can substitute a fixed-seed generator or mock. The callback
    signature matches ``random.randint(min, max)`` inclusive bounds.

    ``is_big_gift`` and ``is_secret_gift`` trigger weighted distributions
    that match legacy ``_random_big_gift_prize()`` and
    ``_random_secret_gift_prize()`` exactly. When both are False,
    falls back to uniform ``random_int_inclusive(min_coins, max_coins)``.
    """

    min_coins: int
    max_coins: int
    random_int_inclusive: Callable[[int, int], int]
    is_big_gift: bool = False
    is_secret_gift: bool = False

    def generate_payout(self) -> int:
        """Generate the RNG payout amount for this spec.

        Returns the exact number of coins to grant to the user.
        Behavior varies by gift type:
        - is_big_gift=True: weighted shape peaking near the low third
        - is_secret_gift=True: weighted shape peaking near the middle
        - Neither: uniform distribution min_coins to max_coins

        #192: the two weighted shapes are stated over the LEGACY spans
        (100–500 and 10–100) because that is where the hand-tuned bands
        came from. Production's catalog was rescaled by 10× — prices and
        the ``data.min``/``data.max`` blobs moved together, but these
        hardcoded bands could not follow. So the shape is remapped onto
        whatever ``[min_coins, max_coins]`` the row actually carries.

        Why not simply draw uniformly from ``data.min``..``data.max``:
        for "🎁 Большой подарок" that is a mean of 3000 against a price
        of 2000 — the ecosystem's single largest coin sink turned into a
        150% faucet. And why not keep the legacy bands as-is: that pays
        a mean of 201 for the same 2000-coin item, i.e. the buyer gets
        back a tenth of the price, and it contradicts the description
        the user reads ("от 1000 до 5000 монет"). Remapping preserves
        the designed edge (the ratios the bands had at their original
        prices) AND pays inside the advertised range.

        #1139 — the edge is a function of PRICE, so keep the prices in
        view when touching this. At the legacy 2000 the big gift pays a
        mean of ~2011, i.e. +0.4% TO THE BUYER: a net faucet. Economy 0017
        prices it at 990 over 350..2000 (a ~23% sink); the secret gift is
        190 over 30..400 (~-21%). Both keep roughly the edge of 2500/500.
        Nothing in the schema enforces this — there is no CHECK
        constraint and no validation on ``shop_items.price`` — so a hand
        edit of the catalog back to 2000 silently flips the ecosystem's
        largest sink into a source.
        """
        if self.is_big_gift:
            return self._scale_into_range(self._weighted_choice(_BIG_GIFT_BANDS), _BIG_GIFT_BANDS)
        if self.is_secret_gift:
            return self._scale_into_range(
                self._weighted_choice(_SECRET_GIFT_BANDS), _SECRET_GIFT_BANDS
            )
        return self.random_int_inclusive(self.min_coins, self.max_coins)

    def _scale_into_range(self, value: int, bands: list[tuple[int, int, int]]) -> int:
        """Map ``value`` from the band table's own span onto this spec's.

        Linear, endpoint-preserving, and a no-op when the spec's span
        already equals the table's — so a catalog still at legacy scale
        keeps producing byte-identical draws. The result is clamped
        because integer rounding at the extremes can land one coin
        outside the advertised range, and a payout above ``max_coins``
        would make the catalog description a lie in the other direction.
        """
        table_min, table_max = bands[0][0], bands[-1][1]
        if (self.min_coins, self.max_coins) == (table_min, table_max):
            return value
        table_span = table_max - table_min
        if table_span <= 0:
            return self.min_coins
        scaled = round(self._map_into_range(value, table_min, table_span))
        return max(self.min_coins, min(self.max_coins, scaled))

    def _map_into_range(self, value: float, table_min: int, table_span: int) -> float:
        """The linear half of the remap, shared with :meth:`expected_payout`.

        Split out so a draw and its own mean cannot drift apart: any
        change to the mapping has to move both or neither. Takes a float
        because the mean of the bands is not an integer.
        """
        span = self.max_coins - self.min_coins
        return self.min_coins + (value - table_min) * span / table_span

    def expected_payout(self) -> float:
        """Mean coins one draw pays, over the same span the draw uses.

        Computed rather than sampled: the shapes are finite weighted
        bands and the third branch is a closed interval, so the mean is
        arithmetic. #1790 needs it to answer "does this row pay more
        than it costs?" *before* the item is consumed, and a Monte-Carlo
        estimate would make the classification of a catalog row depend
        on a seed.

        Off by at most a coin from the true mean of the *integer* draw:
        :meth:`_scale_into_range` rounds and clamps every sample while
        this maps the band mean once. The guard compares against a price
        it misses by hundreds on the live catalog, so a coin either way
        cannot turn a sink into a faucet.
        """
        if self.is_big_gift:
            return self._expected_from_bands(_BIG_GIFT_BANDS)
        if self.is_secret_gift:
            return self._expected_from_bands(_SECRET_GIFT_BANDS)
        return (self.min_coins + self.max_coins) / 2

    def _expected_from_bands(self, bands: list[tuple[int, int, int]]) -> float:
        """Weighted mean of ``bands``, carried onto this spec's span.

        Mirrors :meth:`_weighted_choice` followed by
        :meth:`_scale_into_range`: a band is picked with probability
        proportional to its weight, then drawn uniformly inside it, so
        the mean is the weight-weighted mean of the band midpoints.
        """
        total_weight = sum(weight for _, _, weight in bands)
        if total_weight <= 0:
            return float(self.min_coins)
        mean = sum(weight * (low + high) / 2 for low, high, weight in bands) / total_weight
        table_min, table_max = bands[0][0], bands[-1][1]
        if (self.min_coins, self.max_coins) == (table_min, table_max):
            return mean
        table_span = table_max - table_min
        if table_span <= 0:
            return float(self.min_coins)
        mapped = self._map_into_range(mean, table_min, table_span)
        return max(float(self.min_coins), min(float(self.max_coins), mapped))

    def _weighted_choice(self, ranges: list[tuple[int, int, int]]) -> int:
        """Pick a weighted range then uniform RNG within that range."""
        total_weight = sum(weight for _, _, weight in ranges)
        pick = self.random_int_inclusive(1, total_weight)

        cumulative = 0
        for min_val, max_val, weight in ranges:
            cumulative += weight
            if pick <= cumulative:
                return self.random_int_inclusive(min_val, max_val)

        # Fallback (should never reach here with correct weights)
        min_val, max_val, _ = ranges[-1]
        return self.random_int_inclusive(min_val, max_val)


@dataclass(frozen=True, slots=True)
class EffectPlan:
    """What :func:`plan_effect_application` decided to do, if anything.

    Exactly zero or one of ``vip_grant`` / ``privilege_grant`` /
    ``color_nick_grant`` / ``mute_protection_grant`` / ``coin_payout`` is set:
        * ``kind == UNKNOWN`` → all ``None``.
        * ``kind == VIP_GRANT`` → ``vip_grant`` set, others None.
        * ``kind == DOUBLE_DAILY_BUSTER`` → ``privilege_grant`` set,
          others None.
        * ``kind == COLOR_NICK`` → ``color_nick_grant`` set, others
          None.
        * ``kind == MUTE_PROTECTION`` → ``mute_protection_grant`` set,
          others None.
        * ``kind == LUCK_COIN_PAYOUT`` → ``coin_payout`` set, others
          None.

    The invariant is enforced by the planner (single construction
    site) and re-checked in :meth:`__post_init__` so a hand-built plan
    in tests can't slip past the contract — Stage 28's service will
    pattern-match on ``kind`` and trust the corresponding spec field
    is populated.
    """

    kind: InventoryEffectKind
    vip_grant: VipGrantSpec | None = None
    privilege_grant: PrivilegeGrantSpec | None = None
    color_nick_grant: ColorNickGrantSpec | None = None
    mute_protection_grant: MuteProtectionGrantSpec | None = None
    coin_payout: CoinPayoutSpec | None = None
    xp_boost_grant: XpBoostGrantSpec | None = None
    custom_title: CustomTitleSpec | None = None

    def __post_init__(self) -> None:
        populated = sum(
            spec is not None
            for spec in (
                self.vip_grant,
                self.privilege_grant,
                self.color_nick_grant,
                self.mute_protection_grant,
                self.coin_payout,
                self.xp_boost_grant,
                self.custom_title,
            )
        )
        if populated > 1:
            msg = (
                "EffectPlan invariant: at most one of vip_grant/"
                "privilege_grant/color_nick_grant/mute_protection_grant/"
                "coin_payout/xp_boost_grant/custom_title may be set; got more."
            )
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.VIP_GRANT and self.vip_grant is None:
            msg = "EffectPlan(kind=VIP_GRANT) requires vip_grant to be set."
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.DOUBLE_DAILY_BUSTER and self.privilege_grant is None:
            msg = "EffectPlan(kind=DOUBLE_DAILY_BUSTER) requires privilege_grant to be set."
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.COLOR_NICK and self.color_nick_grant is None:
            msg = "EffectPlan(kind=COLOR_NICK) requires color_nick_grant to be set."
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.MUTE_PROTECTION and self.mute_protection_grant is None:
            msg = "EffectPlan(kind=MUTE_PROTECTION) requires mute_protection_grant to be set."
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.LUCK_COIN_PAYOUT and self.coin_payout is None:
            msg = "EffectPlan(kind=LUCK_COIN_PAYOUT) requires coin_payout to be set."
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.XP_BOOST and self.xp_boost_grant is None:
            msg = "EffectPlan(kind=XP_BOOST) requires xp_boost_grant to be set."
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.CUSTOM_TITLE and self.custom_title is None:
            msg = "EffectPlan(kind=CUSTOM_TITLE) requires custom_title to be set."
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.UNWARN and populated > 0:
            msg = "EffectPlan(kind=UNWARN) must carry no spec (moderation side-effect)."
            raise ValueError(msg)
        if self.kind is InventoryEffectKind.UNKNOWN and populated > 0:
            msg = "EffectPlan(kind=UNKNOWN) must carry no spec."
            raise ValueError(msg)


def _declared_positive_int(raw: object) -> int | None:
    """Coerce one ``shop_items.data`` value to a positive int.

    Every parameter legacy reads out of the JSON blob is a positive
    count — days, hours, minutes, a multiplier — so "declared" and
    "usable" are the same question, and one coercion answers it for all
    of them. Returns ``None`` for absent, non-numeric, zero or negative
    values so callers can fall through to their next source rather than
    granting a zero-length effect.

    ``bool`` is an ``int`` subclass and ``True`` would read as 1, which
    is why it is rejected explicitly. Strings are accepted because the
    blob is operator-edited JSON and ``{"duration": "30"}`` is a typo
    the buyer should not pay for.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str):
        try:
            parsed = int(raw)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


# ----------------------------------------------------------------------
# VIP name → duration lookup
# ----------------------------------------------------------------------
# Sourced from ``bot.py:12330-12361``. These four names are the only
# canonical VIP rows in the legacy ``init_default_items`` seed, and
# every one of them already carries ``data["duration"]``, so
# :func:`declared_vip_duration_days` answers from the row itself and
# never reaches these keys for the canonical catalog — they are a
# middle step that only a hand-crafted ``type='vip'`` row without that
# field can land on. Nor does an unrecognised name classify as UNKNOWN
# any more: #192 made every resolver here fall back to legacy's own
# default instead (see :func:`resolve_vip_duration_days` and the
# ``_resolve_*`` helpers below). What is load-bearing about this table
# is therefore its VALUES, not its keys — ``handlers/vip.py`` builds
# ``_CURATED_TERMS`` out of them, and the i18n key-family tests derive
# their expectations from the same four numbers, so renaming a row is
# free while renumbering one is not. Keys are matched byte-exactly
# against ``item.name``; emoji + spaces included on purpose since
# that's what the catalog stores.
#
# PUBLIC on purpose (RR-2 #21): ``handlers/vip.py`` renders each plan
# card off the SAME table, so the duration the shop advertises is by
# construction the duration :func:`plan_effect` will grant. A private
# copy in the handler would be free to drift, and "VIP (3 месяца)" that
# grants 30 days is the kind of bug users notice a month later.
VIP_NAME_DURATIONS: dict[str, int] = {
    "👑 VIP (1 месяц)": 30,
    "👑 VIP (3 месяца)": 90,
    "👑 VIP (6 месяцев)": 180,
    "👑 VIP (1 год)": 365,
}

# Legacy default when a vip row carries no ``duration`` at all —
# ``bot.py:13155``: ``duration = data.get("duration", 30)``.
VIP_DEFAULT_DURATION_DAYS: int = 30


def declared_vip_duration_days(item: ShopItemEntity) -> int | None:
    """The term this row actually DECLARES, or ``None`` if it declares none.

    Two questions hide behind "how long is this plan?". What we must
    GRANT has to have an answer for every row, because the buyer paid —
    :func:`resolve_vip_duration_days` supplies legacy's 30-day default
    there. What we may TELL the buyer does not: a row that names no term
    anywhere is one whose term we are only guessing, and a card must not
    price, compare or describe a plan off a guess. So ``/vip_shop``
    reads this function and falls back to the operator's own copy.

    Order matches legacy: ``data["duration"]`` first
    (``bot.py:13155``), then the curated name table.
    """
    declared = _declared_positive_int(item.data.get("duration"))
    if declared is not None:
        return declared
    return VIP_NAME_DURATIONS.get(item.name)


def resolve_vip_duration_days(item: ShopItemEntity) -> int:
    """How many days consuming ``item`` should grant.

    #192: the planner used to read this from :data:`VIP_NAME_DURATIONS`
    ALONE and return UNKNOWN for anything absent from it. None of those
    four names exists in the production catalog — the live row is named
    ``👑 VIP статус`` — so every VIP purchase classified as UNKNOWN and
    the buyer got nothing for 5000 COM. Legacy never looked at the name:
    it read ``data["duration"]`` and fell back to 30 days. We restore
    that order and keep the name table only as a middle step, because
    ``handlers/vip.py`` renders curated plan copy off it.

    Never returns ``None``: an operator row that declares nothing still
    took the buyer's coins, and legacy paid it 30 days. Use
    :func:`declared_vip_duration_days` where a guess is worse than
    silence.
    """
    declared = declared_vip_duration_days(item)
    if declared is not None:
        return declared
    return VIP_DEFAULT_DURATION_DAYS


# ----------------------------------------------------------------------
# color_nick name → (duration, color) lookup
# ----------------------------------------------------------------------
# color_nick is not part of ``init_default_items`` — operators seed it
# via /admin_shop. The /admin_shop help block at ``bot.py:34095`` ships
# the canonical example name with a 7-day duration:
#     "🌈 Цветной ник | Цветной ник на 7 дней | 500 | -1 | color_nick |
#      {\"duration\": 7}"
# That row is the middle step: #192 made ``item.data`` the first source
# and legacy's own defaults the last, so a non-canonical operator name
# no longer decides anything.
# The defaults match legacy exactly — ``apply_color_nick``'s signature
# (``bot.py:13406``: ``color: str = "rainbow", duration: int = 7``) and
# the auto-apply path's reads at ``bot.py:13149-13150``
# (``data.get("duration", 7)`` / ``data.get("color", "rainbow")``).
_COLOR_NICK_NAME_SPECS: dict[str, tuple[int, str]] = {
    "🌈 Цветной ник": (7, "rainbow"),
}
_COLOR_NICK_DEFAULT_DURATION_DAYS: int = 7
_COLOR_NICK_DEFAULT_COLOR: str = "rainbow"


def _resolve_color_nick_spec(item: ShopItemEntity) -> tuple[int, str]:
    """Days and colour to grant for a ``color_nick`` row.

    Legacy's order, restored by #192: the operator's own ``data``, then
    the curated name, then legacy's defaults. The two fields fall
    through independently because ``{"color": "gold"}`` with no
    ``duration`` is a perfectly ordinary operator row.
    """
    named_days, named_color = _COLOR_NICK_NAME_SPECS.get(
        item.name, (_COLOR_NICK_DEFAULT_DURATION_DAYS, _COLOR_NICK_DEFAULT_COLOR)
    )
    days = _declared_positive_int(item.data.get("duration")) or named_days
    raw_color = item.data.get("color")
    color = raw_color.strip() if isinstance(raw_color, str) and raw_color.strip() else named_color
    return days, color


# ----------------------------------------------------------------------
# mute_protection name → duration_hours lookup
# ----------------------------------------------------------------------
# Sourced from ``init_default_items`` at ``bot.py:12386-12393``:
#     {"name": "🔇 Защита от мута", "type": "mute_protection",
#      "data": {"duration": 24}, ...}
# Single canonical row, single canonical duration — and since #192 only
# the middle step: the operator's ``data["duration"]`` outranks it and
# legacy's own default backs it. The legacy auto-apply path at
# ``bot.py:13180`` is ``data.get("duration", 24)``, in hours.
_MUTE_PROTECTION_NAME_DURATIONS: dict[str, int] = {
    "🔇 Защита от мута": 24,
}
_MUTE_PROTECTION_DEFAULT_DURATION_HOURS: int = 24


def _resolve_mute_protection_hours(item: ShopItemEntity) -> int:
    """Hours of mute protection to grant for a ``mute_protection`` row."""
    named = _MUTE_PROTECTION_NAME_DURATIONS.get(item.name, _MUTE_PROTECTION_DEFAULT_DURATION_HOURS)
    return _declared_positive_int(item.data.get("duration")) or named


# ----------------------------------------------------------------------
# xp_boost name → (duration_minutes, multiplier) lookup
# ----------------------------------------------------------------------
# Sourced from ``init_default_items`` at ``bot.py:12402-12409``:
#     {"name": "⚡ Ускорение", "type": "xp_boost",
#      "data": {"duration": 60, "multiplier": 2}, ...}
# and the legacy defaults of ``apply_xp_boost`` (``bot.py:13561``:
# ``duration=60`` minutes, ``multiplier=2``). Single canonical row, and
# since #192 only the middle step: the legacy auto-apply path reads
# ``data.get("duration", 60)`` / ``data.get("multiplier", 2)``
# (``bot.py:13185``), so the operator's own blob wins and those two
# defaults back it. Same order VIP / color_nick / mute_protection take.
_XP_BOOST_NAME_SPECS: dict[str, tuple[int, int]] = {
    "⚡ Ускорение": (60, 2),
}
_XP_BOOST_DEFAULT_DURATION_MINUTES: int = 60
_XP_BOOST_DEFAULT_MULTIPLIER: int = 2


def _resolve_xp_boost_spec(item: ShopItemEntity) -> tuple[int, int]:
    """Duration (minutes) and multiplier to grant for an ``xp_boost`` row.

    No upper bound here on purpose: the read side already clamps at
    ``xp_boost._MAX_MULTIPLIER``, which is the only place a
    fat-fingered operator row can actually mint coins.
    """
    named_minutes, named_multiplier = _XP_BOOST_NAME_SPECS.get(
        item.name, (_XP_BOOST_DEFAULT_DURATION_MINUTES, _XP_BOOST_DEFAULT_MULTIPLIER)
    )
    minutes = _declared_positive_int(item.data.get("duration")) or named_minutes
    multiplier = _declared_positive_int(item.data.get("multiplier")) or named_multiplier
    return minutes, multiplier


# ----------------------------------------------------------------------
# custom_title duration (days)
# ----------------------------------------------------------------------
# custom_title is NOT in ``init_default_items`` (a deprecated legacy
# type per ``bot.py:12749``); operators seed it via /admin_shop. The
# only planner-decidable knob is the grant duration, which legacy
# ``apply_custom_title`` (``bot.py:13681``) and ``process_custom_title``
# (``bot.py:23984``) both pin at 7 days. Unlike the name-keyed kinds,
# we classify ANY ``type='custom_title'`` row (no name table) because
# the duration is fixed in legacy regardless of the catalog name — the
# variable part (the title text) is collected from the user via FSM,
# not from the catalog row.
_CUSTOM_TITLE_DURATION_DAYS: int = 7


# ----------------------------------------------------------------------
# luck name → (min, max, gift_type) lookup
# ----------------------------------------------------------------------
# Sourced from the legacy luck dispatch (bot.py:13159-13173), which
# calls apply_luck_gift (bot.py:13582).
# Legacy identifies gift types by SUBSTRING on the item name
# (``bot.py:13161-13164``: ``if "Большой подарок" in item_name``), then
# falls back to ``data.min``/``data.max`` with uniform RNG for anything
# else (``bot.py:13165-13170``).
#
# #192: this table used to be keyed on two FULL names that were
# transcribed from the seed's ``description`` column ("🎲 Случайный приз
# …") rather than its ``name`` column ("🎁 Большой подарок"). The lookup
# was an exact ``dict.get``, so the emoji alone made every production
# luck row fall through to UNKNOWN — paid for, never paid out. Matching
# on the substring, exactly as legacy does, makes the emoji prefix stop
# being load-bearing.
#
# The bounds here are the LEGACY ones and are only a fallback: when the
# row carries ``data.min``/``data.max`` those win, because that is what
# the operator set and what the catalog description promises the user.
# See :meth:`CoinPayoutSpec.generate_payout` for how the weighted shape
# is carried over to a different span.
_LUCK_NAME_SHAPES: tuple[tuple[str, int, int, str], ...] = (
    ("Большой подарок", 100, 500, "big_gift"),
    ("Секретный подарок", 10, 100, "secret_gift"),
)


def _luck_shape_for(name: str) -> tuple[int, int, str] | None:
    """Match a catalog name to a payout shape, legacy-style.

    Legacy tested ``if "Большой подарок" in item_name`` — a substring,
    so the emoji prefix and any operator suffix were irrelevant. The
    port turned that into an exact ``dict.get()`` keyed on strings
    transcribed from the seed's DESCRIPTION rather than its NAME, which
    is why nothing ever matched. Substring is the behaviour to keep.
    """
    for needle, min_coins, max_coins, gift_type in _LUCK_NAME_SHAPES:
        if needle in name:
            return min_coins, max_coins, gift_type
    return None


def _luck_bounds_from_data(item: ShopItemEntity) -> tuple[int, int] | None:
    """Read the payout span the operator actually configured.

    ``bot.py:13165-13170`` draws ``randint(data["min"], data["max"])``.
    Those two numbers are what the catalog description promises the
    buyer, so they outrank the hard-coded legacy seed bounds whenever
    they are present and sane. Returns ``None`` for a missing, non-
    numeric, negative or inverted pair rather than guessing.
    """
    raw_min = item.data.get("min")
    raw_max = item.data.get("max")
    values: list[int] = []
    for raw in (raw_min, raw_max):
        if isinstance(raw, bool) or not isinstance(raw, int | str):
            return None
        try:
            values.append(int(raw))
        except ValueError:
            return None
    low, high = values
    if low < 0 or high < low:
        return None
    return low, high


# Legacy double_daily TTL: 3 days. ``apply_double_daily`` at
# ``bot.py:13658`` sets ``expires = time.time() + 86400 * 3`` — a
# generous safety margin so the buster doesn't silently expire if the
# user buys it Friday and claims /daily Sunday across a tz boundary.
# The PrivilegesRepo row is removed on consumption regardless of TTL.
_DOUBLE_DAILY_TTL_SECONDS: int = 86400 * 3


def plan_effect_application(
    item: ShopItemEntity,
    *,
    now: datetime,
    random_int_inclusive: Callable[[int, int], int] | None = None,
    group_rebate_percent: int = 0,
) -> EffectPlan:
    """Decide what (if anything) consuming ``item`` should do.

    Pure: no I/O, no clock reads, no module-level state mutation.
    ``now`` is injected so the planner's output is reproducible for
    a given (item, now) pair — required for the deterministic tests,
    and for Stage 28 to use the same instant for the plan and the
    repo write (no skew between "what expiry did we promise the
    user?" and "what expiry did we actually store?").

    ``random_int_inclusive`` is injected for LUCK_COIN_PAYOUT plans
    to enable deterministic testing. Defaults to ``money_rng.randint``
    (utils/rng.py) if not provided — a luck item pays out coins, so the
    roll must not come from a stream anyone can replay. Signature
    matches ``random.randint(min, max)`` with inclusive bounds.

    ``group_rebate_percent`` is what a group-scoped purchase pays back
    to the chosen group out of the same price (#1930 — see the ``luck``
    branch). ``0`` is the honest default for every caller that only
    classifies an item; the one caller whose answer decides real coins,
    :class:`~telegram_invite_bot.services.inventory_use_service.InventoryUseService`,
    passes the configured percent.

    Returns :class:`EffectPlan` with ``kind=UNKNOWN`` for a ``type``
    this planner does not handle, for a ``luck`` row that declares no
    usable payout span, and (#1790) for a ``luck`` row whose expected
    payout exceeds its own price net of that rebate. UNKNOWN is *not* an error — the planner's job
    is to classify, and "I don't know yet" is a valid classification the
    service handles gracefully. It is, however, terminal for the buyer:
    the service refuses BEFORE consuming, so an UNKNOWN row the user
    already paid for can never be used. That is why #1204 stopped
    parameterised kinds from reaching it on an unrecognised NAME.
    """
    if random_int_inclusive is None:
        random_int_inclusive = money_rng.randint

    item_type = item.type

    if item_type == "double_daily":
        return EffectPlan(
            kind=InventoryEffectKind.DOUBLE_DAILY_BUSTER,
            privilege_grant=PrivilegeGrantSpec(
                privilege_type="double_daily",
                duration_seconds=_DOUBLE_DAILY_TTL_SECONDS,
                value=None,
            ),
        )

    if item_type == "vip":
        duration_days = resolve_vip_duration_days(item)
        return EffectPlan(
            kind=InventoryEffectKind.VIP_GRANT,
            vip_grant=VipGrantSpec(duration_days=duration_days),
        )

    if item_type == "color_nick":
        # #192/#1204: the operator's own ``data`` first, then the name
        # table, then legacy's defaults. Returning UNKNOWN on an
        # unrecognised name used to brick the item permanently — the
        # service refuses BEFORE the consume, so the row survived, the
        # coins did not, and there is no refund path.
        duration_days, color = _resolve_color_nick_spec(item)
        return EffectPlan(
            kind=InventoryEffectKind.COLOR_NICK,
            color_nick_grant=ColorNickGrantSpec(duration_days=duration_days, color=color),
        )

    if item_type == "mute_protection":
        # #192/#1204: ``data["duration"]`` (hours), then the name table,
        # then legacy's 24 h. See the color_nick branch for why an
        # unrecognised name must not classify as UNKNOWN.
        duration_hours = _resolve_mute_protection_hours(item)
        return EffectPlan(
            kind=InventoryEffectKind.MUTE_PROTECTION,
            mute_protection_grant=MuteProtectionGrantSpec(duration_hours=duration_hours),
        )

    if item_type == "luck":
        bounds = _luck_bounds_from_data(item)
        shape = _luck_shape_for(item.name)
        payout: CoinPayoutSpec | None = None
        if shape is not None:
            legacy_min, legacy_max, gift_type = shape
            min_coins, max_coins = bounds if bounds is not None else (legacy_min, legacy_max)
            payout = CoinPayoutSpec(
                min_coins=min_coins,
                max_coins=max_coins,
                random_int_inclusive=random_int_inclusive,
                is_big_gift=gift_type == "big_gift",
                is_secret_gift=gift_type == "secret_gift",
            )
        elif bounds is not None:
            # Operator-seeded luck row with a name we have no shape for
            # but explicit bounds. Legacy draws uniformly from them
            # (``bot.py:13165-13170``); so do we.
            min_coins, max_coins = bounds
            payout = CoinPayoutSpec(
                min_coins=min_coins,
                max_coins=max_coins,
                random_int_inclusive=random_int_inclusive,
            )
        # #1930: the price is not what the buyer ends up out of pocket.
        # ``handlers/shop`` runs ONE confirm that first applies the item
        # and then routes ``purchase_donation_to_group_percent`` of the
        # same price to the chosen group — minted, not moved
        # (``GroupDonationService._credit`` records the row with
        # ``from_id=None``) — and the recipient is the group's CREATOR,
        # who can be the buyer himself: create a group, add the bot,
        # pick it on the shop card. So a group-scoped tap costs the
        # price and hands ~14.25% of it back (15% less the 5%
        # developer cut), and a row sitting at exactly break-even under
        # the old test — legal, because a break-even row mints nothing
        # ON ITS OWN — was a repeatable +14.25% loop with no error and
        # no log. The threshold is therefore the price NET of the
        # rebate: the full percent, not the post-fee remainder, because
        # the fee is the developer's and may be zero.
        #
        # With ``group_rebate_percent=0`` the arithmetic is exactly the
        # old comparison, which is what every classify-only caller
        # (the inspect card's Use button, /vip, /title) still gets. Such
        # a caller can therefore show a Use button for a row inside the
        # band the service now refuses — the same loud cost #1790
        # already accepts, and the service stays the authority: it
        # refuses BEFORE consuming, so nothing is spent.
        sink_floor = item.price * (100 - max(0, min(100, group_rebate_percent))) / 100
        if payout is not None and payout.expected_payout() > sink_floor:
            # #1790: a luck SKU is the ecosystem's largest coin SINK,
            # and nothing outside this line checks that it still is one.
            # ``shop_items`` carries no CHECK constraint and no
            # validation of ``name``/``price``/``data``, so the row's
            # economics are whatever three independently editable
            # columns happen to say, and either of two ordinary catalog
            # edits flips it into a faucet with no error and no log:
            #
            #  * touch the NAME — pluralise it, translate it, slip a
            #    word between the emoji and the noun — and
            #    ``_luck_shape_for`` stops matching, dropping the live
            #    "Большой подарок" into a uniform 350-2000 draw: a mean
            #    of 1175 against a price of 990, +19% to the buyer,
            #    repeatable for as long as the operator restocks;
            #  * or leave the name alone and put the PRICE back to the
            #    legacy 2000, which the remapped bands already beat by
            #    0.4% (see :meth:`CoinPayoutSpec.generate_payout`).
            #
            # So the invariant is verified here instead of assumed. The
            # cost of refusing is real — the buyer paid for an item he
            # now cannot redeem — but it is a LOUD cost: he complains,
            # and ``InventoryUseService`` logs the offending row. A
            # faucet drains the owner quietly and only shows up in the
            # aggregate. Nothing is consumed either, so restoring a sane
            # price makes every outstanding entry work again.
            #
            # Strictly greater: a row that breaks even against
            # ``sink_floor`` mints nothing on average and is a
            # legitimate design, so only a genuinely net-positive
            # expectation is refused.
            payout = None
        if payout is not None:
            return EffectPlan(kind=InventoryEffectKind.LUCK_COIN_PAYOUT, coin_payout=payout)
        # Neither a known shape nor usable bounds (or, since #1790, a
        # span whose mean beats the price) — there is no number we could
        # pay that wouldn't be either invented or a gift from the
        # owner's wallet, so plan UNKNOWN and leave the entry unspent.
        #
        # #1281: this is a DELIBERATE DIVERGENCE, not parity. Legacy
        # does NOT pay 0 here — ``bot.py:13171-13174`` falls through to
        # ``apply_luck_gift(user_id, 100, 1000)``, a uniform 100-1000
        # draw, and then marks the entry used. We refuse instead
        # because that fallback pays a number nothing in the catalog
        # row asked for: an operator who typed ``{}`` into
        # /admin_shop's ``… | тип | данные`` template (bot.py:34095)
        # would be selling a 100-1000 lottery he never priced. The
        # same posture as ``InventoryRepo.delete_expired`` (:250-266).
        #
        # The cost of refusing is that such a row is UNSELLABLE: the
        # buyer pays, the service returns UNKNOWN_EFFECT without
        # consuming, and the entry sits in the inventory forever. Prod
        # has no such row today (both ``luck`` items carry a matching
        # name AND numeric bounds), but nothing stops one being added,
        # so catalog-side validation is the real fix and is an owner
        # decision, tracked separately.
        return EffectPlan(kind=InventoryEffectKind.UNKNOWN)

    if item_type == "xp_boost":
        # #192/#1204: ``data["duration"]`` (minutes) and
        # ``data["multiplier"]``, then the name table, then legacy's
        # 60 min / x2. See the color_nick branch for why an
        # unrecognised name must not classify as UNKNOWN.
        duration_minutes, multiplier = _resolve_xp_boost_spec(item)
        return EffectPlan(
            kind=InventoryEffectKind.XP_BOOST,
            xp_boost_grant=XpBoostGrantSpec(
                duration_minutes=duration_minutes,
                multiplier=multiplier,
            ),
        )

    if item_type == "custom_title":
        # No name table — legacy pins the 7-day duration regardless of
        # the catalog name (the variable part is the title text, which
        # the FSM collects from the user, not from the row). The service
        # returns a "needs title input" outcome WITHOUT consuming; the
        # FSM handler does the consume + grant once the title lands.
        return EffectPlan(
            kind=InventoryEffectKind.CUSTOM_TITLE,
            custom_title=CustomTitleSpec(duration_days=_CUSTOM_TITLE_DURATION_DAYS),
        )

    if item_type == "unwarn":
        # Moderation side-effect (removes one warning in the main chat).
        # Lives in ``moderation.db``, not economy.db, so the service
        # can't apply it — it returns a NEEDS_MODERATION outcome and the
        # callback handler does the moderation read/write + the consume.
        # No spec: the only parameter (the main chat id) is a deployment
        # constant the handler reads from settings, not a per-item knob.
        return EffectPlan(kind=InventoryEffectKind.UNWARN)

    # ``now`` is part of the signature so future kinds can compute
    # expiries without an API change. Currently unused on the UNKNOWN
    # branch — that's fine; an unused parameter on a pure classifier is
    # much cheaper than re-threading it through call sites later.
    _ = now
    return EffectPlan(kind=InventoryEffectKind.UNKNOWN)
