"""``InventoryUseService`` against a real SQLite economy DB.

Stage 28 atomicity story: consume + grant land together or neither
does. Each test pre-seeds the wallet / inventory / catalog rows
needed for one scenario, runs ``InventoryUseService.use`` and asserts
both the typed result and the resulting DB state. The race-pin uses
two concurrent ``use()`` calls over the same session — SQLite's
write serialisation plus the rowcount guard in ``InventoryRepo.consume``
collapse them to "exactly one SUCCESS, one ALREADY_USED".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    InventoryItem,
    ShopItem,
    Transaction,
    UserPrivilege,
)
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.services.inventory_use_planner import InventoryEffectKind
from telegram_invite_bot.services.inventory_use_service import (
    InventoryUseService,
    UseOutcome,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT

_NOW = datetime(2026, 5, 15, 12, 0, 0)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as s:
            yield s
    finally:
        await engine.dispose()


def _service(
    session: AsyncSession,
    *,
    random_int_inclusive: object = None,
) -> InventoryUseService:
    return InventoryUseService(
        InventoryRepo(session),
        VipRepo(session),
        PrivilegesRepo(session),
        ShopItemsRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
        random_int_inclusive=random_int_inclusive,  # type: ignore[arg-type]
    )


async def _seed_luck_item(session: AsyncSession, *, item_id: int = 5) -> int:
    # Canonical big-gift luck row — the planner's name→spec table keys
    # off this exact string (see ``_LUCK_NAME_SPECS``).
    session.add(
        ShopItem(
            id=item_id,
            name="🎲 Большой подарок",
            description="",
            price=1000,
            type="luck",
            stock=-1,
        )
    )
    return item_id


async def _seed_vip_item(session: AsyncSession, *, item_id: int = 1) -> int:
    session.add(
        ShopItem(
            id=item_id,
            name="👑 VIP (1 месяц)",
            description="",
            price=1000,
            type="vip",
            stock=-1,
        )
    )
    return item_id


async def _seed_color_nick_item(session: AsyncSession, *, item_id: int = 3) -> int:
    session.add(
        ShopItem(
            id=item_id,
            name="🌈 Цветной ник",
            description="",
            price=500,
            type="color_nick",
            stock=-1,
        )
    )
    return item_id


async def _seed_mute_protection_item(session: AsyncSession, *, item_id: int = 4) -> int:
    # Name matches the canonical seed at ``bot.py:12386-12393``;
    # the planner's name→duration table keys off this exact string.
    session.add(
        ShopItem(
            id=item_id,
            name="🔇 Защита от мута",
            description="",
            price=300,
            type="mute_protection",
            stock=-1,
        )
    )
    return item_id


async def _seed_buster_item(session: AsyncSession, *, item_id: int = 2) -> int:
    session.add(
        ShopItem(
            id=item_id,
            name="2x daily",
            description="",
            price=500,
            type="double_daily",
            stock=-1,
        )
    )
    return item_id


async def _seed_inventory_row(
    session: AsyncSession,
    *,
    user_id: int,
    item_id: int,
    used: bool = False,
    expires: datetime | None = None,
) -> int:
    entry = InventoryItem(
        user_id=user_id,
        item_id=item_id,
        purchase_date=_NOW - timedelta(days=1),
        used=used,
        expires=expires,
    )
    session.add(entry)
    await session.flush()
    return entry.id


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_use_vip_item_grants_global_vip_and_marks_entry_used(
    session: AsyncSession,
) -> None:
    item_id = await _seed_vip_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=None))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert result.kind is InventoryEffectKind.VIP_GRANT
    assert result.granted_till == _NOW + timedelta(days=30)
    assert result.buster_expires_at is None

    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is True
    assert row.used_date == _NOW

    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.vip_till == (_NOW + timedelta(days=30)).timestamp()


async def test_use_color_nick_writes_privilege_row_with_payload_and_marks_used(
    session: AsyncSession,
) -> None:
    """Happy-path COLOR_NICK: privilege row written with the JSON
    ``{"color": "rainbow"}`` payload + a ``now + 7d`` expiry, inventory
    row consumed, result carries ``color_nick_expires_at``."""
    item_id = await _seed_color_nick_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert result.kind is InventoryEffectKind.COLOR_NICK
    assert result.granted_till is None
    assert result.buster_expires_at is None
    assert result.color_nick_expires_at == _NOW + timedelta(days=7)

    priv = await session.get(UserPrivilege, (42, "color_nick", 0))
    assert priv is not None
    assert priv.expires_at == (_NOW + timedelta(days=7)).timestamp()
    # Payload JSON matches the planner's color default (rainbow).
    assert priv.value == '{"color": "rainbow"}'

    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is True


async def test_use_color_nick_replaces_existing_longer_grant(
    session: AsyncSession,
) -> None:
    """REPLACE semantics through the service: a pre-seeded 30-day
    color_nick gets OVERWRITTEN by a fresh 7-day activation — both
    expiry and payload. Mirrors legacy ``apply_color_nick``'s
    unconditional UPSERT posture (``bot.py:13406``); opposite of
    VIP's MAX-preserves-longer pin above."""
    item_id = await _seed_color_nick_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    # Pre-seed a longer-TTL color_nick row with a DIFFERENT color so
    # both halves of the replace (expiry + value) are observable.
    far_future_ts = (_NOW + timedelta(days=30)).timestamp()
    session.add(
        UserPrivilege(
            user_id=42,
            privilege_type="color_nick",
            group_id=0,
            expires_at=far_future_ts,
            value='{"color": "red"}',
        )
    )
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    # Service reports the duration it WROTE (now + 7d). And because
    # this is REPLACE not MAX, that's authoritative — the DB row
    # holds the same value (unlike the VIP MAX case).
    assert result.color_nick_expires_at == _NOW + timedelta(days=7)

    priv = await session.get(UserPrivilege, (42, "color_nick", 0))
    assert priv is not None
    assert priv.expires_at == (_NOW + timedelta(days=7)).timestamp()
    assert priv.value == '{"color": "rainbow"}'


