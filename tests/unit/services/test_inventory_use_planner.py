"""Unit coverage for :func:`plan_effect_application` (Stage 27).

The planner is pure logic — no fixtures, no async, no I/O. Tests
exercise the classification table, the UNKNOWN safe-default, and the
:class:`EffectPlan` invariant guards.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from telegram_invite_bot.core.entities.shop import ShopItemEntity
from telegram_invite_bot.services.inventory_use_planner import (
    CoinPayoutSpec,
    ColorNickGrantSpec,
    EffectPlan,
    InventoryEffectKind,
    MuteProtectionGrantSpec,
    PrivilegeGrantSpec,
    VipGrantSpec,
    plan_effect_application,
)

# A fixed instant the tests use as ``now``. Mid-month so day arithmetic
# never bumps the year, and after the bot's launch year so any future
# "must be recent" assertion stays trivially true.
_NOW = datetime(2026, 5, 15, 12, 0, 0)


def _item(
    *,
    name: str = "X",
    type_: str = "unknown",
    item_id: int = 1,
    price: int = 100,
    stock: int = -1,
    description: str = "",
    data: dict[str, object] | None = None,
) -> ShopItemEntity:
    """Builder so each test states only the field that matters."""
    return ShopItemEntity(
        id=item_id,
        name=name,
        description=description,
        price=price,
        type=type_,
        stock=stock,
        data=data or {},
    )


# ----------------------------------------------------------------------
# DOUBLE_DAILY_BUSTER
# ----------------------------------------------------------------------


def test_double_daily_item_classifies_as_buster() -> None:
    plan = plan_effect_application(_item(type_="double_daily"), now=_NOW)

    assert plan.kind is InventoryEffectKind.DOUBLE_DAILY_BUSTER
    assert plan.vip_grant is None
    assert plan.privilege_grant is not None
    assert plan.privilege_grant.privilege_type == "double_daily"
    # 3 days in seconds — matches legacy apply_double_daily TTL.
    assert plan.privilege_grant.duration_seconds == 86400 * 3
    assert plan.privilege_grant.value is None


def test_double_daily_buster_has_non_none_duration() -> None:
    # The spec distinguishes time-bounded grants (duration set) from
    # consumed-on-use busters by ``duration_seconds is None``. The
    # double_daily buster is the *one* exception — it's consumed on
    # use but ALSO carries a safety TTL so a forgotten row doesn't
    # outlive its purpose. Pin that quirk explicitly.
    plan = plan_effect_application(_item(type_="double_daily"), now=_NOW)
    assert plan.privilege_grant is not None
    assert plan.privilege_grant.duration_seconds is not None


# ----------------------------------------------------------------------
# VIP_GRANT
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_days"),
    [
        ("👑 VIP (1 месяц)", 30),
        ("👑 VIP (3 месяца)", 90),
        ("👑 VIP (6 месяцев)", 180),
        ("👑 VIP (1 год)", 365),
    ],
)
def test_canonical_vip_rows_classify_with_correct_duration(name: str, expected_days: int) -> None:
    plan = plan_effect_application(_item(type_="vip", name=name), now=_NOW)

    assert plan.kind is InventoryEffectKind.VIP_GRANT
    assert plan.privilege_grant is None
    assert plan.vip_grant is not None
    assert plan.vip_grant.duration_days == expected_days


def test_vip_duration_comes_from_data_not_from_the_name() -> None:
    """#192: production's only VIP row is ``👑 VIP статус``.

    It is in no name table and never was — legacy read
    ``data["duration"]`` (``bot.py:13155``). Keying off the name made
    every one of those purchases classify as UNKNOWN, so the buyer paid
    5000 COM and got nothing. ``data`` wins.
    """
    plan = plan_effect_application(
        _item(type_="vip", name="👑 VIP статус", data={"duration": 30}), now=_NOW
    )

    assert plan.kind is InventoryEffectKind.VIP_GRANT
    assert plan.vip_grant is not None
    assert plan.vip_grant.duration_days == 30


def test_vip_data_duration_overrides_a_name_that_says_otherwise() -> None:
    # Negative control for the test above: if the name table still won,
    # this would read 365. The operator edited ``data``; that is the
    # number the grant must honour.
    plan = plan_effect_application(
        _item(type_="vip", name="👑 VIP (1 год)", data={"duration": 7}), now=_NOW
    )

    assert plan.vip_grant is not None
    assert plan.vip_grant.duration_days == 7


def test_vip_falls_back_to_the_name_table_when_data_is_silent() -> None:
    plan = plan_effect_application(_item(type_="vip", name="👑 VIP (1 год)"), now=_NOW)

    assert plan.vip_grant is not None
    assert plan.vip_grant.duration_days == 365


@pytest.mark.parametrize("raw", [None, 0, -30, True, "", "abc", 1.5, [30]])
def test_vip_unusable_data_duration_falls_through(raw: object) -> None:
    """Zero, negative, ``True`` and junk must not become a term.

    ``True`` is the interesting one: ``isinstance(True, int)`` holds, so
    a naive positive-int check would grant exactly one day.
    """
    data = {} if raw is None else {"duration": raw}
    plan = plan_effect_application(
        _item(type_="vip", name="👑 VIP (3 месяца)", data=data), now=_NOW
    )

    assert plan.vip_grant is not None
    assert plan.vip_grant.duration_days == 90


def test_vip_with_unknown_name_and_no_data_grants_the_legacy_default() -> None:
    # Operator-seeded "VIP 2 weeks" with nothing usable anywhere. Legacy
    # granted 30 days here (``bot.py:13155`` default); UNKNOWN would mean
    # the buyer paid and got nothing, and there is no legacy process left
    # to pick the item up.
    plan = plan_effect_application(_item(type_="vip", name="👑 VIP (2 недели)"), now=_NOW)

    assert plan.kind is InventoryEffectKind.VIP_GRANT
    assert plan.vip_grant is not None
    assert plan.vip_grant.duration_days == 30


# ----------------------------------------------------------------------
# COLOR_NICK
# ----------------------------------------------------------------------


def test_canonical_color_nick_row_classifies_as_color_nick() -> None:
    plan = plan_effect_application(_item(type_="color_nick", name="🌈 Цветной ник"), now=_NOW)

    assert plan.kind is InventoryEffectKind.COLOR_NICK
    assert plan.vip_grant is None
    assert plan.privilege_grant is None
    assert plan.color_nick_grant is not None
    # Legacy defaults from apply_color_nick(bot.py:13406):
    # duration=7 days, color="rainbow".
    assert plan.color_nick_grant.duration_days == 7
    assert plan.color_nick_grant.color == "rainbow"


def test_color_nick_grant_resolves_to_now_plus_7_days() -> None:
    plan = plan_effect_application(_item(type_="color_nick", name="🌈 Цветной ник"), now=_NOW)
    assert plan.color_nick_grant is not None

    assert plan.color_nick_grant.granted_till_from(_NOW) == _NOW + timedelta(days=7)


def test_color_nick_reads_duration_and_colour_the_operator_declared() -> None:
    # #192/#1204: ``item.data`` outranks both the name table and the
    # legacy default, because it is the only place the operator's own
    # intent is recorded.
    plan = plan_effect_application(
        _item(
            type_="color_nick",
            name="🎨 Радужное имя на 30 дней",
            data={"duration": 30, "color": "gold"},
        ),
        now=_NOW,
    )

    assert plan.kind is InventoryEffectKind.COLOR_NICK
    assert plan.color_nick_grant is not None
    assert plan.color_nick_grant.duration_days == 30
    assert plan.color_nick_grant.color == "gold"


def test_color_nick_with_unknown_name_and_no_data_grants_the_legacy_default() -> None:
    # #1204: this row used to classify as UNKNOWN. The service refuses
    # UNKNOWN BEFORE the consume, so the item survived, the coins did
    # not, and nothing could ever redeem it. Legacy paid 7 days and
    # "rainbow" here (``bot.py:13149-13150``); so do we.
    plan = plan_effect_application(
        _item(type_="color_nick", name="🎨 Радужное имя на 30 дней"),
        now=_NOW,
    )

    assert plan.kind is InventoryEffectKind.COLOR_NICK
    assert plan.color_nick_grant is not None
    assert plan.color_nick_grant.duration_days == 7
    assert plan.color_nick_grant.color == "rainbow"


@pytest.mark.parametrize("raw", [0, -3, True, "abc", None, 1.5])
def test_color_nick_ignores_an_unusable_declared_duration(raw: object) -> None:
    # A zero/negative/boolean/non-numeric ``duration`` must fall through
    # to the next source rather than granting a zero-length effect.
    plan = plan_effect_application(
        _item(type_="color_nick", name="🌈 Цветной ник", data={"duration": raw}),
        now=_NOW,
    )

    assert plan.color_nick_grant is not None
    assert plan.color_nick_grant.duration_days == 7


# ----------------------------------------------------------------------
# MUTE_PROTECTION
# ----------------------------------------------------------------------


def test_canonical_mute_protection_row_classifies_as_mute_protection() -> None:
    plan = plan_effect_application(
        _item(type_="mute_protection", name="🔇 Защита от мута"), now=_NOW
    )

    assert plan.kind is InventoryEffectKind.MUTE_PROTECTION
    assert plan.vip_grant is None
    assert plan.privilege_grant is None
    assert plan.color_nick_grant is None
    assert plan.mute_protection_grant is not None
    # Legacy seed (``init_default_items`` at ``bot.py:12386-12393``):
    # ``{"duration": 24}`` in hours.
    assert plan.mute_protection_grant.duration_hours == 24


def test_mute_protection_grant_resolves_to_now_plus_24_hours() -> None:
    plan = plan_effect_application(
        _item(type_="mute_protection", name="🔇 Защита от мута"), now=_NOW
    )
    assert plan.mute_protection_grant is not None

    assert plan.mute_protection_grant.granted_till_from(_NOW) == _NOW + timedelta(hours=24)


def test_mute_protection_reads_the_duration_the_operator_declared() -> None:
    # Hours, matching legacy ``data.get("duration", 24)`` at
    # ``bot.py:13180``.
    plan = plan_effect_application(
        _item(type_="mute_protection", name="🤐 Anti-mute 48h", data={"duration": 48}),
        now=_NOW,
    )

    assert plan.kind is InventoryEffectKind.MUTE_PROTECTION
    assert plan.mute_protection_grant is not None
    assert plan.mute_protection_grant.duration_hours == 48


def test_mute_protection_with_unknown_name_and_no_data_grants_the_legacy_default() -> None:
    # #1204: same permanently-bricked-item class as color_nick above.
    plan = plan_effect_application(
        _item(type_="mute_protection", name="🤐 Anti-mute 48h"),
        now=_NOW,
    )

    assert plan.kind is InventoryEffectKind.MUTE_PROTECTION
    assert plan.mute_protection_grant is not None
    assert plan.mute_protection_grant.duration_hours == 24


# ----------------------------------------------------------------------
# LUCK_COIN_PAYOUT
# ----------------------------------------------------------------------


def _fixed_rng(value: int) -> Callable[[int, int], int]:
    """Mock RNG that always returns the same value for deterministic testing."""

    def mock_randint(min_val: int, max_val: int) -> int:
        return min(max(value, min_val), max_val)  # Clamp to bounds

    return mock_randint


def test_canonical_big_gift_item_classifies_as_luck_coin_payout() -> None:
    mock_rng = _fixed_rng(200)
    plan = plan_effect_application(
        # #1790: 250 against the legacy shape's mean of 200.89 — the
        # same ~20% house edge the live 2500-coin row carries. The
        # planner refuses a luck row that pays more than it costs, so
        # the price is now part of what makes this a valid catalog row.
        _item(type_="luck", name="🎲 Большой подарок", price=250),
        now=_NOW,
        random_int_inclusive=mock_rng,
    )

    assert plan.kind is InventoryEffectKind.LUCK_COIN_PAYOUT
    assert plan.vip_grant is None
    assert plan.privilege_grant is None
    assert plan.color_nick_grant is None
    assert plan.mute_protection_grant is None
    assert plan.coin_payout is not None
    assert plan.coin_payout.min_coins == 100
    assert plan.coin_payout.max_coins == 500
    assert plan.coin_payout.is_big_gift is True
    assert plan.coin_payout.is_secret_gift is False


def test_canonical_secret_gift_item_classifies_as_luck_coin_payout() -> None:
    mock_rng = _fixed_rng(50)
    plan = plan_effect_application(
        _item(type_="luck", name="🎲 Секретный подарок"), now=_NOW, random_int_inclusive=mock_rng
    )

    assert plan.kind is InventoryEffectKind.LUCK_COIN_PAYOUT
    assert plan.coin_payout is not None
    assert plan.coin_payout.min_coins == 10
    assert plan.coin_payout.max_coins == 100
    assert plan.coin_payout.is_big_gift is False
    assert plan.coin_payout.is_secret_gift is True


def test_big_gift_generates_weighted_payout() -> None:
    mock_rng = _fixed_rng(200)
    plan = plan_effect_application(
        # #1790: 250 against the legacy shape's mean of 200.89 — the
        # same ~20% house edge the live 2500-coin row carries. The
        # planner refuses a luck row that pays more than it costs, so
        # the price is now part of what makes this a valid catalog row.
        _item(type_="luck", name="🎲 Большой подарок", price=250),
        now=_NOW,
        random_int_inclusive=mock_rng,
    )
    assert plan.coin_payout is not None

    # The weighted choice should pick a range then use RNG within that range
    # With mock_rng returning 200, it should land in the high-weight 151-200 range
    payout = plan.coin_payout.generate_payout()
    assert 100 <= payout <= 500
    # Since we're mocking the RNG to return 200, the actual value depends on
    # the weighted algorithm, but should be deterministic


def test_secret_gift_generates_weighted_payout() -> None:
    mock_rng = _fixed_rng(50)
    plan = plan_effect_application(
        _item(type_="luck", name="🎲 Секретный подарок"), now=_NOW, random_int_inclusive=mock_rng
    )
    assert plan.coin_payout is not None

    payout = plan.coin_payout.generate_payout()
    assert 10 <= payout <= 100


def test_luck_with_unknown_name_pays_uniformly_from_its_data_bounds() -> None:
    # Operator-seeded "Lucky coins 500-1000" — type matches, name doesn't.
    # #192: the min/max live in the ``data`` JSON the entity used to drop,
    # which is why this deferred to a legacy process that no longer runs.
    # Legacy drew uniformly from them (``bot.py:13165-13170``).
    plan = plan_effect_application(
        _item(
            type_="luck",
            name="🍀 Супер удача 500-1000 монет",
            price=1000,  # #1790: above the uniform mean of 750.
            data={"min": 500, "max": 1000},
        ),
        now=_NOW,
        random_int_inclusive=_fixed_rng(777),
    )

    assert plan.kind is InventoryEffectKind.LUCK_COIN_PAYOUT
    assert plan.coin_payout is not None
    assert (plan.coin_payout.min_coins, plan.coin_payout.max_coins) == (500, 1000)
    assert plan.coin_payout.is_big_gift is False
    assert plan.coin_payout.is_secret_gift is False
    assert plan.coin_payout.generate_payout() == 777


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"min": 100},
        {"max": 500},
        {"min": "abc", "max": 500},
        {"min": 500, "max": 100},
        {"min": -5, "max": 500},
        {"min": True, "max": 500},
    ],
)
def test_luck_with_unknown_name_and_unusable_bounds_stays_unknown(data: dict[str, object]) -> None:
    # Negative control for the test above. Without a shape AND without
    # a sane pair there is no number to pay that wouldn't be invented,
    # so the coins stay un-spent rather than being guessed at.
    plan = plan_effect_application(_item(type_="luck", name="🍀 Супер удача", data=data), now=_NOW)

    assert plan.kind is InventoryEffectKind.UNKNOWN
    assert plan.coin_payout is None


def test_coin_payout_spec_uniform_distribution() -> None:
    """Test CoinPayoutSpec with uniform RNG (no special gift flags)."""
    mock_rng = _fixed_rng(150)
    spec = CoinPayoutSpec(
        min_coins=100,
        max_coins=200,
        random_int_inclusive=mock_rng,
        is_big_gift=False,
        is_secret_gift=False,
    )

    payout = spec.generate_payout()
    assert payout == 150  # Mock RNG returns 150


def test_coin_payout_spec_big_gift_weighted_distribution() -> None:
    """Test CoinPayoutSpec with big gift weighted distribution."""
    # Use a mock that returns different values for weight selection vs payout
    call_count = 0

    def sequential_rng(min_val: int, max_val: int) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: weight selection (pick weight 38 range: 151-200)
            return 60  # Should select the 151-200 range
        # Second call: value within range
        return min(max(175, min_val), max_val)

    spec = CoinPayoutSpec(
        min_coins=100,
        max_coins=500,
        random_int_inclusive=sequential_rng,
        is_big_gift=True,
        is_secret_gift=False,
    )

    payout = spec.generate_payout()
    assert 100 <= payout <= 500


# ----------------------------------------------------------------------
# UNKNOWN (deferred kinds)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "deferred_type",
    [
        # #1204 removed "xp_boost" from this list: it no longer defers on
        # an unrecognised name. What remains here are types with no
        # effect implementation at all.
        "legend",
        "ad",
        "gift",
        "custom_color",
    ],
)
def test_deferred_kinds_classify_as_unknown(deferred_type: str) -> None:
    # L-21 promoted custom_title + unwarn (they classify regardless of
    # name); they're no longer in this deferred list. See
    # test_inventory_use_planner_l21 for their coverage. UNKNOWN here is
    # honest: there is no effect implementation to reach at all, which
    # is a different thing from #1204's "implemented but unreachable".
    plan = plan_effect_application(_item(type_=deferred_type), now=_NOW)

    assert plan.kind is InventoryEffectKind.UNKNOWN
    assert plan.vip_grant is None
    assert plan.privilege_grant is None


def test_arbitrary_unknown_type_classifies_as_unknown() -> None:
    plan = plan_effect_application(_item(type_="totally_made_up"), now=_NOW)
    assert plan.kind is InventoryEffectKind.UNKNOWN


# ----------------------------------------------------------------------
# Invariants
# ----------------------------------------------------------------------


def test_plan_rejects_both_specs_populated() -> None:
    with pytest.raises(ValueError, match="at most one"):
        EffectPlan(
            kind=InventoryEffectKind.VIP_GRANT,
            vip_grant=VipGrantSpec(duration_days=30),
            privilege_grant=PrivilegeGrantSpec(
                privilege_type="double_daily",
                duration_seconds=None,
                value=None,
            ),
        )


def test_vip_grant_kind_without_spec_is_rejected() -> None:
    with pytest.raises(ValueError, match="VIP_GRANT"):
        EffectPlan(kind=InventoryEffectKind.VIP_GRANT)


def test_double_daily_kind_without_spec_is_rejected() -> None:
    with pytest.raises(ValueError, match="DOUBLE_DAILY_BUSTER"):
        EffectPlan(kind=InventoryEffectKind.DOUBLE_DAILY_BUSTER)


def test_unknown_kind_must_carry_no_spec() -> None:
    with pytest.raises(ValueError, match="UNKNOWN"):
        EffectPlan(
            kind=InventoryEffectKind.UNKNOWN,
            vip_grant=VipGrantSpec(duration_days=30),
        )


def test_color_nick_kind_without_spec_is_rejected() -> None:
    with pytest.raises(ValueError, match="COLOR_NICK"):
        EffectPlan(kind=InventoryEffectKind.COLOR_NICK)


def test_color_nick_kind_with_wrong_spec_is_rejected() -> None:
    # The kind ↔ spec coherence guard: a COLOR_NICK plan carrying a
    # vip_grant slips past the "right kind set" check only if we forget
    # the symmetric "right spec for the kind" check. Pin both halves.
    with pytest.raises(ValueError, match="COLOR_NICK"):
        EffectPlan(
            kind=InventoryEffectKind.COLOR_NICK,
            vip_grant=VipGrantSpec(duration_days=30),
        )


def test_mute_protection_kind_without_spec_is_rejected() -> None:
    with pytest.raises(ValueError, match="MUTE_PROTECTION"):
        EffectPlan(kind=InventoryEffectKind.MUTE_PROTECTION)


def test_plan_rejects_mute_protection_plus_color_nick_specs_populated() -> None:
    # Two value-carrying privilege specs both populated — the
    # at-most-one invariant pins that the planner output stays one
    # spec per kind no matter how many privilege-shaped kinds ship.
    with pytest.raises(ValueError, match="at most one"):
        EffectPlan(
            kind=InventoryEffectKind.MUTE_PROTECTION,
            mute_protection_grant=MuteProtectionGrantSpec(duration_hours=24),
            color_nick_grant=ColorNickGrantSpec(duration_days=7, color="rainbow"),
        )


def test_plan_rejects_color_nick_plus_vip_specs_populated() -> None:
    with pytest.raises(ValueError, match="at most one"):
        EffectPlan(
            kind=InventoryEffectKind.COLOR_NICK,
            color_nick_grant=ColorNickGrantSpec(duration_days=7, color="rainbow"),
            vip_grant=VipGrantSpec(duration_days=30),
        )


def test_luck_coin_payout_kind_without_spec_is_rejected() -> None:
    with pytest.raises(ValueError, match="LUCK_COIN_PAYOUT"):
        EffectPlan(kind=InventoryEffectKind.LUCK_COIN_PAYOUT)


def test_plan_rejects_coin_payout_plus_vip_specs_populated() -> None:
    with pytest.raises(ValueError, match="at most one"):
        EffectPlan(
            kind=InventoryEffectKind.LUCK_COIN_PAYOUT,
            coin_payout=CoinPayoutSpec(
                min_coins=100,
                max_coins=500,
                random_int_inclusive=_fixed_rng(200),
                is_big_gift=True,
            ),
            vip_grant=VipGrantSpec(duration_days=30),
        )


# ----------------------------------------------------------------------
# Purity
# ----------------------------------------------------------------------


def test_planner_is_deterministic_for_same_inputs() -> None:
    # Two calls with identical inputs must yield equal plans —
    # rules out any hidden clock read or RNG drift.
    item = _item(type_="vip", name="👑 VIP (3 месяца)")
    plan_a = plan_effect_application(item, now=_NOW)
    plan_b = plan_effect_application(item, now=_NOW)
    assert plan_a == plan_b


# ----------------------------------------------------------------------
# The production catalog itself (#192)
# ----------------------------------------------------------------------

# Transcribed from the live ``economy.db`` on 2026-08-20 and re-read
# on 2026-09-09 — id, name, price, type, stock, data. The re-read moved
# row 4 from 2000 to 2500: the operator had raised the price and this
# fixture had not followed, which left the file asserting a big gift
# that pays 100.4% of its price. "Nobody is free to adjust it" means
# adjust it only to whatever ``sqlite3 -readonly`` on the prod host
# actually returns — never to whatever makes the code pass. #192 was invisible to every test in this
# file because every fixture here was hand-written to match the code:
# the planner's luck table had been keyed on strings taken from the
# seed's DESCRIPTION column while the rows are matched on NAME, and its
# VIP table listed four names that exist in no catalog anywhere. Both
# tables passed their own tests and classified three of the seven
# buyable SKUs as UNKNOWN in production, which means the buyer paid and
# got nothing. So the catalog is pinned here verbatim: a fixture nobody
# is free to adjust until it matches the code.
_LIVE_CATALOG: tuple[tuple[int, str, int, str, int, dict[str, object]], ...] = (
    (1, "🌈 Цветной ник", 500, "color_nick", 0, {"duration": 7, "color": "rainbow"}),
    (2, "🎨 Свой цвет", 1000, "custom_color", 0, {"duration": 30}),
    (3, "🎁 Секретный подарок", 500, "luck", 82, {"min": 100, "max": 1000}),
    (4, "🎁 Большой подарок", 2500, "luck", 50, {"min": 1000, "max": 5000}),
    (
        5,
        "👑 VIP статус",
        5000,
        "vip",
        -1,
        {
            "duration": 30,
            "message_bonus": 1,
            "daily_bonus_percent": 15,
            "tax_discount_percent": 50,
        },
    ),
    (6, "💎 Легендарный статус", 10000, "legend", 0, {"color": "rainbow", "badge": "👑"}),
    (7, "📢 Реклама", 3000, "ad", 0, {"duration": 24}),
    (8, "🛡️ Снятие предупреждения", 800, "unwarn", 40, {}),
    (9, "🔇 Защита от мута", 1500, "mute_protection", 30, {"duration": 24}),
    (10, "✨ Двойной daily", 2000, "double_daily", 20, {}),
    (11, "⚡ Ускорение", 2500, "xp_boost", 6, {"duration": 60, "multiplier": 2}),
    (12, "📝 Свой титул", 3000, "custom_title", 0, {"duration": 7}),
)

# A row is buyable when ``stock != 0``; ``-1`` means unlimited.
_BUYABLE_LIVE_ROWS = tuple(row for row in _LIVE_CATALOG if row[4] != 0)


def _live_item(row: tuple[int, str, int, str, int, dict[str, object]]) -> ShopItemEntity:
    item_id, name, price, type_, stock, data = row
    return _item(item_id=item_id, name=name, price=price, type_=type_, stock=stock, data=data)


@pytest.mark.parametrize("row", _BUYABLE_LIVE_ROWS, ids=lambda row: f"{row[0]}-{row[3]}")
def test_every_buyable_production_sku_classifies(
    row: tuple[int, str, int, str, int, dict[str, object]],
) -> None:
    """No SKU a user can actually buy may plan to UNKNOWN.

    UNKNOWN means the inventory row stays unconsumed and nothing is
    granted — and since no legacy process runs alongside the new bot any
    more, nothing picks it up afterwards either. The coins are simply
    gone.
    """
    plan = plan_effect_application(_live_item(row), now=_NOW)
    assert plan.kind is not InventoryEffectKind.UNKNOWN


def test_the_three_unclassified_production_rows_are_all_out_of_stock() -> None:
    """The counterpart guard: ``custom_color``, ``legend`` and ``ad`` have
    no effect implementation at all, which is honest UNKNOWN rather than
    a bug — precisely because the operator has them at ``stock=0`` and
    nobody can buy them. If one is ever restocked this test fails and
    says so, instead of the failure showing up as a silent coin loss.
    """
    unclassified = {
        row[0]
        for row in _LIVE_CATALOG
        if plan_effect_application(_live_item(row), now=_NOW).kind is InventoryEffectKind.UNKNOWN
    }
    assert unclassified == {2, 6, 7}
    assert all(row[4] == 0 for row in _LIVE_CATALOG if row[0] in unclassified)


def test_luck_shapes_match_the_name_column_not_the_description() -> None:
    """The exact shape of #192, pinned so it cannot come back.

    The seed writes ``name='🎁 Большой подарок'`` and
    ``description='🎲 Случайный приз от 1000 до 5000 монет'``. The port
    keyed its shape table on strings transcribed from the description,
    so no live row ever matched. A description that mentions another
    plan's name must not sway the classification either way.
    """
    by_name = plan_effect_application(
        _item(type_="luck", name="🎁 Большой подарок", price=250, description="что-то другое"),
        now=_NOW,
    )
    assert by_name.coin_payout is not None
    assert by_name.coin_payout.is_big_gift is True

    by_description = plan_effect_application(
        _item(
            type_="luck",
            name="🎲 Случайный приз",
            price=5000,  # #1790: above the uniform mean of 3000.
            description="🎁 Большой подарок от 1000 до 5000 монет",
            data={"min": 1000, "max": 5000},
        ),
        now=_NOW,
    )
    assert by_description.coin_payout is not None
    assert by_description.coin_payout.is_big_gift is False


def _expected_payout(spec: CoinPayoutSpec, *, draws: int = 20_000) -> float:
    """Mean payout over a fixed-seed sample of ``draws`` redemptions."""
    rng = random.Random(20260820)
    sampled = CoinPayoutSpec(
        min_coins=spec.min_coins,
        max_coins=spec.max_coins,
        random_int_inclusive=rng.randint,
        is_big_gift=spec.is_big_gift,
        is_secret_gift=spec.is_secret_gift,
    )
    return sum(sampled.generate_payout() for _ in range(draws)) / draws


_LIVE_LUCK_ROWS = tuple(row for row in _BUYABLE_LIVE_ROWS if row[3] == "luck")


@pytest.mark.parametrize("row", _LIVE_LUCK_ROWS, ids=lambda row: str(row[0]))
def test_live_luck_rows_pay_inside_their_advertised_range_and_keep_a_house_edge(
    row: tuple[int, str, int, str, int, dict[str, object]],
) -> None:
    """Two guards on the same draw, both about the owner's wallet.

    The payout must land inside the span the row's own description
    advertises — paying 201 coins for an item that promises "от 1000 до
    5000" is a support ticket. And the mean must not exceed the price at
    all: a luck SKU is a coin SINK, and one that returns more than it
    costs is a faucet any user can farm.

    The bound was 1.05 until #1790, to leave room for ``🎁 Большой
    подарок`` sitting at 1.006 — the legacy bands' own ratio at
    the legacy price of 2000. That slack is gone twice over: the live
    row has charged 2500 since (0.80), and the planner now refuses to
    plan a net-positive luck row at all, so one that broke 1.00 would
    fail ``test_every_buyable_production_sku_classifies`` before ever
    reaching this assertion.
    """
    item = _live_item(row)
    plan = plan_effect_application(item, now=_NOW)
    spec = plan.coin_payout
    assert spec is not None
    assert (spec.min_coins, spec.max_coins) == (item.data["min"], item.data["max"])

    mean = _expected_payout(spec)
    assert spec.min_coins <= mean <= spec.max_coins
    assert 0.70 <= mean / item.price <= 1.00


# --- #1790: a luck row must not pay more than it costs -----------------
#
# ``shop_items`` has no CHECK constraint and no validation of ``name`` /
# ``price`` / ``data``, so a row's economics are whatever three
# independently editable columns happen to say. The planner refuses to
# plan a net-positive draw; these pin both the refusal and the exact
# line it is drawn on.


def test_net_positive_uniform_luck_row_is_refused() -> None:
    """The ticket's own scenario: a rename turns a sink into a faucet.

    Rename the live big gift — pluralise it, translate it, slip a word
    between the emoji and the noun — and ``_luck_shape_for`` stops
    matching. The row keeps its ``data`` bounds, so the uniform arm
    takes over and draws a flat 1000-5000: a mean of 3000 against a
    price of 2500, +20% to the buyer, repeatable for as long as the
    operator restocks. Refusing costs the buyer a redemption he paid
    for, but nothing is consumed and the operator gets a warning log —
    a loud cost, against a faucet that drains quietly.
    """
    plan = plan_effect_application(
        _item(
            type_="luck",
            name="🎁 Большие подарки",
            price=2500,
            data={"min": 1000, "max": 5000},
        ),
        now=_NOW,
    )

    assert plan.kind is InventoryEffectKind.UNKNOWN
    assert plan.coin_payout is None


def test_net_positive_shaped_luck_row_is_refused() -> None:
    """The other half: the name still matches, the PRICE moved.

    Put the live row's price back to the legacy 2000 and the weighted
    bands beat it by 0.4% — small, permanent, and invisible without
    this guard. The name matches, so this exercises the shaped arm
    rather than the uniform one.
    """
    plan = plan_effect_application(
        _item(
            type_="luck",
            name="🎁 Большой подарок",
            price=2000,
            data={"min": 1000, "max": 5000},
        ),
        now=_NOW,
    )

    assert plan.kind is InventoryEffectKind.UNKNOWN
    assert plan.coin_payout is None


def test_break_even_luck_row_is_still_planned() -> None:
    """The comparison is strictly greater, and that is deliberate.

    A row that mints nothing on average is a legitimate design — a
    pure gamble with no house edge — so only a genuinely net-positive
    expectation is refused. Priced at exactly the uniform mean of
    ``1000..5000``, this row sits on the line and is planned.
    """
    plan = plan_effect_application(
        _item(
            type_="luck",
            name="🍀 Ровная удача",
            price=3000,
            data={"min": 1000, "max": 5000},
        ),
        now=_NOW,
    )

    assert plan.kind is InventoryEffectKind.LUCK_COIN_PAYOUT
    assert plan.coin_payout is not None
    assert plan.coin_payout.expected_payout() == 3000.0


@pytest.mark.parametrize("price", [0, -1])
def test_free_or_negatively_priced_luck_row_is_refused(price: int) -> None:
    """A price of zero is the degenerate faucet, and it is not special-cased.

    Nothing in ``shop_items`` stops a price of 0 (a giveaway row left in
    the catalog) or a negative one (a typo), and either makes every draw
    net-positive. The guard needs no separate branch for them: any
    positive mean already exceeds a non-positive price.
    """
    plan = plan_effect_application(
        _item(
            type_="luck",
            name="🍀 Бесплатная удача",
            price=price,
            data={"min": 100, "max": 500},
        ),
        now=_NOW,
    )

    assert plan.kind is InventoryEffectKind.UNKNOWN
    assert plan.coin_payout is None


def test_the_guard_reads_the_price_and_nothing_else_about_the_row() -> None:
    """Same span, two prices, opposite verdicts.

    Holds the name, the bounds and the shape fixed and moves only
    ``price`` across the mean, so a future change that made the guard
    depend on stock, description or id would fail here.
    """

    def _plan(price: int) -> EffectPlan:
        return plan_effect_application(
            _item(
                type_="luck",
                name="🍀 Пограничная удача",
                price=price,
                data={"min": 1000, "max": 5000},
            ),
            now=_NOW,
        )

    assert _plan(3001).kind is InventoryEffectKind.LUCK_COIN_PAYOUT
    assert _plan(2999).kind is InventoryEffectKind.UNKNOWN


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(
            CoinPayoutSpec(
                min_coins=1000,
                max_coins=5000,
                random_int_inclusive=random.Random(0).randint,
                is_big_gift=True,
            ),
            id="big-gift-live-span",
        ),
        pytest.param(
            CoinPayoutSpec(
                min_coins=100,
                max_coins=500,
                random_int_inclusive=random.Random(0).randint,
                is_big_gift=True,
            ),
            id="big-gift-legacy-span",
        ),
        pytest.param(
            CoinPayoutSpec(
                min_coins=100,
                max_coins=1000,
                random_int_inclusive=random.Random(0).randint,
                is_secret_gift=True,
            ),
            id="secret-gift-live-span",
        ),
        pytest.param(
            CoinPayoutSpec(
                min_coins=10,
                max_coins=100,
                random_int_inclusive=random.Random(0).randint,
                is_secret_gift=True,
            ),
            id="secret-gift-legacy-span",
        ),
        pytest.param(
            CoinPayoutSpec(
                min_coins=1000,
                max_coins=5000,
                random_int_inclusive=random.Random(0).randint,
            ),
            id="uniform",
        ),
    ],
)
def test_expected_payout_agrees_with_an_actual_sample(spec: CoinPayoutSpec) -> None:
    """The closed form must describe the draw it claims to describe.

    ``expected_payout`` is arithmetic, not sampled, so a catalog row's
    classification cannot depend on a seed. The risk of computing the
    mean separately from the draw is that the two drift; this pins them
    together against 20 000 real redemptions. One percent is generous
    against the worst observed gap of 0.53% (the uniform arm, whose
    per-draw variance is the largest), and the sample is fixed-seed, so
    the bound cannot flake.
    """
    exact = spec.expected_payout()
    sampled = _expected_payout(spec)

    assert abs(sampled - exact) / exact < 0.01


def test_a_break_even_luck_row_is_refused_once_the_group_rebate_pays_it_back() -> None:
    """#1930: the price is not what a group-scoped buyer is out of pocket.

    One ``/shop`` confirm applies the item and then mints
    ``purchase_donation_to_group_percent`` of the same price to the
    chosen group's creator — who can be the buyer, since anyone can make
    a group, add the bot and pick it on the card. A row at exactly
    break-even against the gross price passes #1790 (it mints nothing on
    its own) and is a repeatable net-positive loop for that buyer, so
    the sink test has to run against the price NET of the rebate.
    """
    item = _item(type_="luck", name="unmapped", price=1000, data={"min": 900, "max": 1100})

    assert (
        plan_effect_application(item, now=_NOW, random_int_inclusive=_fixed_rng(1000)).kind
        is InventoryEffectKind.LUCK_COIN_PAYOUT
    )
    assert (
        plan_effect_application(
            item,
            now=_NOW,
            random_int_inclusive=_fixed_rng(1000),
            group_rebate_percent=15,
        ).kind
        is InventoryEffectKind.UNKNOWN
    )


def test_the_rebate_does_not_refuse_a_row_with_a_real_house_edge() -> None:
    """The live catalog's ~20% edge clears the 15% rebate untouched."""
    item = _item(type_="luck", name="unmapped", price=1000, data={"min": 700, "max": 900})

    plan = plan_effect_application(
        item,
        now=_NOW,
        random_int_inclusive=_fixed_rng(800),
        group_rebate_percent=15,
    )

    assert plan.kind is InventoryEffectKind.LUCK_COIN_PAYOUT
