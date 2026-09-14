"""End-to-end ``/groupadmin`` sub-panels (RR-4 #35/#36/#37).

The unit tests next door pin the renderers and the wire vocabulary in
isolation. What can only be pinned end-to-end is the WRITE path — that a
tap on a toggle actually lands in ``group_mod_config`` for the group the
card lives in, and that a tampered payload lands nowhere at all.

Pins:

* Nav — the Settings / Stats / Words tokens each edit the card to their
  own page; an unknown token degrades to the overview.
* Toggle — a tap flips exactly one column for exactly this group and
  re-renders with a confirmation toast.
* Picker — opening the grid edits in place; choosing an offered value
  persists it.
* Refusals — a forged ``field`` (a real column name, or one belonging to
  another subsystem) and a forged ``value`` (0 warnings, a century-long
  mute) both write NOTHING. These are the two ways a crafted callback
  could turn a moderation panel into a weapon against the group.
* A card outside a group is refused outright.

The tapping user is a developer, so the gate short-circuits before the
``get_chat_member`` probe — the gate itself is pinned in
``tests/unit/handlers/test_groupadmin.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.models.group_mod_config import GroupModConfig
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.groupadmin import (
    PAGE_SETTINGS,
    PAGE_STATS,
    PAGE_WORDS,
    GroupAdminPick,
    GroupAdminRefresh,
    GroupAdminSet,
)
from tests.e2e.handlers.conftest import make_callback_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

_GROUP = -1001234
_DEV = 42


async def _wired(make_wired: WiredFactory) -> Any:
    return await make_wired(
        schemas=[ModerationBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )


async def _config_rows(registry: EngineRegistry) -> list[GroupModConfig]:
    engine = registry.engine(DBName.MODERATION)
    async with AsyncSession(engine) as session:
        result = await session.execute(select(GroupModConfig))
        return list(result.scalars())


def _tap(data: str) -> Any:
    return make_callback_update(
        data, user_id=_DEV, chat_id=_GROUP, chat_type="supergroup", chat_title="Клуб"
    )


def _edits(sink: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in sink if e["kind"] == "edit"]


# ---------------------------------------------------------------------------
# Navigation (RR-4 #35)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page", "marker_key"),
    [
        (PAGE_SETTINGS, "h_ga_set_hint"),
        (PAGE_STATS, "h_ga_stats_log_header"),
        (PAGE_WORDS, "h_ga_words_empty"),
    ],
)
async def test_nav_opens_each_subpage(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    page: str,
    marker_key: str,
) -> None:
    bot, dispatcher, _ = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(bot, _tap(GroupAdminRefresh(section=page).pack()))
    edits = _edits(sink)
    assert len(edits) == 1
    assert t(marker_key, "ru") in edits[0]["text"]


@pytest.mark.asyncio
async def test_unknown_page_token_degrades_to_overview(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, _ = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)
    # The legacy literal still owned by the telebot process.
    await dispatcher.feed_update(bot, _tap(GroupAdminRefresh(section="moderation_settings").pack()))
    edits = _edits(sink)
    assert len(edits) == 1
    assert "Панель управления группой" in edits[0]["text"]


@pytest.mark.asyncio
async def test_panel_refused_outside_a_group(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, _ = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(GroupAdminRefresh(section=PAGE_SETTINGS).pack(), user_id=_DEV),
    )
    assert _edits(sink) == []


# ---------------------------------------------------------------------------
# Toggles + pickers write the config (RR-4 #36)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_persists_for_this_group_only(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)
    # automod defaults ON; the button therefore carries value=0.
    await dispatcher.feed_update(bot, _tap(GroupAdminSet(field="auto", value=0).pack()))

    rows = await _config_rows(registry)
    assert [row.group_id for row in rows] == [_GROUP]
    assert rows[0].automod_enabled is False
    # Nothing else moved: legacy's toggle wrote a process-global
    # settings.json, so one admin reconfigured every group.
    assert rows[0].profanity_enabled is True

    assert t("h_ga_saved", "ru") in [e.get("text") for e in sink if e["kind"] == "callback_answer"]
    assert len(_edits(sink)) == 1


@pytest.mark.asyncio
async def test_picker_opens_then_persists_choice(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(bot, _tap(GroupAdminPick(field="mute").pack()))
    assert t("h_ga_pick_header_mute", "ru", current="1 д") in _edits(sink)[0]["text"]
    assert await _config_rows(registry) == []  # opening a grid writes nothing

    await dispatcher.feed_update(bot, _tap(GroupAdminSet(field="mute", value=10080).pack()))
    rows = await _config_rows(registry)
    assert [row.mute_minutes for row in rows] == [10080]


@pytest.mark.asyncio
async def test_picker_unknown_token_falls_back_to_settings(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(bot, _tap(GroupAdminPick(field="max_warns").pack()))
    edits = _edits(sink)
    assert len(edits) == 1
    assert t("h_ga_set_hint", "ru") in edits[0]["text"]
    assert await _config_rows(registry) == []


# ---------------------------------------------------------------------------
# Forged payloads write nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        # A real column, named directly instead of by its short token.
        ("automod_enabled", 1),
        ("max_warns", 1),
        # A column of group_mod_config that no button publishes.
        ("captcha_timeout_sec", 5),
        # Structural / identity columns.
        ("group_id", 1),
        ("", 1),
        # Right token, impossible value: 0 warnings bans on the first
        # message, and 5_256_000 minutes is a decade-long "mute".
        ("warns", 0),
        ("warns", 99),
        ("mute", 5_256_000),
        ("mute", 0),
        # Right toggle token, out-of-range boolean.
        ("auto", 2),
        ("auto", -1),
    ],
)
async def test_forged_payload_writes_nothing(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    field: str,
    value: int,
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(bot, _tap(GroupAdminSet(field=field, value=value).pack()))
    assert await _config_rows(registry) == []
    # Refused silently — the tap is acked so Telegram stops spinning,
    # but nothing is edited and nothing is written.
    assert _edits(sink) == []