async def test_use_mute_protection_writes_privilege_row_with_empty_payload(
    session: AsyncSession,
) -> None:
    """Happy-path MUTE_PROTECTION: privilege row written with the
    literal ``"{}"`` JSON payload + a ``now + 24h`` expiry, inventory
    row consumed, result carries ``mute_protection_expires_at``.
    Mirrors legacy ``apply_mute_protection`` (``bot.py:13638``) which
    pins ``value={}`` unconditionally — the read-side check inspects
    nothing inside the payload, so the empty object is purely a schema
    placeholder for the TEXT column."""
    item_id = await _seed_mute_protection_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert result.kind is InventoryEffectKind.MUTE_PROTECTION
    assert result.granted_till is None
    assert result.buster_expires_at is None
    assert result.color_nick_expires_at is None
    assert result.mute_protection_expires_at == _NOW + timedelta(hours=24)

    priv = await session.get(UserPrivilege, (42, "mute_protection", 0))
    assert priv is not None
    assert priv.expires_at == (_NOW + timedelta(hours=24)).timestamp()
    # Empty-object literal: schema placeholder for the TEXT column,
    # not a user-facing knob. See MuteProtectionGrantSpec docstring.
    assert priv.value == "{}"

    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is True


async def test_use_mute_protection_extends_an_existing_grant(
    session: AsyncSession,
) -> None:
    """#1950: a fresh 24h activation on top of a live 7-day
    mute_protection row now ADDS to it — the expiry lands at
    ``existing + 24h``, not at ``now + 24h``.

    This test used to pin the opposite (REPLACE, mirroring legacy
    ``apply_mute_protection``'s unconditional UPSERT), and that was the
    bug: the payload is presence-only ``{}``, so the second item bought
    nothing but a shorter window than the user already had. Rewritten
    rather than deleted — the seeded-longer-row case is exactly the one
    that has to keep being covered, only its expected answer changed.
    REPLACE still holds where the payload DIFFERS (color_nick,
    xp_boost with another multiplier); see
    :meth:`PrivilegesRepo.grant_with_value`."""
    item_id = await _seed_mute_protection_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    far_future_ts = (_NOW + timedelta(days=7)).timestamp()
    session.add(
        UserPrivilege(
            user_id=42,
            privilege_type="mute_protection",
            group_id=0,
            expires_at=far_future_ts,
            value="{}",
        )
    )
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    expected = _NOW + timedelta(days=7) + timedelta(hours=24)
    assert result.outcome is UseOutcome.SUCCESS
    # The service reports what the row actually holds, not what this
    # single item contributed — the copy would otherwise under-promise.
    assert result.mute_protection_expires_at == expected

    priv = await session.get(UserPrivilege, (42, "mute_protection", 0))
    assert priv is not None
    assert priv.expires_at == expected.timestamp()


