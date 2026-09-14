"""Unit coverage for the L-21 planner kinds (xp_boost / custom_title / unwarn).

Pure-logic tests, same posture as ``test_inventory_use_planner.py``:
exercise the new classification branches, the canonical name tables, the
``item.data`` override #1204 restored on top of them, and the
:class:`EffectPlan` invariant guards for the new spec fields.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from telegram_invite_bot.core.entities.shop import ShopItemEntity
from telegram_invite_bot.services.inventory_use_planner import (
    CustomTitleSpec,
    EffectPlan,
    InventoryEffectKind,
    VipGrantSpec,
    XpBoostGrantSpec,
    plan_effect_application,
)

_NOW = datetime(2026, 5, 15, 12, 0, 0)


def _item(
    *,
    name: str = "X",
    type_: str = "unknown",
    data: dict[str, object] | None = None,
) -> ShopItemEntity:
    return ShopItemEntity(
        id=1,
        name=name,
        description="",
        price=100,
        type=type_,
        stock=-1,
        data=data or {},
    )


# ----------------------------------------------------------------------
# XP_BOOST
# ----------------------------------------------------------------------


def test_canonical_xp_boost_classifies_with_legacy_duration_and_multiplier() -> None:
    plan = plan_effect_application(_item(type_="xp_boost", name="⚡ Ускорение"), now=_NOW)
    assert plan.kind is InventoryEffectKind.XP_BOOST
    assert plan.xp_boost_grant is not None
    # Legacy seed bot.py:12402-12409 + apply_xp_boost defaults bot.py:13561.
    assert plan.xp_boost_grant.duration_minutes == 60
    assert plan.xp_boost_grant.multiplier == 2


def test_xp_boost_expiry_is_now_plus_duration_minutes() -> None:
    spec = XpBoostGrantSpec(duration_minutes=60, multiplier=2)
    assert spec.granted_till_from(_NOW) == _NOW + timedelta(minutes=60)


def test_non_canonical_xp_boost_name_still_grants_the_legacy_default() -> None:
    # #1204: this used to classify as UNKNOWN, and the service refuses
    # UNKNOWN *before* consuming the row — so the buyer's coins were
    # gone and the item could never be redeemed, with no refund path.
    # Legacy granted 60 minutes at ×2 here (bot.py:13561); so do we.
    plan = plan_effect_application(_item(type_="xp_boost", name="⚡ Мега-ускорение"), now=_NOW)
    assert plan.kind is InventoryEffectKind.XP_BOOST
    assert plan.xp_boost_grant is not None
    assert plan.xp_boost_grant.duration_minutes == 60
    assert plan.xp_boost_grant.multiplier == 2


def test_xp_boost_reads_duration_and_multiplier_the_operator_declared() -> None:
    # The exact #1204 harm scenario: an operator-seeded SKU whose name
    # matches no curated table but whose parameters are right there in
    # the ``shop_items.data`` blob legacy reads (bot.py:13145-13195).
    plan = plan_effect_application(
        _item(
            type_="xp_boost",
            name="⚡ Мега-ускорение",
            data={"duration": 120, "multiplier": 3},
        ),
        now=_NOW,
    )
    assert plan.kind is InventoryEffectKind.XP_BOOST
    assert plan.xp_boost_grant is not None
    assert plan.xp_boost_grant.duration_minutes == 120
    assert plan.xp_boost_grant.multiplier == 3


def test_xp_boost_declared_data_outranks_the_canonical_name_table() -> None:
    # A canonical name AND a data blob: the operator's own row wins,
    # because it is the only place their intent is recorded.
    plan = plan_effect_application(
        _item(type_="xp_boost", name="⚡ Ускорение", data={"duration": "30"}),
        now=_NOW,
    )
    assert plan.xp_boost_grant is not None
    assert plan.xp_boost_grant.duration_minutes == 30
    # Undeclared fields fall through independently.
    assert plan.xp_boost_grant.multiplier == 2


@pytest.mark.parametrize("raw", [0, -5, True, "x", None, 2.5, []])
def test_xp_boost_ignores_an_unusable_declared_multiplier(raw: object) -> None:
    # Zero/negative/boolean/non-numeric must fall through rather than
    # grant a ×0 boost the buyer paid real coins for.
    plan = plan_effect_application(
        _item(type_="xp_boost", name="⚡ Ускорение", data={"multiplier": raw}),
        now=_NOW,
    )
    assert plan.xp_boost_grant is not None
    assert plan.xp_boost_grant.multiplier == 2


def test_xp_boost_plan_requires_spec() -> None:
    with pytest.raises(ValueError, match="XP_BOOST"):
        EffectPlan(kind=InventoryEffectKind.XP_BOOST)


# ----------------------------------------------------------------------
# CUSTOM_TITLE
# ----------------------------------------------------------------------


def test_custom_title_classifies_for_any_name() -> None:
    # No name table — any custom_title row classifies (duration is
    # fixed in legacy; the variable part is the FSM-collected title).
    plan = plan_effect_application(_item(type_="custom_title", name="📝 Свой титул"), now=_NOW)
    assert plan.kind is InventoryEffectKind.CUSTOM_TITLE
    assert plan.custom_title is not None
    assert plan.custom_title.duration_days == 7  # legacy bot.py:13681/23984


def test_custom_title_expiry_is_now_plus_duration_days() -> None:
    spec = CustomTitleSpec(duration_days=7)
    assert spec.granted_till_from(_NOW) == _NOW + timedelta(days=7)


def test_custom_title_plan_requires_spec() -> None:
    with pytest.raises(ValueError, match="CUSTOM_TITLE"):
        EffectPlan(kind=InventoryEffectKind.CUSTOM_TITLE)


# ----------------------------------------------------------------------
# UNWARN
# ----------------------------------------------------------------------


def test_unwarn_classifies_with_no_spec() -> None:
    plan = plan_effect_application(_item(type_="unwarn", name="🛡️ Снятие предупреждения"), now=_NOW)
    assert plan.kind is InventoryEffectKind.UNWARN
    # Moderation side-effect — carries no economy-DB spec.
    assert plan.vip_grant is None
    assert plan.privilege_grant is None
    assert plan.coin_payout is None
    assert plan.xp_boost_grant is None
    assert plan.custom_title is None


def test_unwarn_plan_rejects_spec() -> None:
    with pytest.raises(ValueError, match="UNWARN"):
        EffectPlan(
            kind=InventoryEffectKind.UNWARN,
            vip_grant=VipGrantSpec(duration_days=30),
        )


# ----------------------------------------------------------------------
# determinism (no clock read / no RNG for the grant kinds)
# ----------------------------------------------------------------------


def test_new_kinds_are_deterministic() -> None:
    for item in (
        _item(type_="xp_boost", name="⚡ Ускорение"),
        _item(type_="custom_title", name="📝 Свой титул"),
        _item(type_="unwarn", name="🛡️ Снятие предупреждения"),
    ):
        assert plan_effect_application(item, now=_NOW) == plan_effect_application(item, now=_NOW)
