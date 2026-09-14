"""``VipDisplayService.resolve`` — privilege rows → render-ready bundle (L-36).

Pinned guarantees:

* a plain user (no cosmetic rows) resolves to an all-empty bundle
  whose ``has_any`` is False and whose ``decorate_name`` is a no-op —
  so the profile card is byte-identical to the pre-L-36 render.
* ``color_nick`` rows map the stored colour token to the legacy marker
  emoji (🌈 rainbow / 🎨 concrete colour), prepended to the name.
* ``legend`` (permanent, ``expires_at=0``) surfaces its badge + label;
  a missing badge in the payload falls back to 👑.
* ``custom_title`` surfaces the raw user text; an empty / whitespace
  title renders nothing (no bare 📝 marker).
* expired timed rows (color/title) are dropped by the repo's WHERE
  filter, so they don't show — matching legacy getter-returns-None.
* a corrupt / non-object ``value`` JSON degrades to "effect not shown"
  rather than raising mid-render.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import UserPrivilege
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.services.vip_display import VipDisplayService
from tests.integration.repositories._session import build_session

NOW = datetime(2024, 6, 15, tzinfo=UTC)


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[tuple[VipDisplayService, AsyncSession]]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield VipDisplayService(PrivilegesRepo(session)), session


async def _seed(
    session: AsyncSession,
    *,
    user_id: int,
    priv_type: str,
    value: object,
    expires_at: float = 0.0,
) -> None:
    session.add(
        UserPrivilege(
            user_id=user_id,
            privilege_type=priv_type,
            group_id=0,
            value=value if isinstance(value, str) else json.dumps(value),
            expires_at=expires_at,
        )
    )
    await session.commit()


async def test_plain_user_resolves_empty_bundle(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, _ = service
    effects = await svc.resolve(42, now=NOW)
    assert effects.has_any is False
    assert effects.color_marker == ""
    assert effects.custom_title is None
    assert effects.legend_badge is None
    # decorate_name is a strict no-op for a plain user.
    assert effects.decorate_name("<b>Alice</b>") == "<b>Alice</b>"
    assert effects.title_label("ru") is None
    assert effects.legend_label("ru") is None


async def test_rainbow_color_nick_prepends_rainbow_marker(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    future = (NOW + timedelta(days=7)).timestamp()
    await _seed(
        session, user_id=1, priv_type="color_nick", value={"color": "rainbow"}, expires_at=future
    )
    effects = await svc.resolve(1, now=NOW)
    assert effects.color_marker == "🌈"
    assert effects.decorate_name("<b>Bob</b>") == "🌈 <b>Bob</b>"
    assert effects.has_any is True


async def test_hex_color_nick_uses_palette_marker(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    future = (NOW + timedelta(days=7)).timestamp()
    await _seed(
        session, user_id=2, priv_type="color_nick", value={"color": "#ff8800"}, expires_at=future
    )
    effects = await svc.resolve(2, now=NOW)
    assert effects.color_marker == "🎨"


async def test_expired_color_nick_is_not_shown(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    past = (NOW - timedelta(days=1)).timestamp()
    await _seed(
        session, user_id=3, priv_type="color_nick", value={"color": "rainbow"}, expires_at=past
    )
    effects = await svc.resolve(3, now=NOW)
    assert effects.color_marker == ""
    assert effects.has_any is False


async def test_legend_permanent_surfaces_badge_and_label(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    # expires_at=0 → permanent (legacy apply_legend_status).
    await _seed(session, user_id=4, priv_type="legend", value={"color": "rainbow", "badge": "👑"})
    effects = await svc.resolve(4, now=NOW)
    assert effects.legend_badge == "👑"
    label = effects.legend_label("ru")
    assert label is not None
    assert label.startswith("👑 ")


async def test_legend_without_badge_falls_back_to_crown(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    await _seed(session, user_id=5, priv_type="legend", value={"color": "rainbow"})
    effects = await svc.resolve(5, now=NOW)
    assert effects.legend_badge == "👑"


async def test_custom_title_surfaces_raw_text(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    future = (NOW + timedelta(days=7)).timestamp()
    await _seed(
        session, user_id=6, priv_type="custom_title", value={"title": "The Boss"}, expires_at=future
    )
    effects = await svc.resolve(6, now=NOW)
    assert effects.custom_title == "The Boss"
    label = effects.title_label("ru")
    assert label is not None
    assert label == "📝 The Boss"


async def test_empty_custom_title_renders_nothing(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    future = (NOW + timedelta(days=7)).timestamp()
    await _seed(
        session, user_id=7, priv_type="custom_title", value={"title": "   "}, expires_at=future
    )
    effects = await svc.resolve(7, now=NOW)
    assert effects.custom_title is None
    assert effects.title_label("ru") is None


async def test_corrupt_json_value_degrades_gracefully(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    future = (NOW + timedelta(days=7)).timestamp()
    # Non-JSON garbage in the TEXT column — must not raise.
    await _seed(session, user_id=8, priv_type="color_nick", value="not-json{", expires_at=future)
    effects = await svc.resolve(8, now=NOW)
    # color row exists + active, but payload undecodable → default to
    # rainbow marker (legacy default-to-rainbow branch), never raising.
    assert effects.color_marker == "🌈"


async def test_all_three_effects_compose(
    service: tuple[VipDisplayService, AsyncSession],
) -> None:
    svc, session = service
    future = (NOW + timedelta(days=30)).timestamp()
    await _seed(
        session, user_id=9, priv_type="color_nick", value={"color": "rainbow"}, expires_at=future
    )
    await _seed(session, user_id=9, priv_type="legend", value={"badge": "💎"})
    await _seed(
        session, user_id=9, priv_type="custom_title", value={"title": "Founder"}, expires_at=future
    )
    effects = await svc.resolve(9, now=NOW)
    assert effects.color_marker == "🌈"
    assert effects.legend_badge == "💎"
    assert effects.custom_title == "Founder"
    assert effects.decorate_name("<b>X</b>") == "🌈 <b>X</b>"
    assert effects.title_label("ru") == "📝 Founder"