async def test_use_double_daily_grants_buster_and_marks_entry_used(
    session: AsyncSession,
) -> None:
    item_id = await _seed_buster_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert result.kind is InventoryEffectKind.DOUBLE_DAILY_BUSTER
    assert result.granted_till is None
    assert result.buster_expires_at == _NOW + timedelta(days=3)

    priv = await session.get(UserPrivilege, (42, "double_daily", 0))
    assert priv is not None
    assert priv.expires_at == (_NOW + timedelta(days=3)).timestamp()

    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is True


def _clamp_rng(value: int) -> object:
    """Deterministic ``random.randint``-shaped callable.

    Returns ``value`` clamped into each call's ``[lo, hi]`` bounds, so
    the weighted-choice algorithm in ``CoinPayoutSpec`` resolves
    predictably: ``value=1`` makes the first weighted range win (the
    range-pick call returns 1 ≤ first cumulative weight) and then the
    in-range value call clamps to that range's lower bound.
    """

    def rng(lo: int, hi: int) -> int:
        return min(max(value, lo), hi)

    return rng


async def test_use_luck_item_credits_payout_and_marks_entry_used(
    session: AsyncSession,
) -> None:
    """Happy-path LUCK_COIN_PAYOUT: the planner rolls a deterministic
    amount (injected RNG), the service credits the wallet by exactly
    that amount, the result carries ``coin_payout_amount``, and the
    inventory row is consumed."""
    item_id = await _seed_luck_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    # value=1 → weighted-choice picks the first range (100-150) and
    # the in-range value clamps to 100. Deterministic big-gift payout.
    svc = _service(session, random_int_inclusive=_clamp_rng(1))
    result = await svc.use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert result.kind is InventoryEffectKind.LUCK_COIN_PAYOUT
    assert result.coin_payout_amount == 100
    assert result.granted_till is None
    assert result.buster_expires_at is None

    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.balance == 100 + 100  # credited exactly the payout

    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is True
    assert row.used_date == _NOW


async def test_use_luck_item_books_a_ledger_row(session: AsyncSession) -> None:
    """#225: a gift payout used to mint coins with no ``transactions``
    row, so /balance's weekly cashflow and the finances panel showed
    nothing where legacy showed «Подарок из магазина». The row is a
    mint: no payer, the user on ``to_id``."""
    item_id = await _seed_luck_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    svc = _service(session, random_int_inclusive=_clamp_rng(1))
    await svc.use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    rows = (await session.execute(select(Transaction))).scalars().all()
    assert len(rows) == 1
    assert rows[0].from_id is None
    assert rows[0].to_id == 42
    assert rows[0].amount == 100
    assert rows[0].type == "gift_payout"
    # #1905: the ledger row carries the instant the caller
    # injected, not the repo's naive-UTC default — otherwise the
    # row and the consume it belongs to disagree by the local UTC
    # offset (three hours on the production host).
    assert rows[0].date == _NOW
    entry = await session.get(InventoryItem, entry_id)
    assert entry is not None
    assert rows[0].date == entry.used_date


async def test_use_luck_credit_failure_rolls_back_consume(
    session: AsyncSession,
) -> None:
    """Credit-failure path: a wallet at the balance cap can't absorb
    the payout (``EconomyRepo.credit`` returns None on the overflow
    guard). The service raises so the OUTER transaction rolls back —
    the entry stays unused (retryable) and the balance is unchanged.
    Honours the module's "consume + grant land together or neither"
    contract."""
    item_id = await _seed_luck_item(session)
    # Wallet sits at the cap: any positive credit overflows _MAX_AMOUNT.
    session.add(EconomyUser(user_id=42, balance=_MAX_AMOUNT, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    svc = _service(session, random_int_inclusive=_clamp_rng(1))
    with pytest.raises(RuntimeError, match="LUCK_COIN_PAYOUT credit failed"):
        await svc.use(user_id=42, entry_id=entry_id, now=_NOW)
    # Outer boundary rolls back the consume on the raise.
    await session.rollback()

    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is False  # NOT consumed — retryable

    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.balance == _MAX_AMOUNT  # unchanged


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


async def test_use_nonexistent_entry_returns_not_found(session: AsyncSession) -> None:
    result = await _service(session).use(user_id=42, entry_id=999, now=_NOW)
    assert result.outcome is UseOutcome.NOT_FOUND
    assert result.kind is None


async def test_use_other_users_entry_returns_not_found(session: AsyncSession) -> None:
    """Cross-user attempt: B clicks a /use callback packed with A's
    entry_id. Repo's ownership-baked SELECT returns None, surfaced
    as NOT_FOUND — same value as "really doesn't exist" so the
    response leaks no info about whether the id belongs to someone."""
    item_id = await _seed_vip_item(session)
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=999, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.NOT_FOUND
    # Original entry untouched.
    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is False


async def test_use_already_used_entry_returns_already_used(
    session: AsyncSession,
) -> None:
    item_id = await _seed_vip_item(session)
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id, used=True)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    assert result.outcome is UseOutcome.ALREADY_USED


async def test_use_expired_entry_returns_expired(session: AsyncSession) -> None:
    item_id = await _seed_vip_item(session)
    entry_id = await _seed_inventory_row(
        session,
        user_id=42,
        item_id=item_id,
        expires=_NOW - timedelta(seconds=1),
    )
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    assert result.outcome is UseOutcome.EXPIRED


async def test_use_unknown_effect_does_not_consume_entry(
    session: AsyncSession,
) -> None:
    """Planner returns UNKNOWN → service refuses, the entry stays
    available so the still-legacy auto-apply path can handle it (or
    a future planner upgrade can classify it)."""
    session.add(
        ShopItem(
            id=7,
            name="Legendary",
            description="",
            price=10000,
            type="legend",  # in the planner's UNKNOWN bucket
            stock=-1,
        )
    )
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=7)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.UNKNOWN_EFFECT
    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is False  # not consumed


async def test_use_when_catalog_row_deleted_returns_unknown_effect(
    session: AsyncSession,
) -> None:
    """Edge: admin deletes a shop item between purchase and /use.
    The inventory row points at a missing catalog row — planner
    can't classify, service refuses. Stays in inventory rather
    than silently consuming."""
    # Seed inventory pointing at item_id=99 with NO matching ShopItem.
    # Bypassing get_for_user's JOIN constraint by writing inventory
    # directly — get_for_user's INNER JOIN would also miss this row,
    # so the surfaced outcome is NOT_FOUND not UNKNOWN_EFFECT.
    # Instead seed catalog, take an inventory row, then delete catalog.
    item_id = await _seed_vip_item(session)
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    # Delete the catalog row after inventory is bound.
    from sqlalchemy import delete

    await session.execute(delete(ShopItem).where(ShopItem.id == item_id))
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    # JOIN in get_for_user fails on missing catalog → NOT_FOUND.
    # (This documents the actual behaviour; a future change to
    # outer-join the lookup would shift this to UNKNOWN_EFFECT.)
    assert result.outcome is UseOutcome.NOT_FOUND


# ---------------------------------------------------------------------------
# Race pin (the headline test)
# ---------------------------------------------------------------------------


async def test_concurrent_use_collapses_to_exactly_one_success(
    session: AsyncSession,
) -> None:
    """Two concurrent /use calls on the same (user, entry): the race
    guard in ``InventoryRepo.consume`` lets exactly one win. The
    other gets ALREADY_USED. The grant must be applied exactly once
    — vip_till == now + 30d (not + 60d).

    aiosqlite runs statements serially on its background thread, so
    ``asyncio.gather`` of two service calls over the same session
    interleaves them at the await points but the SQL UPDATEs hit
    the DB sequentially — exactly the legacy single-connection
    posture this race guard is designed for.
    """
    item_id = await _seed_vip_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=None))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    svc = _service(session)
    a, b = await asyncio.gather(
        svc.use(user_id=42, entry_id=entry_id, now=_NOW),
        svc.use(user_id=42, entry_id=entry_id, now=_NOW),
    )
    await session.commit()

    outcomes = {a.outcome, b.outcome}
    assert outcomes == {UseOutcome.SUCCESS, UseOutcome.ALREADY_USED}

    # Critical: exactly one grant was applied. If both calls had
    # written, vip_till would be the SAME timestamp (idempotent under
    # MAX) — but we'd also see two consume attempts both flip the
    # row, which can't happen by the rowcount guard. Asserting on
    # the inventory row's single-flip is the more direct check.
    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is True

    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.vip_till == (_NOW + timedelta(days=30)).timestamp()


# ---------------------------------------------------------------------------
# Stacking semantics through the service
# ---------------------------------------------------------------------------


async def test_use_vip_stacks_on_top_of_an_existing_grant(session: AsyncSession) -> None:
    """End-to-end stacking pin: pre-seed a wallet with vip_till 90 days
    out, redeem a 30-day item. Legacy ADDS (``bot.py:13442-13444``), so
    the buyer ends up at 120 days and the card must quote that, not
    "now + 30d".

    #192: the port did ``MAX(existing, now + duration)`` here, which
    made this exact scenario — the only one that matters, since
    ``👑 VIP статус`` is sold with ``stock=-1`` — cost 5000 COM and
    grant nothing. The reported expiry now comes back from the repo
    rather than being recomputed in the service, because only the row
    knows what it already held.
    """
    item_id = await _seed_vip_item(session)  # 30-day VIP
    existing_till = _NOW + timedelta(days=90)
    session.add(
        EconomyUser(user_id=42, balance=100, language="ru", vip_till=existing_till.timestamp())
    )
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    expected = existing_till + timedelta(days=30)
    assert result.granted_till == expected

    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.vip_till == expected.timestamp()


# ---------------------------------------------------------------------------
# #1790: a catalog row that plans to UNKNOWN is an operator problem
# ---------------------------------------------------------------------------


def _capture() -> tuple[list[dict[str, Any]], int]:
    """Attach a loguru sink recording each line's level and bound fields."""
    seen: list[dict[str, Any]] = []

    def sink(message: Any) -> None:  # noqa: ANN401 — loguru hands us its Message
        record = message.record
        seen.append({"level": record["level"].name, **record["extra"]})

    return seen, logger.add(sink, level="DEBUG")


def _use_lines(seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in seen if r.get("component") == "services.inventory_use"]


async def _seed_faucet_luck_item(session: AsyncSession, *, item_id: int = 9) -> int:
    """A luck row that pays more than it costs.

    The name is deliberately one word off the canonical big gift, so
    ``_luck_shape_for`` does not match and the uniform arm takes the
    bounds at face value: a mean of 3000 against a price of 2500. That
    is the whole of #1790 — an ordinary rename in ``/admin_shop``, no
    error, no migration, and the SKU starts minting coins.
    """
    session.add(
        ShopItem(
            id=item_id,
            name="🎁 Большие подарки",
            description="",
            price=2500,
            type="luck",
            stock=-1,
            data='{"min": 1000, "max": 5000}',
        )
    )
    return item_id


async def test_use_net_positive_luck_row_is_refused_and_changes_nothing(
    session: AsyncSession,
) -> None:
    """The refusal has to be total, not merely "no payout".

    Refusing after the consume would be worse than the faucet: the
    buyer would lose the entry AND get nothing. So this asserts the
    whole ledger — outcome, the untouched inventory row, the untouched
    balance, and the absence of any ``transactions`` row — rather than
    just the outcome. Restoring a sane price makes every outstanding
    entry work again.
    """
    item_id = await _seed_faucet_luck_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session, random_int_inclusive=_clamp_rng(1)).use(
        user_id=42, entry_id=entry_id, now=_NOW
    )
    await session.commit()

    assert result.outcome is UseOutcome.UNKNOWN_EFFECT
    assert result.coin_payout_amount is None

    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is False
    assert row.used_date is None

    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.balance == 100

    ledger = (await session.execute(select(Transaction))).scalars().all()
    assert ledger == []


async def test_unknown_effect_is_logged_with_the_row_that_caused_it(
    session: AsyncSession,
) -> None:
    """The refusal is invisible from every surface but this line.

    The buyer sees a generic "этот предмет пока нельзя использовать" and
    has no way to tell a broken catalog row from one that was never
    implemented; the operator sees nothing at all unless he happens to
    read the complaint. The fix is one ``/admin_shop`` edit, so the log
    has to say WHICH row — hence ``item_id``/``item_type``/``price``
    bound onto the line, not just a message. WARNING because a paid-for
    item that cannot be redeemed is never normal.
    """
    item_id = await _seed_faucet_luck_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    seen, sink_id = _capture()
    try:
        await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    finally:
        logger.remove(sink_id)
    await session.commit()

    lines = _use_lines(seen)
    assert len(lines) == 1
    assert lines[0]["level"] == "WARNING"
    assert lines[0]["uid"] == 42
    assert lines[0]["item_id"] == item_id
    assert lines[0]["item_type"] == "luck"
    assert lines[0]["price"] == 2500


async def test_a_usable_luck_row_logs_nothing(session: AsyncSession) -> None:
    """The counter-assertion, so the warning cannot become background noise.

    A warning that fires on the happy path teaches the operator to
    ignore it, which would cost exactly the visibility the previous
    test buys. The canonical big gift is priced at 1000 against a mean
    of 200.89, so it passes the guard.
    """
    item_id = await _seed_luck_item(session)
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_inventory_row(session, user_id=42, item_id=item_id)
    await session.commit()

    seen, sink_id = _capture()
    try:
        result = await _service(session, random_int_inclusive=_clamp_rng(1)).use(
            user_id=42, entry_id=entry_id, now=_NOW
        )
    finally:
        logger.remove(sink_id)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert _use_lines(seen) == []
